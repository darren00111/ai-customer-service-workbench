# -*- coding: utf-8 -*-
"""飞鸽会话读取（Stage 1 核心脚本，只读）。

做的事：
    1) 把 scripts/feige_read.js 编成 ASCII 载荷（绕开 Windows GBK 命令行编码坑）
    2) 通过已打开的飞鸽页面（CDP）执行，拿回结构化 JSON
    3) 打印人类可读摘要，可选落盘为 JSONL 供后续回放

绝不做的事：
    不点击发送、不写入输入框、不切换会话以外的任何交互。

用法（推荐，两阶段——本机沙箱不允许 Python 起子进程）：
    python read_conversation.py --emit-payload payload.txt
    # 由 shell 执行：agent-browser --cdp 9222 eval "$(< payload.txt)" > raw.json
    python read_conversation.py --from-raw raw.json
    python read_conversation.py --from-raw r1.json --from-raw r2.json --from-raw r3.json

若环境允许 Python 起子进程，也可一步到位：
    python read_conversation.py --repeat 3

退出码：
    0  成功
    2  连不上 CDP（飞鸽页面没开 / agent-browser 挂了）
    3  读到了页面但没读到消息气泡
    4  重复读取结果不一致（说明选择器不稳，需要先修）
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
READ_JS = os.path.join(HERE, "feige_read.js")
DATA_CONV = os.path.join(PROJECT_ROOT, "data", "conversations")

DEFAULT_AGENT_BROWSER = (
    r"C:\Users\1154\.workbuddy\binaries\node\versions\22.22.2-3"
    r"\node_modules\agent-browser\bin\agent-browser-win32-x64.exe"
)

ROLE_LABEL = {
    "buyer": "买家",
    "agent": "客服",
    "ai": "智能客服",
    "merchant": "商家配置",
    "system": "系统",
    "unknown": "未知",
}


def build_payload(js_path):
    import base64
    with io.open(js_path, "r", encoding="utf-8") as f:
        js = f.read()
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    # 不能 eval(atob(b64))：atob 产出 Latin-1，中文会乱码
    return (
        "(function(){var s=atob('%s');var u=new Uint8Array(s.length);"
        "for(var i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
        "return eval(new TextDecoder('utf-8').decode(u));})()" % b64
    )


def call_cdp(payload, cdp, exe=None):
    exe = exe or os.environ.get("AGENT_BROWSER_EXE") or DEFAULT_AGENT_BROWSER
    cmd = [exe, "--cdp", str(cdp), "--max-output", "200000", "eval", payload]
    proc = subprocess.run(cmd, capture_output=True)
    raw = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, raw, err


def parse(raw):
    """agent-browser 返回的是 JSON 字符串再套一层 JSON。"""
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, str):
            obj = json.loads(obj)
        return obj
    except Exception:
        return None


def read_once(cdp, exe=None):
    payload = build_payload(READ_JS)
    code, raw, err = call_cdp(payload, cdp, exe)
    if code != 0 and not raw:
        return None, "CDP 调用失败(exit=%s): %s" % (code, err[:300])
    obj = parse(raw)
    if obj is None:
        return None, "无法解析返回: %s" % (raw[:300] or err[:300])
    return obj, None


def signature(obj):
    """用于一致性比对的指纹：忽略时间戳，只比内容。"""
    parts = [obj.get("buyerName", "")]
    for m in obj.get("messages", []):
        parts.append("%s|%s|%s" % (m.get("role"), m.get("time"), m.get("content")))
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()


def render(obj):
    lines = []
    lines.append("会话：%s    %s" % (obj.get("buyerName", "?"), obj.get("buyerMeta", "")))
    lines.append("消息容器：%s   气泡数：%s" %
                 ("OK" if obj.get("messageContainerFound") else "未找到",
                  obj.get("bubbleCount", 0)))
    lines.append("-" * 60)
    for m in obj.get("messages", []):
        lines.append("[%s] %s  %s" % (
            ROLE_LABEL.get(m.get("role"), m.get("role")),
            m.get("time", ""), m.get("content", "")))
    lines.append("-" * 60)
    lines.append("最新买家消息：%s" % (obj.get("latestBuyerMessage") or "（无）"))
    if obj.get("warnings"):
        lines.append("警告：%s" % "; ".join(obj["warnings"]))
    inp = obj.get("input") or {}
    lines.append("输入框：%s   发送按钮：%s" % (
        "就绪" if inp.get("present") and not inp.get("disabled") else "不可用",
        "存在" if (obj.get("sendButton") or {}).get("present") else "缺失"))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default=os.environ.get("FEIGE_CDP", "9222"))
    ap.add_argument("--exe", default=None)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--emit-payload", default=None,
                    help="把可执行的 ASCII 载荷写入文件后退出，不调用浏览器")
    ap.add_argument("--from-raw", action="append", default=[],
                    help="解析已保存的 agent-browser 输出（可重复传入多个做一致性比对）")
    args = ap.parse_args()

    # 阶段一：只产出载荷，交给 shell 去调 agent-browser
    if args.emit_payload:
        payload = build_payload(READ_JS)
        with io.open(args.emit_payload, "w", encoding="ascii") as f:
            f.write(payload)
        print("payload written: %s (%d chars)" % (args.emit_payload, len(payload)))
        return 0

    # 阶段二：解析 shell 已经取回的原始输出；传多个则做一致性比对
    if args.from_raw:
        results, sigs = [], []
        for p in args.from_raw:
            with io.open(p, "r", encoding="utf-8") as f:
                obj = parse(f.read())
            if obj is None:
                print("无法解析 %s" % p, file=sys.stderr)
                return 2
            results.append(obj)
            sigs.append(signature(obj))
        obj = results[-1]
        print(render(obj))
        if len(sigs) > 1:
            print("")
            if len(set(sigs)) == 1:
                print("一致性：%d/%d 次结果完全一致 ✓  指纹 %s"
                      % (len(sigs), len(sigs), sigs[0]))
            else:
                print("!! %d 次读取不一致，选择器或页面状态不稳：" % len(sigs))
                for i, s in enumerate(sigs):
                    print("   第%d次 %s" % (i + 1, s))
                if args.json_out:
                    with io.open(args.json_out, "w", encoding="utf-8") as f:
                        f.write(json.dumps(obj, ensure_ascii=False, indent=1))
                return 4
        if args.json_out:
            with io.open(args.json_out, "w", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False, indent=1))
            print("\n已写入 %s" % args.json_out)
        if args.save:
            if not os.path.isdir(DATA_CONV):
                os.makedirs(DATA_CONV)
            path = os.path.join(DATA_CONV, "read_%d.json" % obj.get("ts", 0))
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False, indent=1))
            print("已归档 %s" % path)
        return 0 if obj.get("bubbleCount") else 3

    results, sigs = [], []
    for i in range(args.repeat):
        obj, err = read_once(args.cdp, args.exe)
        if err:
            print("第 %d 次读取失败：%s" % (i + 1, err), file=sys.stderr)
            return 2
        results.append(obj)
        sigs.append(signature(obj))
        if args.repeat > 1:
            print("第 %d 次指纹 %s" % (i + 1, sigs[-1]))
        if i < args.repeat - 1:
            import time
            time.sleep(args.interval)

    obj = results[-1]

    if len(set(sigs)) > 1:
        print("!! 连续 %d 次读取结果不一致，选择器或页面状态不稳：" % args.repeat,
              file=sys.stderr)
        for i, s in enumerate(sigs):
            print("   第%d次 %s" % (i + 1, s), file=sys.stderr)
        print(render(obj))
        return 4

    print(render(obj))
    if args.repeat > 1:
        print("\n一致性：%d/%d 次结果完全一致 ✓" % (args.repeat, args.repeat))

    if args.json_out:
        with io.open(args.json_out, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        print("\n已写入 %s" % args.json_out)

    if args.save:
        if not os.path.isdir(DATA_CONV):
            os.makedirs(DATA_CONV)
        path = os.path.join(DATA_CONV, "read_%d.json" % obj.get("ts", 0))
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        print("已归档 %s" % path)

    if not obj.get("bubbleCount"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
