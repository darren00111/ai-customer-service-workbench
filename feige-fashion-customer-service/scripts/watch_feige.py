# -*- coding: utf-8 -*-
"""飞鸽会话监听 + 判定（只读，绝不做发送动作）。

职责：
    吃进一次 read_conversation.py 的原始输出，判定「有没有新买家消息」，
    有则走 L2 决策出分类与回复草稿，并维护会话级商品槽位。

绝不做的三件事（写在这里是为了谁改都一样）：
    1. 不写入输入框
    2. 不点击发送
    3. 不切换会话

用法（两阶段 —— 本机沙箱不允许 Python 起子进程）：
    python read_conversation.py --emit-payload p.txt
    agent-browser --cdp 9222 --max-output 200000 eval "$(< p.txt)" > raw.json
    python watch_feige.py --from-raw raw.json --state data/state.json

首次运行会把当前最新消息记为基线（输出 BASELINE，不触发判定），
之后再跑，只有买家发了新消息才会输出 NEW + 决策。
想强制判定当前这条，加 --force。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kb            # noqa: E402
import classify      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 左侧「咨询商品」面板的占位文本：出现这些就说明本会话没有商品卡
PANEL_NOISE = ("暂无咨询商品", "暂无", "请选择", "未选择", "组件渲染", "加载中")

# 顾客回序号时的几种写法
ORDINAL_MAP = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
               "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
               "第1": 1, "第2": 2, "第3": 3, "第4": 4, "第5": 5,
               "第一个": 1, "第二个": 2, "第三个": 3, "第四个": 4, "第五个": 5}


def parse(raw: str):
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


def extract_card(panel: str) -> str | None:
    """左侧商品面板 → 商品卡提示。没有商品卡时返回 None。"""
    if not panel:
        return None
    p = panel.strip()
    if not p:
        return None
    for n in PANEL_NOISE:
        if n in p:
            return None
    return p


def buyer_signature(obj: dict) -> str:
    """用「买家消息条数 + 最新买家消息」做指纹。

    不用最后一条消息做指纹：系统提示（超时提醒、接入通知）会不断冒出来，
    那会导致明明没有新咨询却判定成有新消息。
    """
    msgs = obj.get("messages") or []
    n_buyer = sum(1 for m in msgs if m.get("role") == "buyer")
    return "%d|%s" % (n_buyer, obj.get("latestBuyerMessage") or "")


def load_state(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def conv_state(state: dict, buyer: str) -> dict:
    """按会话隔离状态。

    为什么要隔离：飞鸽的「当前会话」会被手动或自动切走（实测中页面一度
    切到了另一个买家）。如果所有会话共用一个 sig，切走再切回来，同一条
    消息会被重复判定好几次——重复追问/重复通知是实际事故，不是小瑕疵。
    """
    convs = state.setdefault("conversations", {})
    return convs.setdefault(buyer or "_unknown", {})


def save_state(path: str, state: dict) -> None:
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, ensure_ascii=False, indent=1))


def try_pick_candidate(text: str, cands: list):
    """顾客在候选列表里点了一个：回序号 / 回商品名。返回商品或 None。"""
    if not cands:
        return None
    t = (text or "").strip()
    if t in ORDINAL_MAP:
        i = ORDINAL_MAP[t] - 1
        return cands[i] if 0 <= i < len(cands) else None
    for c in cands:
        title = c.get("title") or ""
        if title and (title in t or t in title):
            return c
    return None


def decide(text: str, store: dict, products: list,
           card: str | None, slot: str | None, pending=None):
    """跑一次分类。商品卡若命中失败，降级为「无卡」重跑，别被噪音卡拖累。

    pending: 上一轮比较留下的候选。顾客本轮回答「我要更凉爽的」这类偏好时，
             靠它定款；不传的话偏好应答会被兜底判成意图不明转人工。
    """
    d = classify.classify(text, store, products, card, slot, pending)
    if d.get("matched_by") == "card_miss":
        d2 = classify.classify(text, store, products, None, slot, pending)
        d2["card_downgraded"] = True
        return d2
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-raw", required=True,
                    help="read_conversation.py 的原始输出文件")
    ap.add_argument("--state", default=os.path.join(ROOT, "data", "state.json"))
    ap.add_argument("--store", default=kb.DEFAULT_STORE_ID)
    ap.add_argument("--force", action="store_true",
                    help="不管有没有新消息，都判定当前最新买家消息")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    obj = parse(io.open(args.from_raw, "r", encoding="utf-8").read())
    if obj is None:
        print("无法解析 %s" % args.from_raw, file=sys.stderr)
        return 2
    if not obj.get("bubbleCount"):
        print("没读到消息气泡：会话可能没打开，或页面还在加载", file=sys.stderr)
        return 3

    store, products = kb.load_kb(args.store)
    state = load_state(args.state)
    buyer = obj.get("buyerName") or ""
    conv = conv_state(state, buyer)

    sig = buyer_signature(obj)
    prev_sig = conv.get("sig")
    text = obj.get("latestBuyerMessage") or ""
    switched = bool(state.get("active")) and state["active"] != buyer

    result = {
        "buyer": buyer,
        "session_switched": switched,
        "bubbles": obj.get("bubbleCount"),
        "has_card": bool(extract_card(obj.get("productPanel") or "")),
        "product_panel": obj.get("productPanel") or "",
        "latest_buyer": text,
        "slot_before": conv.get("slot"),
        "event": None,
        "decision": None,
    }

    if not text:
        result["event"] = "NO_BUYER_MESSAGE"
        print("[监听] 会话[%s]里还没有买家消息" % buyer)
        conv["sig"] = sig
        state["active"] = buyer
        save_state(args.state, state)
        return 0

    if sig == prev_sig and not args.force:
        result["event"] = "NO_NEW"
        print("[监听] 无新买家消息（%s 最新仍是：%s）" % (buyer, text[:40]))
        state["active"] = buyer
        save_state(args.state, state)
        return 0

    result["event"] = "NEW" if prev_sig is not None else "BASELINE"
    if args.force:
        result["event"] = "FORCED"

    card = extract_card(obj.get("productPanel") or "")
    slot = conv.get("slot")

    # 上一轮列了候选、这一轮顾客在候选里点了一个 → 先把槽位落定
    picked = try_pick_candidate(text, conv.get("candidates") or [])
    if picked:
        slot = picked.get("product_id")
        result["slot_picked"] = slot

    d = decide(text, store, products, card, slot)
    result["decision"] = d

    # 槽位维护：本轮确实认出了商品才写，歧义不清空（顾客可能下一轮补充）
    if d.get("product_id") and d.get("matched_by") in (
            "card", "card_title", "text", "context"):
        conv["slot"] = d["product_id"]
        conv["candidates"] = []
    elif d.get("candidates"):
        conv["candidates"] = [
            {"product_id": c.get("product_id"), "title": c.get("title")}
            for c in d["candidates"]
        ]
    conv["sig"] = sig
    conv["last_event"] = result["event"]
    state["active"] = buyer
    save_state(args.state, state)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("=" * 64)
        if switched:
            print("!! 会话已从上一个切换为 [%s]，状态按会话隔离，不受影响" % buyer)
        print("[%s] 会话=%s  商品卡=%s  槽位=%s"
              % (result["event"], buyer, "有" if card else "无",
                 result["slot_before"] or "空"))
        print("=" * 64)
        print(classify.render(d))
        print("-" * 64)
        print("建议动作：%s（本脚本只出建议，不发送）"
              % {"reply": "可直接回复", "ask_followup": "追问顾客",
                 "handoff": "转人工并通知"}.get(d["action"], d["action"]))
        if conv.get("slot"):
            print("会话槽位：%s" % conv["slot"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
