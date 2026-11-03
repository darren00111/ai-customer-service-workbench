# -*- coding: utf-8 -*-
"""命令行入口。

桌面界面还没接上之前，用这个把引擎跑通、验性能：
    python run.py doctor              检查环境
    python run.py boot                拉起浏览器并连接
    python run.py peek                看当前会话
    python run.py once                跑一轮（判定 + 发送）
    python run.py once --dry          只判定，不发送
    python run.py watch --seconds 60  连续监听 N 秒
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 输出重定向到管道/文件时 Python 默认全缓冲，后台跑监听会完全看不到实时进度
# （进程没退出就一个字节都不吐）。强制行缓冲，日志才能被及时读到。
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:                                          # noqa: BLE001
    pass

from app import __version__                              # noqa: E402
from app.agent import FeigeAgent                         # noqa: E402
from app.config import (DATA_DIR, SHOP_DIR, SKILL_DIR, STORES_KB_DIR,
                        StoreRegistry)                    # noqa: E402
from app.engine import browser                           # noqa: E402
from app.multistore import MultiStoreSupervisor          # noqa: E402
from merchant import default_manifest, onboarding_errors, read_manifest  # noqa: E402
from app.engine import pipeline as engine_pipeline        # noqa: E402


def _p(title: str) -> None:
    print("\n=== %s ===" % title)


def cmd_doctor(a) -> int:
    _p("环境检查")
    print("应用版本      : %s" % __version__)
    chrome = browser.find_chrome()
    print("浏览器        : %s" % (chrome or "未找到（请安装 Chrome 或 Edge）"))
    port = a.port
    print("调试端口 %-4d : %s" % (port, "在运行" if browser.cdp_alive(port) else "未启动"))
    print("技能目录      : %s" % ("存在" if SKILL_DIR.is_dir() else "缺失 " + str(SKILL_DIR)))
    print("知识库        : %s" % ("存在" if SHOP_DIR.is_dir() else "缺失 " + str(SHOP_DIR)))
    print("数据目录      : %s" % DATA_DIR)
    try:
        __import__("yaml")
        __import__("websocket")
        print("依赖          : pyyaml / websocket-client 就绪")
    except Exception as exc:                            # noqa: BLE001
        print("依赖          : 缺失 %s" % exc)

    # 组装层检查：真的把 FeigeAgent 建一遍。engine 层的自测覆盖不到这里，
    # 早期 Config.get() 签名不匹配就是在这个环节才暴露的。
    try:
        agent = FeigeAgent()
        n = len(agent.engine.products)
        print("组装检查      : 通过（知识库 %d 件商品）" % n)
        agent.shutdown()
    except Exception as exc:                            # noqa: BLE001
        print("组装检查      : 失败 %s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


def cmd_boot(a) -> int:
    agent = FeigeAgent()
    _p("启动")
    r = agent.launch_browser()
    print("浏览器        : %s" % r)
    probe = agent.connect()
    print("页面状态      : %s（%s）" % (probe.get("state"), probe.get("hint")))
    print("地址          : %s" % probe.get("url", ""))
    if probe.get("state") in ("ready", "no_conversation"):
        conns = agent.page.list_convs()
        print("会话数        : %d" % len(conns))
        for c in conns[:6]:
            print("   %s[%2d] %-16s %s" % ("*" if c.get("isCurrent") else " ", c["i"],
                                           c["name"][:16], c.get("preview", "")[:30]))
        if probe.get("state") == "no_conversation":
            print("\n提示：没有打开的会话。可用 python run.py convs 看列表，"
                  "用 python run.py open <序号> 打开一个。")
        else:
            peek = agent.engine.peek()
            print("当前会话      : %s，气泡 %s 条" % (peek.get("buyer"), peek.get("bubbles")))
            print("最新买家消息  : %s" % (peek.get("latest") or "(无)"))
    else:
        print("\n提示：%s" % probe.get("hint"))
    agent.shutdown()
    return 0


def cmd_peek(a) -> int:
    agent = FeigeAgent()
    if not agent.connect().get("ok"):
        print("未能连接，先运行：python run.py boot")
        return 1
    peek = agent.engine.peek()
    _p("当前会话")
    print("买家          : %s" % peek.get("buyer"))
    print("气泡数        : %s" % peek.get("bubbles"))
    print("最后发言者    : %s" % peek.get("last_role"))
    print("会话状态      : %s" % ("已结束（不会自动回复）" if peek.get("closed") else "进行中"))
    print("最新买家消息  : %s" % (peek.get("latest") or "(无)"))
    _p("最近消息")
    for m in peek.get("messages") or []:
        print("  %-6s %-8s %s" % (m.get("time", ""), m.get("role", ""),
                                  (m.get("content") or "")[:60]))
    agent.shutdown()
    return 0


def cmd_once(a) -> int:
    agent = FeigeAgent()
    if not agent.connect().get("ok"):
        print("未能连接，先运行：python run.py boot")
        return 1
    t0 = time.perf_counter()
    result = agent.engine.process_once(dry_run=a.dry)
    dt = (time.perf_counter() - t0) * 1000
    _p("本轮结果")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n本机处理耗时  : %.0f ms" % dt)
    agent.shutdown()
    return 0


def cmd_watch(a) -> int:
    """持续轮询：发现客户新消息就判定并回复。

    不用 watcher 线程，直接串行跑，好处是每一步都能打出来、看得见。
    桌面界面那边用 watcher 是因为它有 UI 要更新，命令行不需要。
    """
    agent = FeigeAgent()
    probe = agent.connect()
    if not probe.get("ok"):
        print("未能连接：%s" % probe.get("hint"))
        return 1
    _p("监听中")
    print("每 %.1f 秒扫一次，最长 %s 秒%s"
          % (a.interval, a.seconds, "，收到即回复并退出" if a.until_sent else ""))
    deadline = time.time() + a.seconds
    rounds = sent = 0
    last_note = ""
    try:
        while time.time() < deadline:
            r = agent.engine.process_cycle(dry_run=a.dry)
            rounds += 1
            if int(r.get("sent") or 0):
                sent += int(r.get("sent") or 0)
                _p("已回复")
                print(json.dumps(r, ensure_ascii=False, indent=2))
                if a.until_sent:
                    break
                last_note = ""
            else:
                note = r.get("skip") or ""
                # 同一状态不重复刷屏，只在变化时打一行
                if note and note != last_note:
                    print("  [%s] %s" % (r.get("at"), note))
                    last_note = note
            time.sleep(max(0.5, a.interval))
    except KeyboardInterrupt:
        pass
    _p("监听结束")
    print("轮次 %d，已回复 %d 条" % (rounds, sent))
    agent.shutdown()
    return 0


def cmd_multi_init(a) -> int:
    registry = StoreRegistry()
    specs = registry.scaffold(count=a.count, base_port=a.base_port)
    _p("多店配置已生成")
    print("配置文件      : %s" % registry.path)
    print("店铺数        : %d" % len(specs))
    print("调试端口      : %d - %d" % (specs[0].cdp_port, specs[-1].cdp_port))
    print("每个店铺使用独立 profile、状态文件和日志目录。")
    return 0


def cmd_multi_doctor(a) -> int:
    registry = StoreRegistry()
    try:
        specs = registry.load()
    except Exception as exc:                            # noqa: BLE001
        print("多店配置无效：%s" % exc)
        return 1
    _p("多店环境检查")
    print("启用店铺      : %d" % len(specs))
    print("浏览器        : %s" % (browser.find_chrome() or "未找到"))
    failed = 0
    for spec in specs:
        kb_ok = (STORES_KB_DIR / spec.kb_store_id).is_dir()
        profile_unique = bool(spec.profile_dir)
        ok = kb_ok and profile_unique
        failed += 0 if ok else 1
        print("  %-12s 端口 %-5d 知识库:%s profile:%s" % (
            spec.store_id, spec.cdp_port, "OK" if kb_ok else "缺失",
            "OK" if profile_unique else "缺失"))
    return 0 if specs and not failed else 1


def cmd_merchant_init(a) -> int:
    """Create a single customer-owned configuration without touching existing shops."""
    import yaml
    target = STORES_KB_DIR / a.store_id
    manifest_path = target / "merchant.yaml"
    if target.exists():
        print("店铺目录已存在，不会覆盖：%s" % target)
        return 1
    try:
        existing = StoreRegistry().load(enabled_only=False)
        occupied = {spec.cdp_port for spec in existing}
        port = a.port if a.port is not None else next(
            candidate for candidate in range(9222, 65536) if candidate not in occupied
        )
        if port in occupied:
            print("调试端口已被其他店铺使用：%s" % port)
            return 1
        if any(spec.store_id == a.store_id for spec in existing):
            print("店铺标识已被使用：%s" % a.store_id)
            return 1
        data = default_manifest(a.store_id, a.name, port, a.platform, a.merchant_id)
    except Exception as exc:  # noqa: BLE001
        print("无法创建客户配置：%s" % exc)
        return 1
    target.mkdir(parents=True)
    manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
    _p("客户配置已创建")
    print("唯一配置入口  : %s" % manifest_path)
    print("自动发送      : 默认关闭；填写真实资料并通过 merchant-doctor 后再开启")
    return 0


def cmd_merchant_doctor(a) -> int:
    path = STORES_KB_DIR / a.store_id / "merchant.yaml"
    try:
        data = read_manifest(path)
        products = engine_pipeline.KB.load_products(a.store_id)
        errors = onboarding_errors(data, products)
        registry = StoreRegistry()
        registry.load(enabled_only=False)
    except Exception as exc:  # noqa: BLE001
        print("配置无效：%s" % exc)
        return 1
    _p("客户接入检查")
    print("店铺          : %s" % data.get("name", a.store_id))
    print("商品          : %d 件" % len(products))
    print("自动发送      : %s" % ("开启" if data["automation"].get("auto_send") else "关闭"))
    for error in errors:
        print("待完善        : %s" % error)
    return 1 if errors else 0


def cmd_multi_status(a) -> int:
    supervisor = MultiStoreSupervisor()
    try:
        print(json.dumps(supervisor.status(), ensure_ascii=False, indent=2))
    finally:
        supervisor.shutdown()
    return 0


def cmd_multi_boot(a) -> int:
    supervisor = MultiStoreSupervisor()
    try:
        result = supervisor.boot_all(max_parallel=a.parallel)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if a.seconds > 0:
            deadline = time.time() + a.seconds
            while time.time() < deadline:
                time.sleep(min(2.0, max(0.1, deadline - time.time())))
            _p("运行状态")
            print(json.dumps(supervisor.status(), ensure_ascii=False, indent=2))
        return 0 if result.get("online") == result.get("attempted") else 1
    finally:
        supervisor.shutdown()


def cmd_desktop(a) -> int:
    from app.desktop import run_desktop
    run_desktop(debug=a.debug, initial_store_id=a.store)
    return 0


def cmd_convs(a) -> int:
    agent = FeigeAgent()
    if not agent.connect().get("ok"):
        print("未能连接，先运行：python run.py boot")
        return 1
    _p("侧栏页签")
    for t in agent.page.tabs():
        print("  %s %s" % ("*" if t.get("selected") else " ", t.get("name")))
    r = agent.page.ensure_conversation()
    _p("会话列表（%s）" % (("已切到「%s」" % r["tab"]) if r.get("tab") else "当前页签"))
    convs = r.get("convs") or []
    if not convs:
        print("  没有找到任何会话（%s）" % r.get("reason", "unknown"))
    for c in convs:
        print("  %s[%2d] %-16s %s" % ("*" if c.get("isCurrent") else " ", c["i"],
                                      c["name"][:16], c.get("preview", "")[:34]))
    agent.shutdown()
    return 0


def cmd_open(a) -> int:
    agent = FeigeAgent()
    if not agent.connect().get("ok"):
        print("未能连接，先运行：python run.py boot")
        return 1
    r = agent.page.click_conv(a.index)
    _p("打开会话 #%d" % a.index)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    peek = agent.engine.peek()
    print("当前会话      : %s，气泡 %s 条" % (peek.get("buyer"), peek.get("bubbles")))
    print("最新买家消息  : %s" % (peek.get("latest") or "(无)"))
    agent.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="飞鸽客服智能体")
    ap.add_argument("--port", type=int, default=9222, help="浏览器调试端口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="检查环境").set_defaults(func=cmd_doctor)
    sub.add_parser("boot", help="拉起浏览器并连接").set_defaults(func=cmd_boot)
    sub.add_parser("peek", help="看当前会话").set_defaults(func=cmd_peek)
    sub.add_parser("convs", help="列出会话").set_defaults(func=cmd_convs)

    p3 = sub.add_parser("open", help="打开指定序号的会话")
    p3.add_argument("index", type=int, help="run.py convs 里显示的序号")
    p3.set_defaults(func=cmd_open)

    p1 = sub.add_parser("once", help="跑一轮")
    p1.add_argument("--dry", action="store_true", help="只判定不发送")
    p1.set_defaults(func=cmd_once)

    p2 = sub.add_parser("watch", help="持续监听并自动回复")
    p2.add_argument("--seconds", type=int, default=60, help="最长监听时长")
    p2.add_argument("--interval", type=float, default=2.0, help="扫描间隔（秒）")
    p2.add_argument("--until-sent", action="store_true", help="回复一条后立即退出")
    p2.add_argument("--dry", action="store_true", help="只判定不发送")
    p2.set_defaults(func=cmd_watch)

    mi = sub.add_parser("multi-init", help="生成多店隔离配置")
    mi.add_argument("--count", type=int, default=10, help="店铺数量")
    mi.add_argument("--base-port", type=int, default=9222, help="起始调试端口")
    mi.set_defaults(func=cmd_multi_init)

    sub.add_parser("multi-doctor", help="检查多店配置").set_defaults(func=cmd_multi_doctor)
    merchant_init = sub.add_parser("merchant-init", help="生成新客户的统一配置文件")
    merchant_init.add_argument("store_id", help="客户唯一英文标识")
    merchant_init.add_argument("--name", required=True, help="店铺显示名称")
    merchant_init.add_argument("--port", type=int, help="浏览器调试端口；默认选择未占用端口")
    merchant_init.add_argument("--platform", choices=["feige", "taobao", "pinduoduo", "xiaohongshu"], default="feige", help="选择平台；非飞鸽平台仅准备资料")
    merchant_init.add_argument("--merchant-id", default="local", help="同一商家的多平台店铺使用相同标识")
    merchant_init.set_defaults(func=cmd_merchant_init)
    merchant_doctor = sub.add_parser("merchant-doctor", help="检查客户资料是否可以上线")
    merchant_doctor.add_argument("store_id", help="客户唯一英文标识")
    merchant_doctor.set_defaults(func=cmd_merchant_doctor)
    sub.add_parser("multi-status", help="输出多店状态 JSON").set_defaults(func=cmd_multi_status)

    mb = sub.add_parser("multi-boot", help="启动全部店铺并监控")
    mb.add_argument("--parallel", type=int, default=3, help="同时启动浏览器数")
    mb.add_argument("--seconds", type=int, default=0, help="启动后保持命令运行的秒数")
    mb.set_defaults(func=cmd_multi_boot)

    desktop = sub.add_parser("desktop", help="打开桌面控制台")
    desktop.add_argument("--debug", action="store_true", help="打开 WebView 调试工具")
    desktop.add_argument("--store", help="打开控制台后仅启动指定店铺")
    desktop.set_defaults(func=cmd_desktop)

    lab = sub.add_parser("lab", help="打开千问设置、试聊与店铺资料接入窗口，不发送到飞鸽")
    lab.add_argument("--debug", action="store_true")
    def launch_lab(args):
        from app.service.workbench_api import run_workbench
        run_workbench(debug=args.debug)
        return 0
    lab.set_defaults(func=launch_lab)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
