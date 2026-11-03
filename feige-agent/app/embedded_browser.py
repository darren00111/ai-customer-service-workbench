# -*- coding: utf-8 -*-
"""Visually dock the dedicated Chrome app window inside the desktop shell.

The automation connection remains CDP-based.  This module only changes native
window ownership, chrome and bounds, so the verified profile, page and message
watcher continue to be the same single browser session.  Chrome is kept as an
owned top-level window instead of a WS_CHILD: Chromium DirectComposition often
renders a blank surface after cross-process SetParent on Windows.
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from urllib.parse import urlparse

import websocket

__all__ = ["EmbeddedBrowserController"]

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EmbeddedBrowserController:
    """Own the reversible Win32 SetParent operation for one Chrome window."""

    def __init__(self):
        self.window = None
        self._host_hwnd = 0
        self._browser_hwnd = 0
        self._port = 0
        self._original_owner = 0
        self._original_style = 0
        self._original_rect = (80, 80, 1360, 900)
        self._bounds = (390, 64, 850, 736)
        self._lock = threading.RLock()
        self._last_error = ""

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def bind_window(self, window) -> None:
        self.window = window

    def _resolve_host(self) -> int:
        if self._host_hwnd:
            return self._host_hwnd
        native = getattr(self.window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is not None:
            self._host_hwnd = int(handle.ToInt64())
        return self._host_hwnd

    @staticmethod
    def _browser_pid(port: int) -> int:
        with _NO_PROXY.open("http://127.0.0.1:%d/json/version" % port, timeout=3) as res:
            ws_url = json.load(res).get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("调试端口没有浏览器级连接")
        ws = websocket.create_connection(ws_url, timeout=4, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "SystemInfo.getProcessInfo"}))
            payload = json.loads(ws.recv())
        finally:
            ws.close()
        if payload.get("error"):
            raise RuntimeError("无法读取浏览器进程：%s" % payload["error"])
        rows = payload.get("result", {}).get("processInfo", [])
        browser = next((row for row in rows if row.get("type") == "browser"), None)
        if not browser:
            raise RuntimeError("未找到浏览器主进程")
        return int(browser["id"])

    @staticmethod
    def _find_chrome_window(pid: int, preferred_title: str) -> int:
        user32 = ctypes.windll.user32
        matches: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def visit(hwnd, _):
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) != pid or not user32.IsWindowVisible(hwnd):
                return True
            class_name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, class_name, len(class_name))
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, len(title))
            if (class_name.value.startswith("Chrome_WidgetWin_")
                    and preferred_title in title.value):
                matches.append(int(hwnd))
            return True

        user32.EnumWindows(visit, 0)
        if not matches:
            raise RuntimeError("飞鸽页面已打开，但对应的浏览器窗口尚未显示")
        # Only windows with the CDP Feige page title are eligible.  A merchant
        # home page in the same Chrome process must never win by window area.
        def area(hwnd: int) -> int:
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)

        return max(matches, key=area)

    def attach(self, port: int, wait: float = 8.0) -> dict:
        if not self.supported:
            return {"ok": False, "error": "内置原页面目前仅支持 Windows。"}
        with self._lock:
            if self._browser_hwnd and self._port != int(port):
                self.detach()
        deadline = time.monotonic() + max(0.5, wait)
        error = "浏览器窗口尚未就绪"
        while time.monotonic() < deadline:
            try:
                host = self._resolve_host()
                if not host:
                    raise RuntimeError("工作台窗口尚未就绪")
                with _NO_PROXY.open("http://127.0.0.1:%d/json" % int(port), timeout=3) as res:
                    pages = json.load(res)
                feige = next((page for page in pages if page.get("type") == "page"
                              and urlparse(page.get("url") or "").hostname == "im.jinritemai.com"), None)
                if not feige or not feige.get("title"):
                    raise RuntimeError("未找到已加载的飞鸽客服页面")
                pid = self._browser_pid(int(port))
                try:
                    hwnd = self._find_chrome_window(pid, str(feige["title"]))
                except RuntimeError:
                    # A Feige tab can exist in the background while the native
                    # window title still belongs to the merchant home tab.
                    ws = websocket.create_connection(
                        feige["webSocketDebuggerUrl"], timeout=2, suppress_origin=True)
                    try:
                        ws.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
                        while json.loads(ws.recv()).get("id") != 1:
                            pass
                    finally:
                        ws.close()
                    hwnd = self._find_chrome_window(pid, str(feige["title"]))
                with self._lock:
                    if hwnd != self._browser_hwnd:
                        self.detach()
                        user32 = ctypes.windll.user32
                        get_style = user32.GetWindowLongPtrW
                        set_style = user32.SetWindowLongPtrW
                        get_style.restype = ctypes.c_ssize_t
                        set_style.restype = ctypes.c_ssize_t
                        self._original_owner = int(get_style(hwnd, -8) or 0)
                        self._original_style = int(get_style(hwnd, -16))
                        rect = wintypes.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        self._original_rect = (rect.left, rect.top, rect.right, rect.bottom)
                        # Keep Chrome top-level for GPU/DirectComposition rendering,
                        # but make it owned by the workbench and remove browser chrome.
                        popup_style = (
                            (self._original_style | 0x80000000)
                            & ~0x40000000
                            & ~0x21CF0000
                        )
                        set_style(hwnd, -16, popup_style)
                        set_style(hwnd, -8, host)
                        # A browser previously hosted as a child may retain the
                        # minimized state bit.  In that state Chromium keeps only
                        # a 160x28 icon rectangle and suspends visual composition.
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        self._browser_hwnd = hwnd
                        self._port = int(port)
                    self._apply_bounds()
                    self._last_error = ""
                return {"ok": True, "embedded": True}
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                time.sleep(0.25)
        self._last_error = error
        self.detach()
        return {"ok": False, "error": "无法内置飞鸽原页面：%s" % error}

    def set_bounds(self, left: float, top: float, width: float, height: float) -> dict:
        with self._lock:
            host = self._resolve_host()
            scale = 1.0
            if host and hasattr(ctypes.windll.user32, "GetDpiForWindow"):
                scale = max(1.0, ctypes.windll.user32.GetDpiForWindow(host) / 96.0)
            self._bounds = tuple(max(0, round(value * scale)) for value in (left, top, width, height))
            if self._browser_hwnd:
                self._apply_bounds()
        return {"ok": True}

    def _apply_bounds(self) -> None:
        if not self._browser_hwnd:
            return
        left, top, width, height = self._bounds
        # The browser region always reaches the workbench's right and bottom
        # edges.  Deriving the final size from the native client rect avoids a
        # transient 160x28 WebView measurement seen during first layout.
        client = wintypes.RECT()
        if ctypes.windll.user32.GetClientRect(self._host_hwnd, ctypes.byref(client)):
            width = max(1, client.right - left)
            height = max(1, client.bottom - top)
        point = wintypes.POINT(left, top)
        ctypes.windll.user32.ClientToScreen(self._host_hwnd, ctypes.byref(point))
        ctypes.windll.user32.SetWindowPos(
            self._browser_hwnd, 0, point.x, point.y, max(1, width), max(1, height),
            0x0004 | 0x0010 | 0x0020 | 0x0040,
        )

    def reposition(self, *_args) -> None:
        """Follow owner moves/resizes without stealing keyboard focus."""
        with self._lock:
            self._apply_bounds()

    def restore(self, *_args) -> None:
        """Restore Chromium composition after the owner was minimized."""
        with self._lock:
            if self._browser_hwnd:
                ctypes.windll.user32.ShowWindow(self._browser_hwnd, 9)  # SW_RESTORE
            self._apply_bounds()

    def detach(self) -> None:
        with self._lock:
            if not self._browser_hwnd:
                return
            user32 = ctypes.windll.user32
            hwnd = self._browser_hwnd
            set_style = user32.SetWindowLongPtrW
            set_style.restype = ctypes.c_ssize_t
            set_style(hwnd, -8, self._original_owner)
            if self._original_style:
                set_style(hwnd, -16, self._original_style & ~0x21000000)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            left, top, right, bottom = self._original_rect
            user32.SetWindowPos(
                hwnd, 0, left, top, max(1, right - left), max(1, bottom - top),
                0x0004 | 0x0010 | 0x0020 | 0x0040,
            )
            self._browser_hwnd = 0
            self._port = 0
            self._original_owner = 0
            self._original_style = 0

    def status(self) -> dict:
        return {
            "supported": self.supported,
            "embedded": bool(self._browser_hwnd),
            "port": self._port,
            "error": self._last_error,
        }
