# -*- coding: utf-8 -*-
"""监听循环：侧栏变化触发未读扫描，周期补扫防止漏检。

单轮出错不终止循环 —— 监听断掉比报错更难发现（表面上像「没有新消息」，
实际是「没在听」）。所以异常一律记录并继续。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

__all__ = ["Watcher"]


class Watcher:
    def __init__(self, engine, cfg, keep_recent: int = 200):
        self.engine = engine
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.events: deque = deque(maxlen=keep_recent)
        self.counters = {"rounds": 0, "sent": 0, "skipped": 0, "errors": 0, "handoffs": 0}
        self.last_error = ""
        self.started_at = 0.0

    # ---------- 生命周期 ----------

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.is_running():
            return False
        self._stop.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self._thread.start()
        return True

    def stop(self, wait: float = 8.0) -> bool:
        if not self.is_running():
            return False
        self._stop.set()
        self._thread.join(timeout=wait)
        return not self._thread.is_alive()

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        next_full = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_full:
                    self.tick()
                    next_full = time.monotonic() + max(
                        5.0, float(self.cfg.get("full_scan_interval", 15.0)))
                    continue
                remaining = max(0.1, next_full - now)
                activity = self.engine.wait_for_activity(min(4.0, remaining)) if hasattr(
                    self.engine, "wait_for_activity") else None
                if activity is None:
                    interval = max(0.5, float(self.cfg.get("poll_interval", 3.0)))
                    if not self._stop.wait(interval):
                        self.tick()
                        next_full = time.monotonic() + max(
                            5.0, float(self.cfg.get("full_scan_interval", 15.0)))
                elif activity and not self._stop.is_set():
                    self.tick(unread_only=True)
            except Exception as exc:                      # noqa: BLE001
                with self._lock:
                    self.counters["errors"] += 1
                    self.last_error = "%s: %s" % (type(exc).__name__, exc)
                    self.events.append({
                        "at": datetime.now().strftime("%H:%M:%S"),
                        "level": "error", "text": self.last_error,
                    })
                self._stop.wait(1.0)

    def tick(self, unread_only: bool = False) -> dict:
        """跑一轮。界面上的「立即检查」也走这里。"""
        result = (self.engine.process_cycle(full_scan=False) if unread_only else
                  self.engine.process_cycle())
        with self._lock:
            self.counters["rounds"] += 1
            sent = int(result.get("sent") or 0)
            processed = int(result.get("processed") or 0)
            self.counters["sent"] += sent
            self.counters["skipped"] += max(0, processed - sent)
            self.counters["handoffs"] += int(result.get("handoffs") or 0)
            for item in result.get("results") or []:
                if item.get("sent") or item.get("skip") not in (
                    "这条消息已经回过", "无买家消息", None
                ):
                    self._push(item)
        return result

    def _push(self, result: dict) -> None:
        if result.get("sent"):
            text = "已回复 %s：%s" % (result.get("buyer") or "客户",
                                    (result.get("draft") or "")[:60])
            level = "sent"
        elif result.get("action") == "handoff" or result.get("paused"):
            text = "%s 已转为人工处理" % (result.get("buyer") or "客户")
            level = "handoff"
        elif result.get("skip") and result.get("latest"):
            text = "跳过：%s（%s）" % (result.get("skip"), (result.get("latest") or "")[:30])
            level = "skip"
        else:
            return
        self.events.append({"at": result.get("at") or datetime.now().strftime("%H:%M:%S"),
                            "level": level, "text": text, "detail": result})

    # ---------- 状态 ----------

    def status(self) -> dict:
        with self._lock:
            uptime = int(time.time() - self.started_at) if self.started_at else 0
            return {
                "running": self.is_running(),
                "uptime": uptime,
                "counters": dict(self.counters),
                "last_error": self.last_error,
                "recent": list(self.events)[-40:],
            }
