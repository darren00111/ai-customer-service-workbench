# -*- coding: utf-8 -*-
"""常驻 CDP 客户端。

与旧方案的差别：旧方案每次操作都要起一个 agent-browser 子进程，重复付出
「进程启动 + Node 初始化 + CDP 握手」的成本（这是旧链路里 23% 的耗时来源）。
这里保持一条长连接，之后每次调用只是一次纯协议往返 —— 实测 0.17 ms。

线程模型：
  - 一个后台接收线程专收消息，按 id 分发到各自的队列
  - 调用方在队列上等自己的响应，因此多个线程可以并发调用
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request

import websocket

__all__ = ["CDPClient", "CDPError"]

# 访问本机 CDP 必须绕过系统代理。
# 实测踩坑：环境里存在 HTTP_PROXY / HTTPS_PROXY 时（公司网络、各类调试代理都
# 会设），urllib 会把 http://127.0.0.1:9222 的请求也塞进代理，拿回 502
# Bad Gateway —— 看起来像浏览器挂了，实际是请求根本没到本机端口。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CDPError(RuntimeError):
    pass


class _EventQueue(queue.Queue):
    def __init__(self, capacity, byte_capacity):
        self.byte_capacity = byte_capacity
        self.buffered_bytes = 0
        super().__init__(maxsize=capacity)

    def _put(self, message):
        size = len(json.dumps(message, ensure_ascii=False).encode('utf-8'))
        if self.buffered_bytes + size > self.byte_capacity:
            raise queue.Full
        self.buffered_bytes += size
        super()._put((message, size))

    def _get(self):
        message, size = super()._get()
        self.buffered_bytes -= size
        return message


class EventStream:
    """Bounded events; overflow is visible so consumers cannot silently lose chats."""
    def __init__(self, methods, capacity=1000, byte_capacity=8*1024*1024):
        self.methods = frozenset(methods)
        self.events = _EventQueue(capacity, byte_capacity)
        self.overflowed = False

    def publish(self, message):
        if message.get("method") in self.methods:
            payload = (message.get('params') or {}).get('response') or {}
            body = payload.get('payloadData')
            if isinstance(body, str) and len(body) > 2 * 1024 * 1024:
                self.overflowed = True
                return
            try:
                self.events.put_nowait(message)
            except queue.Full:
                self.overflowed = True


class CDPClient:
    def __init__(self, port: int = 9222, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self._ws: websocket.WebSocket | None = None
        self._send_lock = threading.Lock()
        self._map_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._streams: list[EventStream] = []
        self._id = 0
        self._reader: threading.Thread | None = None
        self._alive = False
        self.target_url = ""
        self.target_title = ""

    # ---------- 连接 ----------

    def list_targets(self) -> list[dict]:
        url = "http://%s:%d/json" % (self.host, self.port)
        try:
            with _NO_PROXY.open(url, timeout=3) as r:
                return json.load(r)
        except Exception as exc:
            raise CDPError("调试端口 %d 无响应：%s" % (self.port, exc)) from exc

    def _pick_page(self, prefer: str = "jinritemai") -> dict:
        pages = [t for t in self.list_targets() if t.get("type") == "page"]
        if not pages:
            raise CDPError("没有可用的 page 标签页")
        for p in pages:
            if prefer and prefer in (p.get("url") or ""):
                return p
        return pages[0]

    def connect(self, prefer: str = "jinritemai", timeout: float = 10) -> str:
        # A freshly-created Chrome profile briefly exposes a new-tab/onboarding
        # target before the requested workspace target exists.  Binding that first
        # page leaves the agent permanently watching the wrong tab.  Wait for the
        # preferred target; retain the old fallback only after the startup window so
        # callers can still report a useful wrong-site/login state.
        deadline = time.monotonic() + max(0.0, float(timeout))
        target = None
        while target is None:
            pages = [t for t in self.list_targets() if t.get("type") == "page"]
            if not pages:
                if time.monotonic() >= deadline:
                    raise CDPError("没有可用的 page 标签页")
            elif not prefer:
                target = pages[0]
            else:
                target = next(
                    (page for page in pages if prefer in (page.get("url") or "")), None
                )
                if target is None and time.monotonic() >= deadline:
                    target = pages[0]
            if target is None:
                time.sleep(0.2)
        return self.connect_target(target, timeout)

    def connect_target(self, target: dict, timeout: float = 10) -> str:
        """Connect to an explicitly selected target; no fallback to another shop."""
        if self.is_alive():
            raise CDPError("请先断开旧页面连接")
        self.target_url = target.get("url", "")
        self.target_title = target.get("title", "")
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True
        )
        # 握手用短超时；之后 recv 不能被超时打断，否则接收线程会误退出
        ws.settimeout(None)
        self._ws = ws
        self._alive = True
        self._reader = threading.Thread(target=self._recv_loop, name="cdp-recv", daemon=True)
        self._reader.start()
        return self.target_url

    def is_alive(self) -> bool:
        return bool(self._alive and self._ws)

    def subscribe(self, methods, capacity=1000) -> EventStream:
        stream = EventStream(methods, capacity)
        with self._map_lock:
            self._streams.append(stream)
        return stream

    def unsubscribe(self, stream) -> None:
        with self._map_lock:
            if stream in self._streams:
                self._streams.remove(stream)

    def _recv_loop(self) -> None:
        ws = self._ws
        while self._alive and self._ws is ws:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is None:
                with self._map_lock:
                    streams = tuple(self._streams)
                for stream in streams:
                    stream.publish(msg)
                continue
            with self._map_lock:
                box = self._pending.pop(mid, None)
            if box is not None:
                box.put(msg)
        if self._ws is ws:
            self._alive = False

    # ---------- 调用 ----------

    def call(self, method: str, params: dict | None = None, timeout: float = 15):
        if not self._alive:
            raise CDPError("连接已断开")
        with self._map_lock:
            self._id += 1
            mid = self._id
            box: queue.Queue = queue.Queue(maxsize=1)
            self._pending[mid] = box
        payload = json.dumps({"id": mid, "method": method, "params": params or {}})
        try:
            with self._send_lock:
                self._ws.send(payload)
        except Exception as exc:
            with self._map_lock:
                self._pending.pop(mid, None)
            self._alive = False
            raise CDPError("发送失败：%s" % exc) from exc
        try:
            msg = box.get(timeout=timeout)
        except queue.Empty:
            with self._map_lock:
                self._pending.pop(mid, None)
            raise CDPError("等待 %s 响应超时（%.0fs）" % (method, timeout))
        if "error" in msg:
            raise CDPError("%s 返回错误：%s" % (method, msg["error"]))
        return msg.get("result", {})

    def evaluate(self, expr: str, timeout: float = 15, by_value: bool = True):
        res = self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": by_value, "awaitPromise": True},
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            detail = json.dumps(res["exceptionDetails"], ensure_ascii=False)
            raise CDPError("页面 JS 异常：%s" % detail[:400])
        return res.get("result", {}).get("value")

    # ---------- 关闭 ----------

    def close(self) -> None:
        self._alive = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
        with self._map_lock:
            self._streams.clear()
