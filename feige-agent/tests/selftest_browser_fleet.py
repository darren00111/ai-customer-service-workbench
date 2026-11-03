# -*- coding: utf-8 -*-
"""Launch ten real isolated Chrome/CDP workers against the local Feige mock."""
from __future__ import annotations

import socket
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine import browser                  # noqa: E402
from app.engine.cdp import CDPClient             # noqa: E402
from app.engine.page import PageAgent            # noqa: E402


def free_ports(count):
    sockets, ports = [], []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        return ports
    finally:
        for sock in sockets:
            sock.close()


def wait_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if browser.cdp_alive(port):
            return True
        time.sleep(0.1)
    return False


def main():
    chrome = browser.find_chrome()
    if not chrome:
        print("FAIL 未找到 Chrome 或 Edge")
        return 1
    temp = Path(tempfile.mkdtemp(prefix="feige-fleet-"))
    mock = (ROOT / "tests" / "feige_mock.html").resolve().as_uri()
    ports = free_ports(10)
    procs = []
    started = time.perf_counter()

    def launch(index):
        proc = browser.launch_chrome(
            ports[index], temp / ("profile-%02d" % index), mock, chrome=chrome,
            extra_args=["--headless=new", "--disable-gpu", "--window-size=1000,700"],
        )
        return index, proc

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            for index, proc in pool.map(launch, range(10)):
                procs.append((index, proc))
        ready = [port for port in ports if wait_port(port)]
        if len(ready) != 10:
            print("FAIL 只有 %d/10 个浏览器调试端口就绪" % len(ready))
            return 1

        def exercise(index):
            cdp = CDPClient(port=ports[index])
            try:
                cdp.connect(prefer="")
                page = PageAgent(cdp, workspace_host=None)
                deadline = time.time() + 8
                snap = {}
                while time.time() < deadline:
                    page.inject(force=True)
                    snap = page.read()
                    if snap.get("buyerName") and snap.get("input", {}).get("hasTextarea"):
                        break
                    time.sleep(0.1)
                sent = page.send_text("店铺%02d并发通道测试" % (index + 1))
                ok = bool(snap.get("buyerName") == "热力台球" and sent.get("ok"))
                return {"index": index, "ok": ok, "buyer": snap.get("buyerName"),
                        "send": sent}
            except Exception as exc:
                return {"index": index, "ok": False, "error": "%s: %s" %
                        (type(exc).__name__, exc)}
            finally:
                cdp.close()

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(exercise, range(10)))
        elapsed = time.perf_counter() - started
        ok = sum(1 for item in results if item.get("ok"))
        for item in results:
            if not item.get("ok"):
                print("  worker %02d: %s" % (item["index"] + 1, item))
        print("%s 10 个独立浏览器/CDP/profile 并发读写（%d/10，%.2fs）" %
              ("PASS" if ok == 10 else "FAIL", ok, elapsed))
        return 0 if ok == 10 else 1
    finally:
        for _, proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        for _, proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
