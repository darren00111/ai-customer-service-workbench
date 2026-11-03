# -*- coding: utf-8 -*-
"""Multi-shop supervisor used by both CLI and the future desktop UI."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

from .platforms import create_agent, platform_info
from .config import DATA_DIR, StoreRegistry, StoreSpec
from .notifications import HandoffOutbox

__all__ = ["MultiStoreSupervisor"]


class MultiStoreSupervisor:
    """Owns one isolated FeigeAgent per shop and keeps workers healthy."""

    def __init__(self, specs: list[StoreSpec] | None = None,
                 registry: StoreRegistry | None = None,
                 agent_factory: Callable[[StoreSpec], object] | None = None,
                 health_interval: float = 10.0,
                 browser_app_mode: bool = False):
        self.registry = registry or StoreRegistry()
        self.specs = specs if specs is not None else self.registry.load()
        self.registry.validate(self.specs)
        self.notifications = HandoffOutbox(DATA_DIR / "notifications.jsonl")
        self.agent_factory = agent_factory or (
            lambda spec: create_agent(
                spec, event_sink=self.notifications.publish,
                browser_app_mode=browser_app_mode,
            )
        )
        self.health_interval = max(0.2, float(health_interval))
        self.agents: dict[str, object] = {}
        self.runtime: dict[str, dict] = {
            s.store_id: {
                "state": "stopped", "last_error": "", "failures": 0,
                "last_change": time.time(), "next_retry": 0.0,
                # A desktop launch never implies that a shop should launch.  This
                # flag is an in-process user intent, set only by Start buttons.
                "desired": False,
            } for s in self.specs
        }
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    def _agent(self, spec: StoreSpec):
        with self._lock:
            if spec.store_id not in self.agents:
                self.agents[spec.store_id] = self.agent_factory(spec)
            return self.agents[spec.store_id]

    def reload_registry(self) -> None:
        """Discover newly onboarded shops without restarting existing browsers."""
        specs = self.registry.load()
        with self._lock:
            known = {spec.store_id for spec in self.specs}
            for spec in specs:
                if spec.store_id not in known:
                    self.specs.append(spec)
                    self.runtime[spec.store_id] = {
                        "state": "stopped", "last_error": "", "failures": 0,
                        "last_change": time.time(), "next_retry": 0.0, "desired": False,
                    }

    def _set(self, store_id: str, state: str, error: str = "") -> None:
        with self._lock:
            row = self.runtime[store_id]
            row["state"] = state
            row["last_error"] = error
            row["last_change"] = time.time()
            if error:
                row["failures"] = int(row.get("failures", 0)) + 1
                delay = min(60.0, 2.0 ** min(row["failures"], 6))
                row["next_retry"] = time.time() + delay
            else:
                row["failures"] = 0
                row["next_retry"] = 0.0

    def boot_store(self, store_id: str) -> dict:
        spec = next((s for s in self.specs if s.store_id == store_id), None)
        if spec is None:
            raise KeyError("未知店铺：%s" % store_id)
        connection = platform_info(spec.platform)
        if not connection["runtime_available"]:
            self._set(store_id, "connection_required", connection["status"])
            return {"ok": False, "store_id": store_id, "state": "connection_required", "detail": connection["status"]}
        with self._lock:
            self.runtime[store_id]["desired"] = True
        self._set(store_id, "starting")
        try:
            agent = self._agent(spec)
            result = agent.boot()
            if result.get("ok"):
                self._set(store_id, "online")
            else:
                state = result.get("state") or "needs_attention"
                self._set(store_id, state, result.get("detail") or result.get("hint") or state)
            return dict(result, store_id=store_id)
        except Exception as exc:  # noqa: BLE001
            self._set(store_id, "error", "%s: %s" % (type(exc).__name__, exc))
            return {"ok": False, "store_id": store_id, "state": "error", "detail": str(exc)}

    def boot_all(self, max_parallel: int = 3) -> dict:
        """Boot all enabled shops after the user explicitly requests it."""
        targets = [s for s in self.specs if s.enabled and platform_info(s.platform)["runtime_available"]]
        results = []
        if targets:
            with ThreadPoolExecutor(max_workers=min(max(1, max_parallel), len(targets))) as pool:
                future_map = {pool.submit(self.boot_store, s.store_id): s.store_id for s in targets}
                for future in as_completed(future_map):
                    results.append(future.result())
        self.start_monitor()
        return {
            "configured": len(self.specs),
            "attempted": len(targets),
            "online": sum(1 for r in results if r.get("ok")),
            "results": sorted(results, key=lambda r: r["store_id"]),
        }

    def stop_store(self, store_id: str) -> bool:
        if store_id not in self.runtime:
            return False
        with self._lock:
            self.runtime[store_id]["desired"] = False
        agent = self.agents.get(store_id)
        if agent:
            agent.stop_watch()
        self._set(store_id, "stopped")
        return True

    def start_monitor(self) -> bool:
        if self._monitor and self._monitor.is_alive():
            return False
        self._stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, name="shop-supervisor", daemon=True)
        self._monitor.start()
        return True

    def send_handoff_reply(self, event_id: str, text: str) -> dict:
        event = self.notifications.get_pending(event_id)
        if not event:
            return {"ok": False, "error": "这条待办已经处理或不存在。"}
        if event.get('external_manual'):
            return {'ok':False,'error':'该平台售后请在原平台人工回复，不能使用飞鸽发送通道。'}
        agent = self.agents.get(event.get("store_id"))
        if agent is None:
            return {"ok": False, "error": "该店铺尚未启动，无法发送草稿。"}
        result = agent.send_handoff_reply(event, text)
        if result.get("ok"):
            self.notifications.mark_done(event_id)
        return result

    def resolve_handoff(self, event_id: str) -> bool:
        event = self.notifications.get_pending(event_id)
        if not event:
            return False
        if event.get('external_manual'):
            return self.notifications.mark_done(event_id)
        spec = next((s for s in self.specs if s.store_id == event.get("store_id")), None)
        if spec:
            try:
                self._agent(spec).mark_handoff_handled(event)
            except Exception:  # noqa: BLE001
                return False
        return self.notifications.mark_done(event_id)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self.health_interval):
            now = time.time()
            for spec in self.specs:
                if not spec.enabled:
                    continue
                row = self.runtime[spec.store_id]
                if not row.get("desired"):
                    continue
                if now < float(row.get("next_retry") or 0):
                    continue
                agent = self.agents.get(spec.store_id)
                if agent is None:
                    # Only boot_store creates a browser.  The health monitor must
                    # never create one by itself, especially after a manual close.
                    self.runtime[spec.store_id]["desired"] = False
                    self._set(spec.store_id, "stopped")
                    continue
                try:
                    status = agent.status()
                    page_state = status.get("page", {}).get("state")
                    if page_state in ("ready", "no_conversation"):
                        if not status.get("watching") and agent.cfg.get("auto_send"):
                            agent.start_watch()
                        self._set(spec.store_id, "online")
                    elif page_state == "not_connected":
                        # Closing the shop browser is an explicit stop action.
                        # Do not fight the operator by launching it again.
                        with self._lock:
                            self.runtime[spec.store_id]["desired"] = False
                        try:
                            agent.stop_watch()
                        except Exception:  # noqa: BLE001
                            pass
                        self._set(spec.store_id, "stopped", "浏览器已关闭")
                    elif page_state == "not_logged_in":
                        self._set(spec.store_id, page_state, status.get("page", {}).get("hint", ""))
                    elif page_state == "wrong_site":
                        # Chrome first-run/session restore can make the initial CDP
                        # socket bind to a temporary tab.  Re-enumerate targets instead
                        # of leaving the shop permanently stuck in "页面异常".
                        probe = agent.reconnect()
                        if probe.get("ok"):
                            if agent.cfg.get("auto_send"):
                                agent.start_watch()
                            self._set(spec.store_id, "online")
                        else:
                            self._set(spec.store_id, probe.get("state") or "wrong_site",
                                      probe.get("detail") or probe.get("hint") or "重新绑定失败")
                    else:
                        probe = agent.reconnect()
                        if probe.get("ok"):
                            if agent.cfg.get("auto_send"):
                                agent.start_watch()
                            self._set(spec.store_id, "online")
                        else:
                            self._set(spec.store_id, probe.get("state") or "error",
                                      probe.get("detail") or probe.get("hint") or "重连失败")
                except Exception as exc:  # noqa: BLE001
                    self._set(spec.store_id, "error", "%s: %s" % (type(exc).__name__, exc))

    def status(self) -> dict:
        shops = []
        with self._lock:
            for spec in self.specs:
                item = {"store_id": spec.store_id, "name": spec.name,
                        "cdp_port": spec.cdp_port, "platform": spec.platform, "merchant_id": spec.merchant_id,
                        "connection": platform_info(spec.platform), **dict(self.runtime[spec.store_id])}
                if not item["connection"]["runtime_available"]:
                    item["state"] = "connection_required"
                agent = self.agents.get(spec.store_id)
                if agent:
                    try:
                        live = agent.status()
                        item["watching"] = bool(live.get("watching"))
                        item["counters"] = live.get("watcher", {}).get("counters", {})
                        item["recent"] = live.get("watcher", {}).get("recent", [])[-20:]
                        item["conversations"] = live.get("conversations", {})
                        item["total_replies"] = int(
                            item["conversations"].get("total_replies", 0) or 0
                        )
                        item["page"] = live.get("page", {})
                    except Exception as exc:  # noqa: BLE001
                        item["status_error"] = str(exc)
                else:
                    # Cumulative replies are durable and should be visible before a
                    # shop is started.  Reading them must not instantiate a browser.
                    state_path = DATA_DIR / "stores" / spec.store_id / "conversations.json"
                    candidates = [state_path, state_path.with_suffix(state_path.suffix + ".bak")]
                    for candidate in candidates:
                        try:
                            saved = json.loads(candidate.read_text(encoding="utf-8"))
                            conversations = saved.get("conversations", {}) or {}
                            item["total_replies"] = sum(
                                int(row.get("replies", 0) or 0)
                                for row in conversations.values() if isinstance(row, dict)
                            )
                            break
                        except Exception:
                            continue
                    item.setdefault("total_replies", 0)
                shops.append(item)
        return {
            "configured": len(shops),
            "online": sum(1 for s in shops if s.get("state") == "online"),
            "needs_attention": sum(1 for s in shops if s.get("state") not in ("online", "stopped")),
            "shops": shops,
            "pending_notifications": len(self.notifications.list_pending()),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }

    def shutdown(self) -> None:
        self._stop.set()
        if self._monitor and self._monitor.is_alive():
            self._monitor.join(timeout=max(1.0, self.health_interval + 0.5))
        for agent in list(self.agents.values()):
            try:
                agent.shutdown()
            except Exception:  # noqa: BLE001
                pass
