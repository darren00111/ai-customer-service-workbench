# -*- coding: utf-8 -*-
"""Offline P2 dialogue acceptance cases. No merchant browser or live sends."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "feige-fashion-customer-service" / "scripts"))

import classify_standard as standard  # noqa: E402
from merchant import default_manifest  # noqa: E402


def main() -> None:
    store = default_manifest("offline", "离线测试店", 13999)
    business = store["business"]
    business["synonyms"] = {"台球棍": "台球杆", "巧粉": "巧克粉"}
    business["shipping"].update({
        "origin": "广东", "lead_time": "48小时内发货",
        "carriers": ["中通", "圆通"], "delivery_by_region": {"湖南": "2-4天"},
    })
    business["payment"]["methods"] = ["平台支付"]
    business["orders"]["flow"] = "商品页选择规格后提交订单"
    products = [
        {"product_id": "CUE-1", "title": "台球杆", "aliases": ["黑杆"],
         "stock": 8, "price": 199, "attributes": {"长度": "145cm"}},
        {"product_id": "CHALK-1", "title": "巧克粉", "aliases": ["台球粉"],
         "stock": 0, "price": 29, "attributes": {"包装": "盒装"}},
    ]
    cases = [
        ("台球杆有货吗", "reply", "库存"),
        ("黑杆多少钱", "reply", "199"),
        ("台球棍几钱", "reply", "199"),
        ("巧粉有货吗", "reply", "无货"),
        ("台球粉价格", "reply", "29"),
        ("黑杆什么规格", "reply", "145cm"),
        ("哪里发货", "reply", "广东"),
        ("啥时候发", "reply", "48小时"),
        ("哪个快递", "reply", "中通"),
        ("湖南多久到", "reply", "2-4天"),
        ("多久到", "ask_followup", "收货地"),
        ("这个有货吗", "ask_followup", "商品名称"),
        ("台球杆有货吗，发到湖南多久到", "reply", "2-4天"),
        ("黑杆多少钱，还有货吗", "reply", "库存"),
        ("台球杆多少钱，怎么付款", "reply", "平台支付"),
        ("申请退款，台球杆多少钱", "handoff", ""),
        ("台球杆坏了，能换货吗", "handoff", ""),
        ("转人工", "handoff", ""),
        ("台球杆能再便宜点吗", "handoff", ""),
        ("台球棍多少钱", "reply", "199"),
        ("球棍子多少钱", "ask_followup", "商品名称"),
    ]
    failures = []
    for question, expected_action, expected_text in cases:
        result = standard.classify(question, store, products)
        if result["action"] != expected_action or expected_text not in result["draft_reply"]:
            failures.append((question, expected_action, expected_text, result))
    for question, action, text, result in failures:
        print("FAIL", question, "expected", action, text, "got", result)
    print("P2 offline dialogues: %d/%d PASS" % (len(cases) - len(failures), len(cases)))
    if failures:
        raise AssertionError("P2 offline acceptance failed")


if __name__ == "__main__":
    main()
