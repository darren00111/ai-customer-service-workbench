# -*- coding: utf-8 -*-
"""Chrome 生命周期管理。

这套方案依赖浏览器而不是开放平台 API，原因是飞鸽的网页接口没有对卖家开放。
所以「把浏览器拉起来并确认登录」是应用必须自己承担的职责：
  1) 用固定 profile 目录，登录一次长期有效（cookie 持久化）
  2) 用固定调试端口，程序与页面之间的通道
  3) 启动后等待端口就绪，而不是盲等固定秒数
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

__all__ = [
    "CHROME_CANDIDATES", "find_chrome", "port_open", "cdp_alive",
    "launch_chrome", "ensure_browser", "kill_browser", "WORKSPACE_URL",
]

WORKSPACE_URL = "https://im.jinritemai.com/pc_seller_v2/main/workspace"

# 访问本机端口必须绕过系统代理。环境里若有 HTTP_PROXY / HTTPS_PROXY（公司网络、
# 各类调试代理都会设），urllib 会把 http://127.0.0.1:9222 也发进代理拿回 502，
# 表现得像浏览器挂了，实际请求根本没到本机端口。
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CHROME_CANDIDATES = [
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def find_chrome() -> str | None:
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def cdp_alive(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with _NO_PROXY.open("http://%s:%d/json/version" % (host, port), timeout=2) as r:
            return r.status == 200
    except OSError:
        # 只把网络类异常当成「端口不可用」。这里曾经写成 except Exception，
        # 结果代码里一个变量名写错也被吞成「端口不通」，排查方向被带偏了半天。
        # 编程错误就该原样抛出来。
        return False


def launch_chrome(port: int, profile_dir: str | Path, url: str = WORKSPACE_URL,
                  chrome: str | None = None, extra_args: list[str] | None = None,
                  app_mode: bool = False):
    exe = chrome or find_chrome()
    if not exe:
        raise RuntimeError(
            "没有找到 Chrome 或 Edge。请安装其一，或把可执行文件路径配置到配置里。"
        )
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % str(profile_dir),
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if extra_args:
        args.extend(extra_args)
    # App mode keeps the original Feige page while removing Chrome's tabs and
    # address bar.  It is enabled only by the embedded desktop shell; CLI and
    # multi-window operation retain the familiar normal browser window.
    args.append("--app=%s" % url if app_mode else url)
    return subprocess.Popen(args, close_fds=True)


def ensure_browser(port: int, profile_dir: str | Path, url: str = WORKSPACE_URL,
                   wait: float = 25.0, chrome: str | None = None,
                   app_mode: bool = False) -> dict:
    """确保有一个带调试端口的浏览器可用。已开则复用，不重复启动。"""
    if cdp_alive(port):
        return {"status": "reused", "port": port}
    if port_open(port):
        raise RuntimeError(
            "端口 %d 被占用但不是 CDP 服务。请更换端口，或关掉占用该端口的程序。" % port
        )
    proc = launch_chrome(port, profile_dir, url, chrome=chrome, app_mode=app_mode)
    deadline = time.time() + wait
    while time.time() < deadline:
        if cdp_alive(port):
            return {"status": "launched", "port": port, "pid": proc.pid}
        if proc.poll() is not None:
            raise RuntimeError("浏览器启动后立即退出（返回码 %s），profile 可能被占用。" % proc.returncode)
        time.sleep(0.4)
    raise RuntimeError("等待调试端口 %d 就绪超时（%.0fs）。" % (port, wait))


def kill_browser(port: int, host: str = "127.0.0.1") -> bool:
    """请求浏览器自己关闭（走 CDP），比按进程杀更干净。"""
    if not cdp_alive(port, host):
        return False
    try:
        import json
        with _NO_PROXY.open("http://%s:%d/json/version" % (host, port), timeout=3) as r:
            info = json.load(r)
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            return False
        import websocket
        ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        try:
            ws.recv()
        except Exception:
            pass
        ws.close()
        return True
    except Exception:
        return False
