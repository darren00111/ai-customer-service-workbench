# -*- coding: utf-8 -*-
"""引擎自测：用本地模拟页跑通「连接 -> 注入 -> 读取 -> 写入 -> 提交」全链路。

为什么要有这个：把「代码对不对」和「真实环境能不能登录」拆成两件事。
模拟页覆盖不了 React 兼容性（那部分已在真实飞鸽里验证过），
但足以证明通道、注入、解析、发送闸门的逻辑没有写错。
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine.cdp import CDPClient, CDPError          # noqa: E402
from app.engine import browser
from app.engine import page as page_mod                  # noqa: E402
from app.engine.page import PageAgent                    # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9333
PROFILE = ROOT / "data" / "test-profile"
MOCK = (ROOT / "tests" / "feige_mock.html").as_uri()

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (passed if cond else failed).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -> " + detail) if detail and not cond else ""))


def wait_port(port: int, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if browser.cdp_alive(port):
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    print("=== 启动本地模拟页 ===")
    PROFILE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--remote-debugging-port=%d" % PORT,
         "--user-data-dir=%s" % PROFILE, "--no-first-run", "--no-default-browser-check",
         MOCK],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_port(PORT):
            print("模拟页调试端口未就绪，测试中止")
            return 2

        print("\n=== 1. 连接与注入 ===")
        cdp = CDPClient(port=PORT)
        url = cdp.connect(prefer="")            # 空 -> 取第一个 page
        check("连接 CDP", bool(url), url)
        page = PageAgent(cdp, workspace_host=None)

        t0 = time.perf_counter()
        r = page.inject(force=True)
        dt_inject = (time.perf_counter() - t0) * 1000
        check("注入页面代理", r.startswith("installed"), r)
        check("注入幂等（重复注入不重装）", page.inject() == "cached")

        t0 = time.perf_counter()
        v = page.injected_version()
        dt_probe = (time.perf_counter() - t0) * 1000
        # 期望值从注入模块读，不要硬编码：以前写死 3，脚本升到 4 之后
        # 这条就开始假报失败，白白误导一次排查。
        check("版本探测", v == page_mod.FGA_VERSION,
              "实测 %s，期望 %s" % (v, page_mod.FGA_VERSION))

        order = []
        entered = threading.Event()
        def first_owner():
            with page.control("first"):
                order.append("first-enter")
                entered.set()
                time.sleep(0.08)
                order.append("first-exit")
        def second_owner():
            entered.wait()
            with page.control("second"):
                order.append("second-enter")
        a = threading.Thread(target=first_owner)
        b = threading.Thread(target=second_owner)
        a.start(); b.start(); a.join(); b.join()
        check("页面控制权按完整操作串行", order == ["first-enter", "first-exit", "second-enter"],
              repr(order))

        print("\n=== 2. 读取 ===")
        snap = page.read()
        check("读到买家名", snap.get("buyerName") == "热力台球", repr(snap.get("buyerName")))
        check("气泡数正确", snap.get("bubbleCount") == 7, str(snap.get("bubbleCount")))
        check("最新买家消息", snap.get("latestBuyerMessage") == "160 95斤穿什么码",
              repr(snap.get("latestBuyerMessage")))
        roles = [m.get("role") for m in snap.get("messages") or []]
        check("角色识别 - 买家", roles.count("buyer") == 3, str(roles))
        check("角色识别 - 客服(带已读)", roles.count("agent") == 2, str(roles))
        check("角色识别 - 系统", "system" in roles, str(roles))
        check("角色识别 - 平台智能客服", "ai" in roles, str(roles))
        check("正文已剥离时间/已读噪声",
              all("已读" not in (m.get("content") or "") for m in snap.get("messages") or []))

        print("\n=== 3. 发送闸门 ===")
        st = page.status()
        check("输入框存在", st.get("snap", {}).get("hasTextarea") is True)
        check("发送按钮存在", st.get("snap", {}).get("hasButton") is True)

        empty = page.send_text("   ")
        check("拒绝发送空文本", empty.get("ok") is False and empty.get("stage") == "guard",
              json.dumps(empty, ensure_ascii=False))

        filled = page.fill("亲，建议您选 M 码，这款版型偏松一些。")
        check("写入输入框成功", filled.get("ok") is True, json.dumps(filled, ensure_ascii=False)[:200])
        check("React 计数已同步", filled.get("counterSynced") is True,
              str(filled.get("after", {}).get("counter")))

        sent = page.submit()
        check("提交发送成功", sent.get("ok") is True, json.dumps(sent, ensure_ascii=False)[:200])
        check("发送后输入框被清空", sent.get("cleared") is True)

        time.sleep(0.3)
        got = cdp.evaluate("window.__MOCK_SENT") or []
        check("模拟页确实收到了这条消息", len(got) == 1 and got[0].startswith("亲，建议您选 M 码"),
              json.dumps(got, ensure_ascii=False)[:120])

        snap2 = page.read()
        check("消息已进入气泡列表", snap2.get("bubbleCount") == 8, str(snap2.get("bubbleCount")))

        cdp.evaluate("window.__MOCK_DROP_CONFIRMATION = true")
        uncertain = page.send_text("只清空输入框但不生成气泡")
        check("输入框清空但无发送气泡时不误报成功",
              uncertain.get("ok") is False and
              uncertain.get("delivery_status") == "unknown" and
              uncertain.get("safe_to_retry") is False,
              json.dumps(uncertain, ensure_ascii=False)[:240])
        cdp.evaluate("window.__MOCK_DROP_CONFIRMATION = false")

        events = cdp.subscribe(['Runtime.consoleAPICalled'], capacity=1)
        cdp.call('Runtime.enable')
        cdp.evaluate("console.info('local-event-1'); console.info('local-event-2')")
        deadline = time.monotonic() + 2
        while not events.overflowed and time.monotonic() < deadline:
            time.sleep(.02)
        check('真实 CDP 事件可订阅', not events.events.empty())
        check('事件缓冲溢出明确可见', events.overflowed)
        check('事件溢出不阻断请求响应', cdp.evaluate('1+1') == 2)
        cdp.unsubscribe(events)

        print("\n=== 4. 性能（常驻连接下的完整发送）===")
        read_ms, send_ms = [], []
        for i in range(10):
            t0 = time.perf_counter()
            page.read()
            read_ms.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            page.send_text("性能测试第 %d 条，检查常驻连接下的往返耗时。" % (i + 1))
            send_ms.append((time.perf_counter() - t0) * 1000)
        print("  读取会话      : 中位 %.2f ms  (n=10)" % statistics.median(read_ms))
        print("  写入+提交     : 中位 %.2f ms  (n=10)" % statistics.median(send_ms))
        print("  注入一次      : %.1f ms" % dt_inject)
        print("  版本探测      : %.2f ms" % dt_probe)
        print("  完整一轮(读+发): 中位 %.2f ms" % statistics.median(
            [a + b for a, b in zip(read_ms, send_ms)]))

        print("\n=== 5. 断线表现 ===")
        cdp2 = CDPClient(port=9444)          # 一个确定没人监听的端口
        try:
            cdp2.connect(prefer="")
            check("连不存在的端口应报错", False, "居然连上了")
        except CDPError as exc:
            check("连不存在的端口给出明确错误", "无响应" in str(exc), str(exc)[:80])

        cdp.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    if failed:
        for f in failed:
            print("  失败：%s" % f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
