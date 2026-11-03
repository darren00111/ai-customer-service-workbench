# -*- coding: utf-8 -*-
"""端到端编排：读会话 -> 判定 -> 决定「能不能发」。阶段 3 主入口。

## 为什么是两阶段

本机沙箱里 Python 起子进程调 agent-browser 会**挂死**：实测跑
`agent-browser --cdp 9222 eval ...` 三分钟无返回，连 `--version` 都不回。
所以浏览器动作一律由 shell 执行，Python 只负责「生成载荷」和「解析结果」。

这不是设计偏好，是被环境逼出来的。完整链路见 scripts/auto_reply.sh。

## 单独用法

    # 1) 产出读取会话的载荷
    python reply_once.py --emit _tmp/p_load.txt
    # 2) shell 执行：
    #    agent-browser --cdp 9222 --max-output 200000 eval "$(< _tmp/p_load.txt)" > _tmp/raw.json
    # 3) 判定 + 出草稿
    python reply_once.py --from-raw _tmp/raw.json --force --outbox _tmp/outbox.txt

## 为什么默认不发转人工的占位回复

占位话术里写着「已为您转接人工」。但转人工入口尚未标定，我们**其实没有真的
转接**。承诺了做不到，比不承诺更糟——顾客会干等一个不会来的人。
要么先标定转人工入口，要么就只发不含承诺的表述。

退出码：
    0   有可发送的草稿，已写入 --outbox
    10  没有可发内容（无新消息 / handoff 被拦 / 无草稿）
    2   载荷或解析失败
    3   没读到消息气泡
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import kb                  # noqa: E402
import classify            # noqa: E402
import watch_feige as W    # noqa: E402

READ_JS = os.path.join(HERE, "feige_read.js")

# 只有这两种动作的草稿默认可发
SENDABLE = ("reply", "ask_followup")


def build_payload(js_path: str) -> str:
    with io.open(js_path, "r", encoding="utf-8") as f:
        js = f.read()
    b = base64.b64encode(js.encode("utf-8")).decode("ascii")
    # 不能 eval(atob(b64))：atob 产出 Latin-1，中文会乱码
    return (
        "(function(){var s=atob('%s');var u=new Uint8Array(s.length);"
        "for(var i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
        "return eval(new TextDecoder('utf-8').decode(u));})()" % b
    )


def parse_raw(path: str):
    raw = io.open(path, "r", encoding="utf-8").read().strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, str):
            obj = json.loads(obj)
        return obj
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="飞鸽自动回复编排（阶段 3，两阶段）")
    ap.add_argument("--store", default=kb.DEFAULT_STORE_ID)
    ap.add_argument("--state", default=os.path.join(ROOT, "data", "state.json"))
    ap.add_argument("--emit", default=None, help="产出读取载荷后退出")
    ap.add_argument("--from-raw", default=None, help="解析 shell 取回的会话输出")
    ap.add_argument("--outbox", default=None, help="把待发草稿写到这个文件")
    ap.add_argument("--force", action="store_true",
                    help="忽略「无新消息」，强制处理当前最新买家消息")
    ap.add_argument("--send-handoff", action="store_true",
                    help="连转人工占位回复也算作可发（见文件头说明）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    def say(*a):
        """--json 模式下 stdout 必须是纯 JSON。

        踩过的坑：以前判定块（分隔线、render 输出）和 JSON 一起打到
        stdout，调用方 json.loads(整段) 必然失败。wait_next.sh 因此
        从来没抠出过 event，新消息一条都没唤醒过人。
        所以 --json 时把人读的文本改走 stderr，stdout 只留 JSON。

        定义在 parse_args 之后、emit 分支之前：两个分支都要用它。
        """
        stream = sys.stderr if args.json else sys.stdout
        stream.write(" ".join(str(x) for x in a) + "\n")

    if args.emit:
        payload = build_payload(READ_JS)
        with io.open(args.emit, "w", encoding="ascii") as f:
            f.write(payload)
        say("payload written: %s (%d chars)" % (args.emit, len(payload)))
        return 0

    if not args.from_raw:
        sys.stderr.write("需要 --emit 或 --from-raw\n")
        return 2

    obj = parse_raw(args.from_raw)
    if obj is None:
        sys.stderr.write("无法解析 %s\n" % args.from_raw)
        return 2
    if not obj.get("bubbleCount"):
        sys.stderr.write("没读到消息气泡：会话可能没打开，或页面还在加载\n")
        return 3

    store, products = kb.load_kb(args.store)
    buyer = obj.get("buyerName") or ""
    text = obj.get("latestBuyerMessage") or ""
    card = W.extract_card(obj.get("productPanel") or "")

    state = W.load_state(args.state)
    conv = W.conv_state(state, buyer)
    sig = W.buyer_signature(obj)

    result = {"buyer": buyer, "latest_buyer": text, "has_card": bool(card),
              "event": None, "decision": None, "sendable": False, "hold_reason": None}

    def finish(code):
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        return code

    if not text:
        result["event"] = "NO_BUYER_MESSAGE"
        result["hold_reason"] = "会话里还没有买家消息"
        say("[监听] 会话[%s]里还没有买家消息" % buyer)
        return finish(10)

    if sig == conv.get("sig") and not args.force:
        result["event"] = "NO_NEW"
        result["hold_reason"] = "无新买家消息"
        say("[监听] 无新买家消息（%s 最新仍是：%s）" % (buyer, text[:40]))
        return finish(10)

    result["event"] = "FORCED" if args.force else (
        "NEW" if conv.get("sig") is not None else "BASELINE")

    # ---- L2 决策 ----
    slot = conv.get("slot")
    picked = W.try_pick_candidate(text, conv.get("candidates") or [])
    if picked:
        slot = picked.get("product_id")
        result["slot_picked"] = slot
    d = W.decide(text, store, products, card, slot,
                 pending=conv.get("candidates") or None)
    result["decision"] = d

    say("=" * 66)
    say("[%s] 会话=%s  商品卡=%s" % (result["event"], buyer, "有" if card else "无"))
    say("=" * 66)
    say(classify.render(d))
    say("-" * 66)

    # 槽位维护（与 watch_feige 同一套状态）
    if d.get("product_id") and d.get("matched_by") in (
            "card", "card_title", "text", "context"):
        conv["slot"] = d["product_id"]
        conv["candidates"] = []
    elif d.get("candidates"):
        conv["candidates"] = [{"product_id": c.get("product_id"), "title": c.get("title")}
                              for c in d["candidates"]]
    conv["sig"] = sig
    conv["last_event"] = result["event"]
    state["active"] = buyer
    W.save_state(args.state, state)

    # ---- 决定能不能发 ----
    draft = d.get("draft_reply") or ""
    action = d.get("action")

    if not draft:
        result["hold_reason"] = "没有草稿（%s）" % action
        say("没有草稿可发。")
        return finish(10)

    if action not in SENDABLE and not (action == "handoff" and args.send_handoff):
        result["hold_reason"] = "动作 %s 默认不发（转人工入口未接通，不能承诺已转接）" % action
        say("动作是 %s，默认不发送。" % action)
        say("原因：占位话术写着「已为您转接」，但转人工入口尚未标定，")
        say("      我们并没有真的转接——承诺做不到比不承诺更糟。")
        say("      确认要发就加 --send-handoff。")
        return finish(10)

    result["sendable"] = True
    if args.outbox:
        with io.open(args.outbox, "w", encoding="utf-8", newline="\n") as f:
            f.write(draft)
        say("待发草稿已写入 %s（%d 字符）" % (args.outbox, len(draft)))
    say("✅ 可发送（动作=%s，分类=%s）" % (action, d.get("category")))
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
