# -*- coding: utf-8 -*-
"""Render the desktop UI in a real browser at wide and narrow widths."""
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine import browser       # noqa: E402
from app.engine.cdp import CDPClient  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    executable = browser.find_chrome()
    if not executable:
        print("FAIL 未找到 Chrome 或 Edge")
        return 1
    port = free_port()
    profile = tempfile.mkdtemp(prefix="feige-ui-test-")
    url = (ROOT / "app" / "ui" / "index.html").resolve().as_uri()
    proc = subprocess.Popen([
        executable, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile, url,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checks = []
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not browser.cdp_alive(port):
            time.sleep(0.1)
        cdp = CDPClient(port)
        cdp.connect(prefer="")
        time.sleep(0.3)
        for width in (1240, 700, 320):
            cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": 800, "deviceScaleFactor": 1, "mobile": False,
            })
            time.sleep(0.1)
            metrics = cdp.evaluate(
                "({innerWidth:innerWidth, scrollWidth:document.documentElement.scrollWidth})"
            )
            checks.append(("%d px 无横向溢出" % width,
                           metrics["scrollWidth"] <= metrics["innerWidth"]))

        names = cdp.evaluate(
            "Array.from(document.querySelectorAll('button')).map(function(b){"
            "return (b.getAttribute('aria-label')||b.innerText||'').trim()})"
        )
        checks.append(("所有按钮都有可访问名称", bool(names) and all(names)))
        draft_value = cdp.evaluate("document.querySelector('.draft-input').value")
        checks.append(("人工待办包含可编辑草稿", bool(draft_value)))
        before = cdp.evaluate("document.getElementById('attentionCount').textContent")
        cdp.evaluate("document.querySelector('.draft-send').click()")
        after = cdp.evaluate("document.getElementById('attentionCount').textContent")
        checks.append(("确认发送后待办立即更新", before == "3" and after == "2"))
        cdp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    failed = []
    for name, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            failed.append(name)
    print("通过 %d 项，失败 %d 项" % (len(checks) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
