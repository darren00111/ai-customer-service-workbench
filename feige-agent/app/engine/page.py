# -*- coding: utf-8 -*-
"""页面代理：把 fga.js 注入飞鸽页面，并对外暴露成普通的 Python 方法。

注入只在「页面刷新导致丢失」时发生，正常情况下每次调用都只是一次 CDP 往返。
页面状态探测放在这里，因为「未登录」和「页面没加载完」需要区分对待：
前者要提示人去扫码，后者只需要等。
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from .cdp import CDPClient, CDPError

__all__ = ["PageAgent", "PageState"]

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
FGA_VERSION = 8


class PageState:
    NOT_CONNECTED = "not_connected"      # 连不上调试端口
    NO_PAGE = "no_page"                  # 没有可用标签页
    NOT_LOGGED_IN = "not_logged_in"      # 需要人工扫码登录
    LOADING = "loading"                  # 页面在加载
    NO_CONVERSATION = "no_conversation"  # 已登录但没有打开会话
    READY = "ready"                      # 可以工作
    WRONG_SITE = "wrong_site"            # 当前标签页不是飞鸽后台
    NETWORK_ERROR = "network_error"      # 浏览器网络错误页


class PageAgent:
    def __init__(self, cdp: CDPClient, workspace_host: str | None = "im.jinritemai.com"):
        self.cdp = cdp
        # 传 None 可跳过「必须是飞鸽页面」的校验，自测时指向本地模拟页用
        self.workspace_host = workspace_host
        # CDP supports concurrent requests, but the page itself is a single mutable
        # resource.  A scan, a manual reply and a health probe must never interleave
        # clicks or reads.  Every multi-step caller shares this one re-entrant lease.
        self._control_lock = threading.RLock()
        self._control_meta_lock = threading.Lock()
        self._control_owner = ""
        self._control_since = 0.0

    @contextmanager
    def control(self, owner: str = "operation"):
        """Own the page for one complete multi-step operation."""
        self._control_lock.acquire()
        previous_owner, previous_since = "", 0.0
        try:
            with self._control_meta_lock:
                previous_owner, previous_since = self._control_owner, self._control_since
                self._control_owner = owner
                self._control_since = time.time()
            yield self
        finally:
            with self._control_meta_lock:
                self._control_owner = previous_owner
                self._control_since = previous_since
            self._control_lock.release()

    def control_status(self) -> dict:
        with self._control_meta_lock:
            return {
                "owner": self._control_owner,
                "held_seconds": max(0.0, time.time() - self._control_since)
                if self._control_since else 0.0,
            }

    # ---------- 注入 ----------

    def injected_version(self) -> int:
        try:
            return int(self.cdp.evaluate("(window.__FGA && window.__FGA.__v) || 0") or 0)
        except CDPError:
            return 0

    def inject(self, force: bool = False) -> str:
        if not force and self.injected_version() == FGA_VERSION:
            return "cached"
        src = (_ASSETS / "fga.js").read_text(encoding="utf-8")
        return str(self.cdp.evaluate(src))

    def _ensure(self) -> None:
        if self.injected_version() != FGA_VERSION:
            self.inject(force=True)

    # ---------- 状态 ----------

    def url(self) -> str:
        try:
            return str(self.cdp.evaluate("location.href") or "")
        except CDPError:
            return ""

    def probe(self) -> dict:
        """判断当前能不能干活，并给出原因。"""
        with self.control("probe"):
            return self._probe_unlocked()

    def _probe_unlocked(self) -> dict:
        try:
            url = self.url()
        except CDPError as exc:
            return {"state": PageState.NOT_CONNECTED, "detail": str(exc), "url": ""}
        if not url:
            return {"state": PageState.NOT_CONNECTED, "detail": "读不到地址", "url": ""}
        if url.startswith("chrome-error:"):
            code = self.cdp.evaluate("(document.body.innerText.match(/ERR_[A-Z_]+/)||[])[0]||''")
            return {"state": PageState.NETWORK_ERROR,
                    "detail": "网页连接失败：%s" % (code or "浏览器网络错误"), "url": url}
        if self.workspace_host and urlparse(url).hostname != self.workspace_host:
            return {"state": PageState.WRONG_SITE, "detail": "当前标签页不是飞鸽后台", "url": url}
        if "login" in url.lower():
            return {"state": PageState.NOT_LOGGED_IN, "detail": "处于登录页", "url": url}
        try:
            info = self.cdp.evaluate(
                "(function(){"
                "var t=document.querySelector('#topbar-left-info');"
                "var a=document.querySelector('[data-qa-id=\"qa-send-message-textarea\"]');"
                "var c=document.querySelectorAll('[data-qa-id=\"qa-conversation-chat-item\"]');"
                "var sk=document.querySelector('[data-qa-id=\"qa-active-chat-tab\"]')"
                "||document.querySelector('[data-qa-id=\"qa-last-chat-tab\"]');"
                "return {title:document.title, buyer:t?t.innerText.trim():'',"
                "hasInput:!!a, convCount:c.length, skeleton:!!sk,"
                "bodyLen:(document.body.innerText||'').length};})()"
            )
        except CDPError as exc:
            return {"state": PageState.LOADING, "detail": str(exc), "url": url}
        if not info:
            return {"state": PageState.LOADING, "detail": "页面对象未就绪", "url": url}

        ready = {"url": url, "info": info}
        if info.get("hasInput"):
            return dict(ready, state=PageState.READY, detail="会话已打开")

        # 没有输入框时，要分清两种完全不同的情况：
        #   未登录   -> 页面骨架（侧栏页签）压根不渲染，URL 通常已跳登录页
        #   没开会话 -> 骨架在，只是右侧等着人点会话
        # 早期把这两种混为一谈，判成「未登录」，于是明明登录着却提示去扫码。
        # 飞鸽在未选中会话时是不渲染输入框和会话项的，所以「没有」并不代表「没登录」。
        if info.get("skeleton"):
            return dict(ready, state=PageState.NO_CONVERSATION,
                        detail="已登录，但未打开任何会话")
        if info.get("bodyLen", 0) < 200:
            return dict(ready, state=PageState.LOADING, detail="页面内容过少，仍在加载")
        return dict(ready, state=PageState.NOT_LOGGED_IN,
                    detail="页面骨架未渲染，需要登录飞鸽")

    def wait_ready(self, timeout: float = 30.0, interval: float = 1.0) -> dict:
        deadline = time.time() + timeout
        last = self.probe()
        while time.time() < deadline:
            if last["state"] in (PageState.READY, PageState.NOT_LOGGED_IN):
                return last
            time.sleep(interval)
            last = self.probe()
        return last

    # ---------- 操作 ----------

    def read(self) -> dict:
        self._ensure()
        return self.cdp.evaluate("__FGA.read()") or {}

    def status(self) -> dict:
        self._ensure()
        return self.cdp.evaluate("__FGA.status()") or {}

    def fill(self, text: str) -> dict:
        self._ensure()
        return self.cdp.evaluate("__FGA.fill(%s)" % json.dumps(text)) or {}

    def submit(self) -> dict:
        self._ensure()
        return self.cdp.evaluate("__FGA.submit()") or {}

    def list_convs(self) -> list:
        self._ensure()
        return self.cdp.evaluate("__FGA.listConvs()") or []

    def tabs(self) -> list:
        self._ensure()
        return self.cdp.evaluate("__FGA.tabs()") or []

    def click_tab(self, name: str) -> dict:
        self._ensure()
        return self.cdp.evaluate("__FGA.clickTab(%s)" % json.dumps(name)) or {}

    # 侧栏页签。会话项只在「有内容」的页签里渲染，所以要按序找。
    TAB_ORDER = ("当前会话", "最近联系", "平台消息")

    def ensure_conversation(self, wait: float = 0.7) -> dict:
        """确保有一个打开的会话；当前页签是空的就换页签找。

        为什么必须自动找：页面默认停在「当前会话」，但待接待的客户可能出现在
        「最近联系」。早期只读当前 DOM 的会话项，侧栏为空就当成「没有客户」——
        实际是没切对页签，于是监听一直空转，看起来像死了一样。
        """
        convs = self.list_convs()
        if convs:
            return {"ok": True, "tab": None, "switched": False, "convs": convs}
        tried = []
        for name in self.TAB_ORDER:
            r = self.click_tab(name)
            if not r.get("ok"):
                continue
            tried.append(name)
            time.sleep(wait)
            convs = self.list_convs()
            if convs:
                return {"ok": True, "tab": name, "switched": True, "convs": convs}
        return {"ok": False, "reason": "no_conversation_found", "tried": tried,
                "tabs": [t.get("name") for t in self.tabs()]}

    def click_conv(self, index: int) -> dict:
        self._ensure()
        return self.cdp.evaluate("__FGA.clickConv(%d)" % index) or {}

    def open_conv(self, key: str, name: str = "") -> dict:
        """Re-locate and click by stable identity in one page operation."""
        self._ensure()
        return self.cdp.evaluate("__FGA.openConv(%s,%s)" % (
            json.dumps(key), json.dumps(name))) or {}

    def wait_for_sidebar_change(self, revision: int | None, timeout: float = 4.0) -> dict:
        """Wait for DOM-side sidebar changes without owning the page-control lease."""
        self._ensure()
        ms = min(5000, max(100, int(timeout * 1000)))
        return self.cdp.evaluate(
            "__FGA.waitForSidebarChange(%s,%d)" % (json.dumps(revision), ms),
            timeout=timeout + 3,
        ) or {}

    # ---------- 发送（含两道闸门） ----------

    def send_text(self, text: str) -> dict:
        """写入并提交，两步之间校验 React 状态。

        闸门存在的理由：写着空串却点了发送，就是给客户发空消息。
        """
        with self.control("send"):
            if not text or not text.strip():
                return {"ok": False, "stage": "guard", "reason": "empty_text_refused",
                        "delivery_status": "not_sent", "safe_to_retry": True}
            filled = self.fill(text)
            if not filled.get("ok"):
                return {"ok": False, "stage": "fill",
                        "reason": filled.get("reason", "fill_failed"), "detail": filled,
                        "delivery_status": "not_sent", "safe_to_retry": True}
            sent = self.submit()
            status = sent.get("deliveryStatus") or ("confirmed" if sent.get("ok") else "unknown")
            if not sent.get("ok"):
                return {"ok": False, "stage": "submit",
                        "reason": sent.get("reason", "submit_failed"), "detail": sent,
                        "delivery_status": status,
                        "safe_to_retry": bool(sent.get("safeToRetry"))}
            return {"ok": True, "stage": "done", "chars": len(text), "detail": sent,
                    "delivery_status": "confirmed", "safe_to_retry": False}
