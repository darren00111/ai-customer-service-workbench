# -*- coding: utf-8 -*-
"""应用总控：把配置、浏览器、页面代理、判定编排、监听循环组装起来。

这个类只有一份实例，界面与命令行都通过它操作，避免两套状态各自为政。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .config import DATA_DIR, Config, StoreSpec
from .engine import browser
from .engine import cdp as cdp_mod
from .engine.page import PageAgent, PageState
from .engine.pipeline import ReplyEngine
from .engine.store import ConversationStore
from .engine.watcher import Watcher
from .service.qwen import model_manager

__all__ = ["FeigeAgent"]

_STATE_TEXT = {
    PageState.NOT_CONNECTED: "未连接浏览器",
    PageState.NO_PAGE: "没有可用标签页",
    PageState.NOT_LOGGED_IN: "需要登录飞鸽（请在弹出的浏览器里扫码）",
    PageState.LOADING: "页面加载中",
    PageState.NO_CONVERSATION: "已登录，请点开一个会话",
    PageState.READY: "就绪",
    PageState.WRONG_SITE: "当前标签页不是飞鸽后台",
    PageState.NETWORK_ERROR: "网页连接失败，请检查网络或代理后重新载入",
}


class FeigeAgent:
    def __init__(self, config: Config | None = None, store_id: str = "shop_001",
                 kb_store_id: str | None = None, runtime_dir=None, event_sink=None,
                 browser_app_mode: bool = False):
        self.store_id = store_id
        self.kb_store_id = kb_store_id or store_id
        self.runtime_dir = DATA_DIR / "stores" / store_id if runtime_dir is None else Path(runtime_dir)
        self.cfg = config or Config()
        self.cdp = cdp_mod.CDPClient(port=int(self.cfg.get("cdp_port")))
        self.page = PageAgent(self.cdp)
        self.state = ConversationStore(
            self.runtime_dir / "conversations.json",
            rate_limit_per_min=int(self.cfg.get("max_replies_per_minute", 20)),
        )
        self.engine = ReplyEngine(
            self.cfg, self.page, self.state,
            store_id=self.kb_store_id,
            runtime_store_id=self.store_id,
            event_sink=event_sink,
            interpreter_factory=lambda: model_manager().interpreter(),
        )
        self.watcher = Watcher(self.engine, self.cfg)
        self.browser_app_mode = bool(browser_app_mode)
        self.log: list[dict] = []
        self._log_lock = threading.RLock()
        self._workspace_recovery_at = float("-inf")

    @classmethod
    def from_spec(cls, spec: StoreSpec, base_config: Config | None = None, event_sink=None,
                  browser_app_mode: bool = False):
        if getattr(spec, "platform", "feige") != "feige":
            raise ValueError("飞鸽运行器不能接收其他平台的店铺")
        base = dict((base_config or Config()).data)
        cfg_path = DATA_DIR / "stores" / spec.store_id / "config.json"
        cfg = Config(cfg_path, base=base, overrides=spec.config_overrides())
        return cls(cfg, store_id=spec.store_id, kb_store_id=spec.kb_store_id,
                   event_sink=event_sink, browser_app_mode=browser_app_mode)

    # ---------- 日志 ----------

    def _note(self, text: str, level: str = "info") -> None:
        with self._log_lock:
            self.log.append({"at": datetime.now().strftime("%H:%M:%S"),
                             "level": level, "text": text})
            del self.log[:-300]

    # ---------- 启动 ----------

    def launch_browser(self) -> dict:
        port = int(self.cfg.get("cdp_port"))
        chrome = self.cfg.get("chrome_path") or None
        info = browser.ensure_browser(
            port, self.cfg.get("profile_dir"), self.cfg.get("workspace_url"), chrome=chrome,
            app_mode=self.browser_app_mode,
        )
        self._note("浏览器已就绪（%s，端口 %d）" % (info.get("status"), port))
        return info

    def connect(self) -> dict:
        """连接浏览器并注入页面代理。返回带状态与提示的字典。"""
        with self.page.control("connect"):
            return self._connect_unlocked()

    def _connect_unlocked(self) -> dict:
        workspace_url = self.cfg.get("workspace_url")
        workspace_host = urlparse(workspace_url).hostname or "im.jinritemai.com"
        if not self.cdp.is_alive():
            try:
                # Match the host rather than one exact route: Feige changes paths
                # during login and startup, while the host remains stable.
                url = self.cdp.connect(prefer=workspace_host, timeout=3.0)
                self._note("已连接页面：%s" % (url[:80] or "(空)"))
            except Exception as exc:                       # noqa: BLE001
                msg = "连接浏览器失败：%s" % exc
                self._note(msg, "error")
                return {"ok": False, "state": PageState.NOT_CONNECTED, "detail": msg}
        current = urlparse(self.page.url())
        if current.scheme == "chrome-error":
            current = urlparse(self.cdp.target_url)
        if current.hostname != workspace_host:
            targets = self.cdp.list_targets()
            workspace = next((target for target in targets if target.get("type") == "page"
                              and urlparse(target.get("url") or "").hostname == workspace_host), None)
            if workspace:
                self.cdp.close()
                self.cdp.connect(prefer=workspace_host, timeout=3.0)
            elif (current.hostname == "fxg.jinritemai.com"
                  and current.path.startswith("/ffa/")
                  and time.monotonic() - self._workspace_recovery_at >= 30.0):
                # Authentication can replace the original app tab with the
                # merchant home.  Reuse that shop's authenticated tab, rather
                # than opening more windows or navigating an unfinished login.
                self._workspace_recovery_at = time.monotonic()
                if current.path.startswith("/ffa/mshop/homepage"):
                    self.cdp.call("Page.navigate", {"url": workspace_url})
                else:
                    # Keep order/product screens intact if the operator has
                    # already left the post-login home screen.
                    browser.launch_chrome(
                        int(self.cfg.get("cdp_port")), self.cfg.get("profile_dir"),
                        workspace_url, chrome=self.cfg.get("chrome_path") or None,
                        app_mode=self.browser_app_mode)
                self._note("已登录抖店，正在重新打开飞鸽客服")
                return {"ok": False, "state": PageState.LOADING,
                        "detail": "正在打开已登录店铺的飞鸽客服", "hint": "正在载入飞鸽客服"}
        probe = self.page.probe()
        # 「没打开会话」也算通道可用：agent 正是要靠列会话 + 点会话把它打开。
        # 只有连不上、不是飞鸽页、未登录这三类才算不可用。
        usable = probe["state"] in (PageState.READY, PageState.NO_CONVERSATION)
        if usable:
            try:
                self.page.inject()
            except Exception as exc:                       # noqa: BLE001
                self._note("注入页面代理失败：%s" % exc, "error")
                return {"ok": False, "state": PageState.LOADING, "detail": str(exc)}
        probe["ok"] = usable
        probe["hint"] = _STATE_TEXT.get(probe["state"], probe["state"])
        return probe

    def boot(self) -> dict:
        """一键启动：拉起浏览器 -> 连接 -> 注入 -> 开始监听。"""
        try:
            self.launch_browser()
        except Exception as exc:                           # noqa: BLE001
            self._note(str(exc), "error")
            return {"ok": False, "state": "browser_error", "detail": str(exc),
                    "hint": "浏览器启动失败，请检查是否已安装 Chrome 或 Edge"}
        probe = self.connect()
        if probe.get("ok") and self.cfg.get("auto_send"):
            self.watcher.start()
            self._note("自动回复已开启")
        return probe

    # ---------- 控制 ----------

    def start_watch(self) -> bool:
        started = self.watcher.start()
        self._note("开始监听" if started else "监听已在运行")
        return started

    def stop_watch(self) -> bool:
        stopped = self.watcher.stop()
        self._note("已停止监听" if stopped else "监听未在运行")
        return stopped

    def check_now(self) -> dict:
        """立即跑一轮，不等轮询周期。用于界面上手动触发。"""
        result = self.watcher.tick()
        if result.get("sent"):
            self._note("本轮已回复 %s 条" % result.get("sent"), "sent")
        elif result.get("skip"):
            self._note("本轮未回复：%s" % result["skip"], "skip")
        return result

    def send_handoff_reply(self, event: dict, text: str) -> dict:
        """Serialize a reviewed send against the background page scanner.

        The page lease is sufficient; stopping the watcher could time out in the
        middle of a long scan and accidentally leave monitoring disabled.
        """
        return self._send_handoff_reply_now(event, text)

    def _send_handoff_reply_now(self, event: dict, text: str) -> dict:
        """Open the queued conversation, verify it is still current, then send."""
        with self.page.control("manual-handoff"):
            return self._send_handoff_reply_controlled(event, text)

    def _send_handoff_reply_controlled(self, event: dict, text: str) -> dict:
        reply = (text or "").strip()
        if not reply:
            return {"ok": False, "error": "草稿不能为空。"}
        if len(reply) > 800:
            return {"ok": False, "error": "草稿超过 800 字，请精简后发送。"}
        if not self.cdp.is_alive():
            return {"ok": False, "error": "该店铺浏览器未连接。"}

        self.page.click_tab("当前会话")
        time.sleep(float(self.cfg.get("conversation_switch_wait", 0.7)))
        rows = self.page.list_convs()
        conversation_id = event.get("conversation_id") or ""
        buyer = event.get("buyer") or ""
        target = next((row for row in rows if row.get("key") == conversation_id), None)
        if target is None:
            by_name = [row for row in rows if row.get("name") == buyer]
            if len(by_name) == 1:
                target = by_name[0]
        if target is None:
            return {"ok": False, "error": "当前会话列表中找不到该客户，请在飞鸽中人工处理。"}

        opened = self.page.click_conv(int(target["i"]))
        if not opened.get("ok"):
            return {"ok": False, "error": "无法打开客户会话。"}
        time.sleep(float(self.cfg.get("conversation_switch_wait", 0.7)))
        snap = self.page.read()
        current_buyer = (snap.get("buyerName") or "").strip()
        latest = (snap.get("latestBuyerMessage") or "").strip()
        expected = (event.get("summary") or "").strip()
        if current_buyer != buyer:
            return {"ok": False, "error": "会话已经切换，为避免发错客户，本次发送已取消。"}
        if expected and latest[:200] != expected:
            return {"ok": False, "error": "客户发来了新消息，请刷新草稿后再发送。"}
        if (snap.get("lastRole") or "") != "buyer":
            return {"ok": False, "error": "最后一条已不是客户消息，请先检查会话。"}

        fp = event.get("fingerprint") or event.get("event_id") or ""
        if not fp or not self.state.reserve_reply(buyer, fp, conversation_id=conversation_id):
            return {"ok": False, "error": "这条消息已发送、正在发送或等待人工核对。"}
        sent = self.page.send_text(reply)
        if not sent.get("ok"):
            if not sent.get("safe_to_retry"):
                self.state.mark_delivery_unknown(
                    buyer, fp, reply,
                    conversation_id=conversation_id,
                )
                return {"ok": False, "delivery_unknown": True,
                        "error": "发送结果无法确认，请先到飞鸽核对，系统不会自动重发。"}
            self.state.release_reply(buyer, fp, conversation_id=conversation_id)
            return {"ok": False, "error": "发送失败：%s" % sent.get("reason", "未知原因")}
        self.state.mark_handled(
            buyer, fp, expected, reply,
            conversation_id=conversation_id,
        )
        self._note("人工确认草稿并回复 %s" % (buyer or "客户"), "sent")
        return {"ok": True}

    def mark_handoff_handled(self, event: dict) -> None:
        self.state.mark_handled(
            event.get("buyer") or "", event.get("fingerprint") or "",
            event.get("summary") or "", conversation_id=event.get("conversation_id") or "",
        )

    def reconnect(self) -> dict:
        """Recover this shop's browser after a stale socket or browser crash."""
        try:
            self.cdp.close()
        except Exception:                                  # noqa: BLE001
            pass
        if not browser.cdp_alive(int(self.cfg.get("cdp_port"))):
            self.launch_browser()
        self.cdp = cdp_mod.CDPClient(port=int(self.cfg.get("cdp_port")))
        self.page = PageAgent(self.cdp)
        self.engine.page = self.page
        return self.connect()

    # ---------- 状态 ----------

    def status(self) -> dict:
        probe = {"state": PageState.NOT_CONNECTED, "hint": _STATE_TEXT[PageState.NOT_CONNECTED]}
        if self.cdp.is_alive():
            try:
                probe = self.page.probe()
                probe["hint"] = _STATE_TEXT.get(probe["state"], probe["state"])
            except Exception as exc:                       # noqa: BLE001
                probe = {"state": PageState.NOT_CONNECTED, "hint": str(exc)}
        convs = self.state.summary()
        return {
            "store_id": self.store_id,
            "page": probe,
            "ready": probe.get("state") == PageState.READY,
            "watching": self.watcher.is_running(),
            "watcher": self.watcher.status(),
            "conversations": convs,
            "config": dict(self.cfg.data),
            "kb": {"store": self.engine.store.get("store_name") or self.engine.store_id,
                   "products": len(self.engine.products)},
            "log": list(self.log)[-60:],
            "ts": datetime.now().strftime("%H:%M:%S"),
        }

    def shutdown(self) -> None:
        try:
            self.watcher.stop()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            self.cdp.close()
        except Exception:                                  # noqa: BLE001
            pass
