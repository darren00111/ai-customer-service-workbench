# -*- coding: utf-8 -*-
"""Cross-category customer onboarding and standard-engine smoke checks."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Config, StoreRegistry  # noqa: E402
from app.engine import pipeline  # noqa: E402
from app.engine.store import ConversationStore  # noqa: E402
from merchant import default_manifest, onboarding_errors, read_manifest  # noqa: E402
import classify_standard as standard  # noqa: E402
import yaml  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="feige-merchant-") as temp:
        base = Path(temp)
        stores = base / "knowledge" / "stores"
        products = [
            {"product_id": "CUE-1", "title": "台球杆", "aliases": ["黑杆"],
             "price": 199, "stock": 8, "attributes": {"长度": "145cm"},
             "source": "merchant", "updated_at": "2026-09-23"},
            {"product_id": "LAMP-1", "title": "台灯", "aliases": ["护眼灯"],
             "price": 99, "stock": 5, "attributes": {"功率": "8W"},
             "source": "merchant", "updated_at": "2026-09-23"},
        ]
        for index, (store_id, product) in enumerate(zip(("billiards", "home"), products)):
            merchant = default_manifest(store_id, store_id, 13000 + index)
            merchant["business"]["category"] = "台球用品" if index == 0 else "家居"
            merchant["business"]["shipping"]["origin"] = "广东"
            merchant["business"]["shipping"]["lead_time"] = "48小时内发货"
            merchant["business"]["shipping"]["delivery_by_region"] = {"湖南": "2-4天"}
            merchant["business"]["payment"]["methods"] = ["平台支付"]
            merchant["business"]["orders"]["flow"] = "商品页选择规格后提交订单"
            merchant["business"]["policies"]["authenticity"] = "可在店铺主页查看授权信息"
            merchant["business"]["intents"]["authenticity"] = {
                "keywords": ["是正品吗"], "source": "policies.authenticity"
            }
            merchant["catalog"]["products"] = [product]
            path = stores / store_id / "merchant.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(merchant, allow_unicode=True), encoding="utf-8")
            assert not onboarding_errors(read_manifest(path), [product])

        registry = StoreRegistry(base / "stores.json", manifest_dir=stores)
        specs = registry.load()
        assert len(specs) == 2
        assert specs[0].kb_store_id == "billiards"
        assert specs[0].config_overrides()["auto_send"] is False
        assert len({spec.cdp_port for spec in specs}) == 2
        assert len({spec.config_overrides()["profile_dir"] for spec in specs}) == 2

        original = pipeline.KB.STORES_DIR
        pipeline.KB.STORES_DIR = str(stores)
        try:
            for store_id, product in zip(("billiards", "home"), products):
                store, catalog = pipeline.KB.load_kb(store_id)
                assert store["decision_engine"] == "standard"
                assert len(catalog) == 1 and catalog[0]["product_id"] == product["product_id"]
                price = standard.classify("%s多少钱" % product["title"], store, catalog)
                assert price["action"] == "reply" and str(product["price"]) in price["draft_reply"]
                origin = standard.classify("哪里发货", store, catalog)
                assert origin["action"] == "reply" and "广东" in origin["draft_reply"]
                assert "广东" in standard.classify("是在广东发货吗", store, catalog)["draft_reply"]
                dispatch = standard.classify("明天可以发货吗", store, catalog)
                assert dispatch["category"] == "pre_sale_shipping_time"
                assert "48小时" in dispatch["draft_reply"]
                eta = standard.classify("湖南多久到", store, catalog)
                assert eta["category"] == "pre_sale_delivery_estimate"
                assert "湖南" in eta["draft_reply"] and "2-4天" in eta["draft_reply"]
                followup = standard.classify("多久到", store, catalog)
                assert followup["action"] == "ask_followup"
                assert followup["missing"] == ["destination"]
                followup_answer = standard.classify("湖南", store, catalog,
                                                     pending_intent=followup["pending_intent"])
                assert "2-4天" in followup_answer["draft_reply"]
                product_question = standard.classify("有货吗", store, catalog)
                assert product_question["action"] == "ask_followup"
                product_answer = standard.classify(product["title"], store, catalog,
                                                   pending_intent=product_question["pending_intent"])
                assert product_answer["action"] == "reply" and "库存" in product_answer["draft_reply"]
                payment = standard.classify("怎么付款", store, catalog)
                assert payment["action"] == "reply" and "平台支付" in payment["draft_reply"]
                order = standard.classify("怎么下单", store, catalog)
                assert order["action"] == "reply" and "提交订单" in order["draft_reply"]
                policy = standard.classify("是正品吗", store, catalog)
                assert policy["action"] == "reply" and "授权信息" in policy["draft_reply"]
                handoff = standard.classify("申请退款", store, catalog)
                assert handoff["action"] == "handoff"
                assert standard.classify("多久到", store, catalog,
                                         handoff_pending=True)["category"] == "handoff_context"
                assert standard.classify("能便宜点吗", store, catalog)["action"] == "reply"
                assert standard.classify("买十件能便宜多少", store, catalog)["action"] == "handoff"
                assert "无货" in standard.classify(
                    "%s有货吗" % product["title"], store,
                    [dict(product, stock=0)])["draft_reply"]
                assert "下架" in standard.classify(
                    "%s还卖吗" % product["title"], store,
                    [dict(product, status="off_shelf", stock=None)])["draft_reply"]
                expired = [dict(product, price={"value": 199, "source": "商品中心",
                                                "expires_at": "2020-01-01"})]
                assert standard.classify("%s多少钱" % product["title"], store,
                                         expired)["category"] == "handoff_data_gap"
                missing_region = dict(store, business=dict(store["business"]))
                missing_region["business"]["shipping"] = dict(
                    store["business"]["shipping"], delivery_by_region={"广东": "1天"})
                assert standard.classify("湖南多久到", missing_region, catalog)["action"] == "handoff"
            # Context product must not override an explicitly named different item.
            store, _ = pipeline.KB.load_kb("billiards")
            decision = standard.classify("台灯多少钱", store, products,
                                         context_product="CUE-1")
            assert decision["product_id"] == "LAMP-1"
            store["business"]["synonyms"] = {"黑色球杆": "台球杆"}
            assert "199" in standard.classify("黑色球杆几钱", store, products)["draft_reply"]
            multi = standard.classify("台球杆有货吗，发到湖南多久到", store, products)
            assert multi["action"] == "reply" and multi["category"] == "pre_sale_multi", multi
            assert "库存" in multi["draft_reply"] and "2-4天" in multi["draft_reply"]
            assert len(multi["multi_intents"]) == 2
            mixed_risk = standard.classify("申请退款，台球杆多少钱", store, products)
            assert mixed_risk["action"] == "handoff"
            unknown_product = standard.classify("台球棍多少钱", store, products)
            assert unknown_product["action"] == "ask_followup"
            sample = [dict(products[0], sample_data=True)]
            assert onboarding_errors(read_manifest(stores / "billiards" / "merchant.yaml"), sample)
            assert standard.classify("台球杆多少钱", store, sample)["action"] == "handoff"

            state = ConversationStore(base / "state.json")
            allowed, handoff_fp, _ = state.should_reply("buyer", "申请退款", 1,
                                                        conversation_id="conversation")
            assert allowed
            state.record_handoff("buyer", handoff_fp, "申请退款", conversation_id="conversation")
            assert not state.should_reply("buyer", "申请退款", 1,
                                          conversation_id="conversation")[0]
            assert state.should_reply("buyer", "台球杆多少钱", 2,
                                      conversation_id="conversation")[0]
            state.mark_replied("buyer", "info-fp", "199元", "台球杆多少钱",
                               conversation_id="conversation", product_id="CUE-1")
            state.mark_handled("buyer", handoff_fp, "申请退款",
                               conversation_id="conversation")
            assert state.get("buyer", "conversation")["last_fp"] == "info-fp"
            assert state.get("buyer", "conversation")["last_product_id"] == "CUE-1"
            assert not state.should_reply("buyer", "申请退款", 1,
                                          conversation_id="conversation")[0]
            assert state.summary()["handoff_pending"] == 0
            state.reserve_reply("buyer", "unknown-fp", conversation_id="conversation")
            state.mark_delivery_unknown("buyer", "unknown-fp", "待确认回复",
                                        conversation_id="conversation")
            assert not state.should_reply("buyer", "新的信息问题", 3,
                                          conversation_id="conversation")[0]

            class FakePage:
                def __init__(self):
                    self.message = ""
                    self.seq = 0
                    self.sent = []

                def read(self):
                    return {"buyerName": "buyer", "conversationKey": "live",
                            "latestBuyerMessage": self.message, "latestBuyerKey": "m%d" % self.seq,
                            "buyerMessageCount": self.seq, "latestBuyerTime": "12:00",
                            "lastRole": "buyer", "bubbleCount": self.seq,
                            "messages": [{"role": "buyer", "content": self.message,
                                          "time": "12:00"}]}

                def send_text(self, reply):
                    self.sent.append(reply)
                    return {"ok": True}

            old_logs = pipeline.LOGS_DIR
            old_kb_root = pipeline.STORES_KB_DIR
            pipeline.LOGS_DIR = base / "logs"
            pipeline.STORES_KB_DIR = stores
            try:
                page = FakePage()
                engine = pipeline.ReplyEngine(
                    Config(base / "empty-config.json", overrides={
                        "auto_send": True, "gap_min": 0.0, "gap_max": 0.0}),
                    page, ConversationStore(base / "live-state.json"),
                    store_id="billiards", event_sink=lambda event: {"queued": True},
                )
                for index, (message, action, expected) in enumerate((
                    ("申请退款", "handoff", None),
                    ("台球杆多少钱", "reply", "199"),
                    ("这个还有货吗", "reply", "库存"),
                    ("物流多久到", "ask_followup", "省"),
                    ("湖南", "reply", "2-4天"),
                ), 1):
                    page.message, page.seq = message, index
                    outcome = engine.process_once(ensure_live=False)
                    assert outcome["action"] == action, outcome
                    if expected:
                        assert outcome["sent"] and expected in page.sent[-1], outcome
                assert engine.state.summary()["handoff_pending"] == 1
                assert not engine.state.get("buyer", "live")["paused"]
            finally:
                pipeline.LOGS_DIR = old_logs
                pipeline.STORES_KB_DIR = old_kb_root
        finally:
            pipeline.KB.STORES_DIR = original

    print("merchant onboarding and cross-category routing: PASS")


if __name__ == "__main__":
    main()
