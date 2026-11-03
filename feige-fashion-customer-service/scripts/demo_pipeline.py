# -*- coding: utf-8 -*-
"""端到端串联演示：L1 读取结果 -> L2 决策。

把飞鸽读取脚本产出的 JSON（`data/last_read.json`）直接喂给决策逻辑，
验证「读得到 → 判得对」这一跳。

⚠️ 本脚本零副作用：不碰浏览器、不发消息。

用法：
    python demo_pipeline.py                     # 用 data/last_read.json
    python demo_pipeline.py --product P1001     # 指定商品（模拟飞鸽商品卡）
    python demo_pipeline.py --read other.json --product P1009 --show-all
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C  # noqa: E402
import kb            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_READ = os.path.join(os.path.dirname(HERE), "data", "last_read.json")

ROLE_LABEL = {
    "buyer": "买家", "agent": "客服", "ai": "智能客服",
    "merchant": "商家配置", "system": "系统",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--read", default=DEFAULT_READ, help="L1 读取结果 JSON")
    ap.add_argument("--product", default=None, help="飞鸽商品卡提示（模拟）")
    ap.add_argument("--store", default=kb.DEFAULT_STORE_ID)
    ap.add_argument("--show-all", action="store_true", help="打印全部历史消息")
    args = ap.parse_args()

    if not os.path.exists(args.read):
        print("找不到读取结果：%s" % args.read)
        return 2

    with io.open(args.read, "r", encoding="utf-8") as f:
        read = json.load(f)

    store, products = kb.load_kb(args.store)

    print("=" * 66)
    print("L1 感知层 · 读取结果")
    print("=" * 66)
    print("会话   ：%s" % read.get("buyerName"))
    print("画像   ：%s" % read.get("buyerMeta"))
    print("消息数 ：%s（容器找到=%s）" % (read.get("bubbleCount"),
                                       read.get("messageContainerFound")))
    print("一致性 ：warnings=%s" % (read.get("warnings") or "无"))

    msgs = read.get("messages") or []
    shown = msgs if args.show_all else msgs[-3:]
    print("\n最近 %d 条：" % len(shown))
    for m in shown:
        print("   [%-4s] %-9s %s" % (ROLE_LABEL.get(m.get("role"), m.get("role")),
                                     m.get("time") or "", m.get("content", "")[:60]))

    text = read.get("latestBuyerMessage") or ""
    print("\n取用最新买家消息：%s" % text)

    print()
    print("=" * 66)
    print("L2 决策层 · 分类结果")
    print("=" * 66)
    print("知识库 ：%s（%s），商品 %d 件"
          % (args.store, store.get("store_name"), len(products)))
    print("商品提示：%s" % (args.product or "（无 —— 相当于飞鸽商品卡没读到）"))
    print()

    d = C.classify(text, store, products, args.product)
    print(C.render(d))

    if d["action"] == "need_product":
        print("\n提示：这条消息本身是标准的尺码咨询，之所以走「追问」而不是直接答，")
        print("      是因为知识库里没有「热力台球工作室」在售的服装商品——")
        print("      这正是阶段 3 要先标定飞鸽商品卡的原因。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
