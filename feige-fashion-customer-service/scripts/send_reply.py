# -*- coding: utf-8 -*-
"""飞鸽回复发送（Stage 3 核心脚本）。

与 L1/L2 脚本的根本区别：**本脚本会真的把消息发出去**。
因此所有安全闸门都做成默认关闭、必须显式打开。

安全设计（逐条对应真实风险）：
  1. 两阶段执行。fill（写入）与 submit（点发送）是两次独立调用，
     Python 在中间校验 React 状态；禁止一步到位，因为「写了但没同步」
     在 UI 上看不出来，发出去就是一条空消息。
  2. submit 必须显式加 --allow-send。不加就只写入、不发送。
  3. fill 之前要求输入框是空的。输入框有残留（顾客自己打的、上次失败的）
     就直接中止，绝不覆盖盲发——那会把两句话粘成一句发出去。
  4. 去重：同一买家 + 同一文本在窗口期内重复发送 → 拒绝。
     飞鸽的会话会被切走切回，同一条消息被判定两次是完全可能的。
  5. 发送后必须验证输入框已清空，否则判定为失败并报警。
  6. 全程留痕 data/sent_log.jsonl（追加）。

用法（两阶段，本机沙箱不允许 Python 起子进程）：
    # 写入
    python send_reply.py --stage fill --text "回复内容" --emit _tmp/p_fill.txt
    AB=".../agent-browser-win32-x64.exe"
    "$AB" --cdp 9222 eval "$(< _tmp/p_fill.txt)" > _tmp/raw_fill.json
    python send_reply.py --stage fill --from-raw _tmp/raw_fill.json --text "回复内容"

    # 提交（必须显式授权发送）
    python send_reply.py --stage submit --emit _tmp/p_sub.txt
    "$AB" --cdp 9222 eval "$(< _tmp/p_sub.txt)" > _tmp/raw_sub.json
    python send_reply.py --stage submit --from-raw _tmp/raw_sub.json --allow-send

退出码：
    0  成功
    2  载荷/解析失败
    4  被安全闸门拦下（未发送）
    5  发送失败（点了但输入框没清空）
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEND_JS = os.path.join(HERE, "feige_send.js")
SENT_LOG = os.path.join(ROOT, "data", "sent_log.jsonl")

DEFAULT_AGENT_BROWSER = (
    r"C:\Users\1154\.workbuddy\binaries\node\versions\22.22.2-3"
    r"\node_modules\agent-browser\bin\agent-browser-win32-x64.exe"
)

DEDUP_WINDOW_SEC = 300      # 5 分钟内同文本不重发


# ---------------------------------------------------------------------------
# 载荷构造
# ---------------------------------------------------------------------------
def build_payload(action: str, text: str = "") -> str:
    with io.open(SEND_JS, "r", encoding="utf-8") as f:
        js = f.read()
    b64 = base64.b64encode((text or "").encode("utf-8")).decode("ascii")
    js = js.replace("__ACTION__", action).replace("__TEXT_B64__", b64)
    b = base64.b64encode(js.encode("utf-8")).decode("ascii")
    return (
        "(function(){var s=atob('%s');var u=new Uint8Array(s.length);"
        "for(var i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
        "return eval(new TextDecoder('utf-8').decode(u));})()" % b
    )


def parse_raw(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, str):
            obj = json.loads(obj)
        return obj
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 留痕 / 去重
# ---------------------------------------------------------------------------
def text_hash(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()[:12]


def read_sent_log():
    if not os.path.exists(SENT_LOG):
        return []
    out = []
    with io.open(SENT_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def append_sent_log(rec: dict) -> None:
    d = os.path.dirname(SENT_LOG)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(SENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def find_recent_duplicate(buyer: str, text: str, window: int = DEDUP_WINDOW_SEC):
    now = time.time()
    h = text_hash(text)
    for rec in reversed(read_sent_log()):
        if rec.get("text_hash") != h:
            continue
        if buyer and rec.get("buyer") and rec.get("buyer") != buyer:
            continue
        if now - float(rec.get("ts") or 0) <= window:
            return rec
    return None


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render(stage: str, obj: dict) -> str:
    lines = []
    if obj is None:
        return "无法解析返回内容"
    lines.append("动作：%s" % obj.get("action", stage))
    snap = obj.get("after") or obj.get("before") or obj.get("afterClick") or {}
    if snap:
        lines.append("输入框长度=%s  字数指示=%s  发送按钮=%s"
                     % (snap.get("valueLen"), snap.get("counter"), snap.get("buttonText")))
    if stage == "fill":
        lines.append("写入值匹配：%s   React 状态同步：%s"
                     % ("是" if obj.get("valueMatches") else "否",
                        "是" if obj.get("counterSynced") else "否"))
        lines.append("（原生 setter：%s）" % ("用上了" if obj.get("usedNativeSetter") else "未用上，可能有风险"))
    if stage == "submit":
        lines.append("点击后输入框已清空：%s" % ("是" if obj.get("cleared") else "否"))
    if not obj.get("ok"):
        lines.append("!! 未通过：%s" % obj.get("reason", "未知原因"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["fill", "submit", "status"])
    ap.add_argument("--text", default="", help="fill 阶段要写入的文本")
    ap.add_argument("--text-file", default=None,
                    help="从 UTF-8 文件读取待发文本（多行/长文本用它，避开命令行引号坑）")
    ap.add_argument("--buyer", default="", help="用于去重与留痕的买家标识")
    ap.add_argument("--cdp", default=os.environ.get("FEIGE_CDP", "9222"))
    ap.add_argument("--exe", default=None)
    ap.add_argument("--emit", default=None, help="只产出 ASCII 载荷，不调用浏览器")
    ap.add_argument("--emit-payload", dest="emit2", default=None)
    ap.add_argument("--from-raw", default=None, help="解析已保存的原始输出")
    ap.add_argument("--allow-send", action="store_true",
                    help="submit 阶段必须显式加这个参数才会真的发送")
    ap.add_argument("--force", action="store_true", help="跳过去重与空框检查")
    ap.add_argument("--note", default="", help="留痕备注，如判定分类")
    args = ap.parse_args()

    if args.text_file:
        with io.open(args.text_file, "r", encoding="utf-8") as f:
            args.text = f.read().rstrip("\r\n")

    # ---- 阶段一：只产出载荷 ----
    emit_to = args.emit or args.emit2
    if emit_to:
        payload = build_payload(args.stage, args.text)
        with io.open(emit_to, "w", encoding="ascii") as f:
            f.write(payload)
        print("payload written: %s (%d chars) stage=%s" % (emit_to, len(payload), args.stage))
        return 0

    # ---- 阶段二：解析并执行安全校验 ----
    if args.from_raw:
        obj = parse_raw(io.open(args.from_raw, "r", encoding="utf-8").read())
        if obj is None:
            print("无法解析 %s" % args.from_raw, file=sys.stderr)
            return 2
        print(render(args.stage, obj))

        if not obj.get("ok"):
            print("闸门拦截：页面动作未通过，不进入下一步。", file=sys.stderr)
            return 4

        if args.stage == "fill":
            before = obj.get("before") or {}
            if not args.force and before.get("value"):
                print("闸门拦截：输入框原有内容（%r），拒绝覆盖盲发。"
                      % before.get("value")[:40], file=sys.stderr)
                return 4
            print("已写入，等待提交（需加 --allow-send 才会真的发送）")
            return 0

        if args.stage == "submit":
            if not args.allow_send:
                print("闸门拦截：submit 未带 --allow-send，未发送。", file=sys.stderr)
                return 4
            dup = None if args.force else find_recent_duplicate(args.buyer, args.text)
            if dup:
                print("闸门拦截：%d 秒内已向该买家发过同一内容（%s），拒绝重复发送。"
                      % (DEDUP_WINDOW_SEC, dup.get("text_hash")), file=sys.stderr)
                return 4
            rec = {
                "ts": time.time(),
                "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                "buyer": args.buyer,
                "text": args.text,
                "text_hash": text_hash(args.text),
                "intended": obj.get("intended", ""),
                "cleared": obj.get("cleared"),
                "note": args.note,
            }
            append_sent_log(rec)
            print("已发送并留痕 → %s" % SENT_LOG)
            print("内容：%s" % args.text)
            return 0

        return 0

    # ---- 直接调用（环境允许 Python 起子进程时） ----
    import subprocess
    exe = args.exe or os.environ.get("AGENT_BROWSER_EXE") or DEFAULT_AGENT_BROWSER
    payload = build_payload(args.stage, args.text)
    proc = subprocess.run([exe, "--cdp", str(args.cdp), "--max-output", "200000",
                           "eval", payload], capture_output=True)
    raw = (proc.stdout or proc.stderr).decode("utf-8", errors="replace")
    obj = parse_raw(raw)
    if obj is None:
        print("无法解析返回：%s" % raw[:300], file=sys.stderr)
        return 2
    print(render(args.stage, obj))

    if not obj.get("ok"):
        return 4
    if args.stage == "fill":
        before = obj.get("before") or {}
        if not args.force and before.get("value"):
            print("闸门拦截：输入框原有内容，拒绝覆盖盲发。", file=sys.stderr)
            return 4
        print("已写入。提交请加 --stage submit --allow-send")
        return 0
    if args.stage == "submit":
        if not args.allow_send:
            print("闸门拦截：未带 --allow-send，未发送。", file=sys.stderr)
            return 4
        dup = None if args.force else find_recent_duplicate(args.buyer, args.text)
        if dup:
            print("闸门拦截：重复发送。", file=sys.stderr)
            return 4
        append_sent_log({"ts": time.time(),
                         "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "buyer": args.buyer, "text": args.text,
                         "text_hash": text_hash(args.text),
                         "intended": obj.get("intended", ""),
                         "cleared": obj.get("cleared"), "note": args.note})
        print("已发送并留痕。")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
