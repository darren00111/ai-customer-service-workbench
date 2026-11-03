# -*- coding: utf-8 -*-
"""多轮会话演示 —— 顾客全程不点商品卡。

回答一个关键问题：顾客不通过商品卡进来，智能客服还能不能接上话？

靠三件事兜住：
    1. 店铺级知识（政策/发货/优惠）—— 根本不需要商品
    2. 消息文本里找商品线索 —— 顾客说「连衣裙」「阔腿裤」就能定位
    3. 会话级商品槽位 —— 一旦某轮确定了商品，后续「这个」「它」都继承它

槽位更新规则（模拟真实会话状态机）：
    本轮唯一命中商品          -> 写入槽位
    本轮给出候选、顾客回序号   -> 按序号写入槽位
    其余                       -> 槽位不变（不强改，避免污染）

用法：
    python demo_nocard_session.py            # 演一轮完整会话
    python demo_nocard_session.py --store shop_001
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C  # noqa: E402
import kb            # noqa: E402

# 一条完整的顾客会话脚本：(顾客说的话, 备注)
# 全程不点商品卡，不提供任何 product 参数。
SCRIPT = [
    ("你们家连衣裙多少钱",              "纯文本线索，看能不能自己找到商品"),
    ("有M码吗",                          "纯指代 —— 依赖上一轮确认的商品"),
    ("我身高160，体重50公斤，买什么码",  "还是指代，且给了尺码要素"),
    ("这个能机洗吗",                     "指代 + 商品资料缺失"),
    ("那你们什么时候发货",               "店铺级问题，不需要商品"),
    ("支持七天无理由吗",                 "店铺政策，不需要商品"),
    ("高腰裤有M码吗",                    "出现品类+特征，但命中多款 -> 应给候选"),
    ("2",                                "顾客点序号 -> 槽位应按候选写入"),
    ("那它有现货吗",                     "再用指代验证槽位有没有被正确更新"),
    ("我要退货",                         "售后 —— 与商品识别无关，必须转人工"),
    ("能便宜点吗",                       "议价 —— 涉及让利，转人工"),
]

ACT_LABEL = {"reply": "回复", "ask_followup": "追问", "handoff": "转人工"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=kb.DEFAULT_STORE_ID)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    store, products = kb.load_kb(args.store)
    print("店铺：%s（%s），知识库商品 %d 件" % (args.store, store.get("store_name"), len(products)))
    print("场景：顾客全程不点商品卡，直接打字咨询")
    print("=" * 74)

    slot = None          # 会话级商品槽位
    pending = []         # 上一轮给出的候选，等顾客点序号
    stat = {"reply": 0, "ask_followup": 0, "handoff": 0}

    for turn, (text, note) in enumerate(SCRIPT, 1):
        # 顾客回序号 -> 先把槽位定下来，本轮归类才有商品可用
        if pending and re.fullmatch(r"\s*[1-9]\s*", text):
            idx = int(text) - 1
            if 0 <= idx < len(pending):
                slot = pending[idx]["product_id"]
                print("\n[%02d] 顾客：%s" % (turn, text))
                print("      · 顾客点选候选 -> 槽位写入 %s（%s）"
                      % (slot, pending[idx]["title"]))
                pending = []
                continue
        pending = []

        d = C.classify(text, store, products, None, slot)
        stat[d["action"]] = stat.get(d["action"], 0) + 1

        print("\n[%02d] 顾客：%s" % (turn, text))
        print("      · 意图：%s" % note)
        print("      · 判定：%s -> %s   (匹配方式=%s)"
              % (ACT_LABEL.get(d["action"], d["action"]), d["category"],
                 d.get("matched_by") or "-"))
        if d.get("product_title"):
            print("      · 商品：%s (%s)" % (d["product_title"], d["product_id"]))
            slot = d["product_id"]
        if d.get("handoff_reason"):
            print("      · 转人工原因：%s" % d["handoff_reason"])
        if d.get("draft_reply"):
            head = "      · 回复："
            for i, line in enumerate(str(d["draft_reply"]).split("\n")):
                print((head if i == 0 else " " * len(head)) + line)

        if d.get("candidates"):
            pending = d["candidates"]
            print("      · 等待顾客点选：%s"
                  % "、".join(c["title"] for c in pending))

    total = sum(stat.values())
    print("\n" + "=" * 74)
    print("全程 %d 轮，槽位最终值 = %s" % (total, slot))
    for k, label in (("reply", "自动回复"), ("ask_followup", "追问顾客"), ("handoff", "转人工")):
        n = stat.get(k, 0)
        print("  %-8s %2d 轮 %5.1f%%  %s"
              % (label, n, n * 100.0 / total, "█" * int(round(n / float(total) * 28))))
    print("\n注：其中「追问顾客」若发生在有候选的场景，顾客只需点一下序号，不用重述。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
