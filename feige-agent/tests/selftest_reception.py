"""Offline acceptance: grounding, conversation state, tenancy, learning, and failures."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import StoreRegistry, StoreSpec
from app.service import KnowledgeStore, ReceptionService
from app.service.contracts import Understanding
from app.service.data import SnapshotDataSource
from app.service.workbench import Workbench, FIXTURES


class ScriptedInterpreter:
    name = "scripted_contract_test"

    def __init__(self, value):
        self.value = value
        self.requests = []

    def understand(self, request):
        self.requests.append(request)
        return self.value


class ReceptionAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kb = KnowledgeStore(self.root, "merchant")
        fixture = json.loads(FIXTURES.read_text(encoding="utf-8"))["demo_billiards"]
        self.products = copy.deepcopy(fixture["products"])
        for p in self.products:
            p.pop("sample_data", None)
            p["source"] = "confirmed_fixture_for_test_only"
        self.source = SnapshotDataSource("merchant", fixture["business"], self.products, fixture["orders"])
        self.service = ReceptionService(self.source, self.kb)

    def test_colloquial_multi_question(self):
        result = self.service.respond("黑棍几钱，还有货吗")
        self.assertIn("199", result["draft_reply"])
        self.assertIn("库存8", result["draft_reply"])
        self.assertEqual(result["mode"], "offline_rules")
        self.assertFalse(result["model_connected"])

    def test_destination_followup(self):
        first = self.service.respond("啥时候到")
        self.assertEqual(first["missing"], ["destination"])
        second = self.service.respond("长沙", first["state"])
        self.assertIn("2–4天", second["draft_reply"])
        self.assertEqual(second["state"]["destination"], "湖南")

    def test_destination_correction(self):
        first = self.service.respond("湖南多久到")
        second = self.service.respond("不是寄湖南，改寄广东，啥时候到", first["state"])
        self.assertIn("1–2天", second["draft_reply"])
        third = self.service.respond("不是寄广东，改寄湖南，啥时候到", second["state"])
        self.assertIn("2–4天", third["draft_reply"])

    def test_negated_intent(self):
        result = self.service.respond("不是问哪里发货，我问什么时候到")
        self.assertEqual(result["intents"], ["delivery"])
        self.assertNotIn("发货地", result["draft_reply"])

    def test_product_reference_and_previous(self):
        one = self.service.respond("黑杆多少钱")
        two = self.service.respond("白杆多少钱", one["state"])
        three = self.service.respond("前一个还有货吗", two["state"])
        self.assertEqual(three["product_id"], "CUE-B")
        self.assertIn("库存8", three["draft_reply"])

    def test_unresolved_switch_clears_product(self):
        one = self.service.respond("黑杆多少钱")
        two = self.service.respond("换款，有货吗", one["state"])
        self.assertIsNone(two["product_id"])
        self.assertIn("product", two["missing"])

    def test_policy_is_not_after_sale_action(self):
        result = self.service.respond("还没买，想了解退货规则")
        self.assertEqual(result["action"], "reply")
        self.assertIn("七天", result["draft_reply"])

    def test_negated_after_sale_action(self):
        result = self.service.respond("不是要退货，我问黑杆价格")
        self.assertEqual(result["action"], "reply")
        self.assertIn("199", result["draft_reply"])

    def test_invalid_policy_import_is_rejected(self):
        with self.assertRaises(ValueError):
            self.kb.stage("business", {"shipping": "48小时"}, "商家导入")

    def test_after_sale_stays_with_human(self):
        first = self.service.respond("收到就坏了，我要退货")
        self.assertEqual(first["action"], "handoff")
        self.assertEqual(self.service.respond("啥时候到", first["state"])["action"], "handoff")

    def test_human_request(self):
        self.assertEqual(self.service.respond("找真人")["category"], "handoff_human_request")

    def test_bargain_uses_approved_promotion(self):
        result = self.service.respond("买十根再便宜点")
        self.assertEqual(result["action"], "reply")
        self.assertIn("满299", result["draft_reply"])

    def test_no_invented_performance(self):
        first = self.service.respond("黑杆多少钱")
        result = self.service.respond("这根打着会不会震手", first["state"])
        self.assertEqual(result["action"], "ask_followup")
        self.assertFalse(result["automatic_resolution"])
        self.assertIn("attributes.震手", result["missing"])

    def test_partial_answers_keep_known_facts(self):
        result = self.service.respond("黑杆多少钱，还有货吗，哪里发货，啥时候到")
        self.assertIn("199", result["draft_reply"])
        self.assertIn("库存8", result["draft_reply"])
        self.assertIn("广东", result["draft_reply"])
        self.assertEqual(result["pending_intents"], ["delivery"])

    def test_expired_price(self):
        self.products[0]["price"] = {"value": 1, "expires_at": "2020-01-01"}
        result = self.service.respond("黑杆多少钱")
        self.assertEqual(result["action"], "ask_followup")
        self.assertNotIn("标价1元", result["draft_reply"])

    def test_expired_attribute(self):
        self.products[0]["attributes"]["长度"] = {"value": "999cm", "expires_at": "2020-01-01"}
        result = self.service.respond("黑杆长度")
        self.assertNotIn("999", result["draft_reply"])
        self.assertIn("attributes.长度", result["missing"])

    def test_invalid_numeric_fact(self):
        for value in (True, -1, float("inf"), "便宜"):
            self.products[0]["price"] = value
            self.assertEqual(self.service.respond("黑杆多少钱")["action"], "ask_followup")

    def test_sku_does_not_use_product_total(self):
        self.products[0]["variants"] = [{"sku_id": "M-B", "color": "黑色", "size": "M", "stock": 0}]
        result = self.service.respond("黑杆黑色M码有货吗")
        self.assertIn("无货", result["draft_reply"])
        self.assertNotIn("库存8", result["draft_reply"])

    def test_missing_sku(self):
        result = self.service.respond("黑杆M码有货吗")
        self.assertIn("variant.stock", result["missing"])

    def test_single_color_product_uses_its_own_price_with_identity_evidence(self):
        self.products[0]['title']='黑色台球杆'
        self.products[0].pop('variants',None)
        result=self.service.respond('黑色台球杆多少钱')
        self.assertEqual(result['action'],'reply')
        self.assertIn('199',result['draft_reply'])
        self.assertTrue(any(row['path'].endswith('.title') for row in result['evidence']))

    def test_unknown_or_ambiguous_color_still_needs_sku_facts(self):
        self.products[0]['title']='黑色台球杆'
        self.products[0].pop('variants',None)
        result=self.service.respond('黑杆红色多少钱')
        self.assertIn('variant.price',result['missing'])
        self.products[0]['title']='黑色／白色台球杆'
        result=self.service.respond('黑杆黑色多少钱')
        self.assertIn('variant.price',result['missing'])
        self.products[0]['title']='黑色台球杆'
        self.products[0]['attributes']['颜色']={'value':'黑色','expires_at':'2020-01-01'}
        result=self.service.respond('黑杆黑色多少钱')
        self.assertIn('variant.price',result['missing'])

    def test_unknown_never_hallucinates(self):
        result = self.service.respond("阿拉周末就要用，侬看成伐")
        self.assertEqual(result["action"], "ask_followup")
        self.assertFalse(result["automatic_resolution"])

    def test_model_contract_full_history(self):
        model = ScriptedInterpreter({"intents": ["delivery"], "destination": "湖南", "product_id": "CUE-B"})
        service = ReceptionService(self.source, self.kb, model)
        history = [{"role": "buyer", "content": "黑杆"}, {"role": "agent", "content": "寄到哪里？"}]
        result = service.respond("长沙，明天来得及不", messages=history, live=True)
        self.assertEqual(result["mode"], model.name)
        self.assertEqual(model.requests[0]["messages"][1]["role"], "agent")
        self.assertIn("2–4天", result["draft_reply"])
        self.assertNotIn("保证", result["draft_reply"])

    def test_model_cannot_supply_answer_or_foreign_product(self):
        for value in ({"intents": ["price"], "product_id": "OTHER"},
                      {"intents": ["price"], "draft_reply": "免费送"},
                      {"intents": ["delete_orders"]}, {"intents": "price"},
                      {"intents": ["price"], "product_id": []}):
            service = ReceptionService(self.source, self.kb, ScriptedInterpreter(value))
            result = service.respond("多少钱", live=True)
            self.assertTrue(result["model_error"])
            self.assertFalse(result["can_send"])

    def test_model_clarification_is_respected(self):
        model = ScriptedInterpreter({"intents": ["price"], "product_id": "CUE-B", "needs_clarification": True})
        result = ReceptionService(self.source, self.kb, model).respond("那款多少钱")
        self.assertFalse(result["automatic_resolution"])
        self.assertNotIn("199", result["draft_reply"])

    def test_model_cannot_bypass_after_sale(self):
        model = ScriptedInterpreter({"intents": ["greeting"]})
        result = ReceptionService(self.source, self.kb, model).respond("我要退货")
        self.assertEqual(result["action"], "handoff")

    def test_order_requires_independent_authorization(self):
        model = ScriptedInterpreter({"intents": ["delivery"], "order_id": "DEMO-ORDER-1"})
        service = ReceptionService(self.source, self.kb, model)
        denied = service.respond("查这个订单")
        self.assertIn("order", denied["missing"])
        allowed = service.respond("查这个订单", authorized_order_ids=["DEMO-ORDER-1"])
        self.assertIn("承运商预计", allowed["draft_reply"])

    def test_data_query_failure_does_not_invent_or_crash(self):
        model = ScriptedInterpreter({"intents": ["order_status"], "order_id": "DEMO-ORDER-1"})
        service = ReceptionService(self.source, self.kb, model)
        with patch.object(self.source, "order", side_effect=TimeoutError("private transport detail")):
            result = service.respond("查订单", authorized_order_ids=["DEMO-ORDER-1"])
        self.assertIn("data_source_unavailable", result["missing"])
        self.assertNotIn("private transport", json.dumps(result))
        self.assertFalse(result["automatic_resolution"])

    def test_no_model_no_live_auto_send(self):
        self.assertFalse(self.service.respond("黑杆多少钱", live=True)["can_send"])

    def test_demo_even_with_model_cannot_send_live(self):
        self.source.demo = True
        service = ReceptionService(self.source, self.kb, ScriptedInterpreter({"intents": ["price"], "product_id": "CUE-B"}))
        self.assertFalse(service.respond("几钱", live=True)["can_send"])

    def test_tenant_state_and_source_isolation(self):
        with self.assertRaises(ValueError):
            self.service.respond("多少钱", {"store_id": "another"})
        with self.assertRaises(ValueError):
            ReceptionService(self.source, KnowledgeStore(self.root, "another"))
        with self.assertRaises(ValueError):
            KnowledgeStore(self.root, "../merchant")

    def test_staging_activation_rollback(self):
        v1 = self.kb.stage("products", self.products, "商家确认")
        self.assertIsNone(self.kb.active("products"))
        self.kb.activate(v1)
        changed = copy.deepcopy(self.products)
        changed[0]["price"] = 299
        v2 = self.kb.stage("products", changed, "价格更新")
        self.kb.activate(v2)
        self.assertEqual(self.kb.active("products")["payload"][0]["price"], 299)
        self.kb.activate(v1)
        self.assertEqual(self.kb.active("products")["payload"][0]["price"], 199)
        self.assertEqual(sum(r["active"] for r in self.kb.revisions()), 1)

    def test_demo_activation_refused(self):
        version = self.kb.stage("products", self.products, "模拟", demo=True)
        with self.assertRaises(ValueError):
            self.kb.activate(version)

    def test_csv_import(self):
        revision = self.kb.import_products("product_id,title,price,stock,aliases\nA,小灯,99,0,灯|台灯", "csv", "商家表格")
        row = self.kb.preview(revision)["payload"][0]
        self.assertEqual(row["aliases"], ["灯", "台灯"])
        self.assertEqual(row["stock"], 0)

    def test_invalid_import_keeps_active(self):
        self.kb.activate(self.kb.stage("products", self.products, "初版"))
        for payload in ([self.products[0], self.products[0]], [{"product_id": "A"}], "bad"):
            with self.assertRaises(ValueError):
                self.kb.stage("products", payload, "更新")
        self.assertEqual(len(self.kb.active("products")["payload"]), 3)

    def test_history_is_redacted_and_requires_review(self):
        history = [{"messages": [{"role": "buyer", "content": "手机号13812345678，地址：XX路88号"},
                                  {"role": "agent", "content": "联系a@example.com，编号123456789123456"}]}]
        revision = self.kb.learn_history(json.dumps(history), "历史导出")
        preview = json.dumps(self.kb.preview(revision), ensure_ascii=False)
        for value in ("13812345678", "XX路", "a@example.com", "123456789123456"):
            self.assertNotIn(value, preview)
        self.assertIsNone(self.kb.active("faq"))
        with self.assertRaises(ValueError):
            self.kb.activate(revision)
        self.kb.activate(revision, reviewed=True)

    def test_faq_update_changes_answer_and_expires(self):
        revision = self.kb.stage("faq", [{"question": "能刻字吗", "answer": "此款不提供刻字服务"}], "商家核准")
        self.kb.activate(revision)
        self.assertIn("不提供刻字", self.service.respond("能刻字吗？")["draft_reply"])
        revision = self.kb.stage("faq", [{"question": "能刻字吗", "answer": "免费刻字", "expires_at": "2020-01-01"}], "旧活动")
        self.kb.activate(revision)
        self.assertNotIn("免费刻字", self.service.respond("能刻字吗？")["draft_reply"])

    def test_gaps_deduplicate_without_phone(self):
        for _ in range(2):
            self.service.respond("联系13812345678帮我看看")
        self.assertEqual(self.kb.gaps()[0]["occurrences"], 2)
        self.assertNotIn("13812345678", self.kb.gaps()[0]["question"])

    def test_preview_does_not_learn(self):
        self.service.respond("未收录的新问题", track_gaps=False)
        self.assertEqual(self.kb.gaps(), [])

    def test_workbench_demo_and_onboarding(self):
        registry = StoreRegistry(self.root / "registry.json", self.root / "stores")
        wb = Workbench(self.root / "reception", self.root / "stores", registry)
        self.assertEqual(len(wb.stores()), 0)  # Community lists only actual connected shops.
        self.assertTrue(wb.info("demo_billiards")["demo"])
        one = wb.chat("demo_billiards", "黑杆多少钱")
        self.assertIn("199", one["draft_reply"])
        self.assertIn("库存8", wb.chat("demo_billiards", "这个还有货吗")["draft_reply"])
        info = wb.create_store("client", "客户店铺")
        self.assertEqual(info["products"], 0)
        self.assertFalse(info["automatic_sending"])
        revision = wb.stage("client", "products", json.dumps(self.products), "客户导入")
        self.assertEqual(wb.info("client")["products"], 0)
        wb.activate("client", revision["id"], reviewed=True)
        self.assertIn("199", wb.chat("client", "黑杆多少钱")["draft_reply"])
        with self.assertRaises(ValueError):
            wb.create_store("demo_reserved", "非法")

    def test_new_specs_default_to_own_knowledge(self):
        self.assertEqual(StoreSpec("client", "店铺", 12345, "profile").kb_store_id, "client")

    def test_expired_context_does_not_reuse_old_product(self):
        first = self.service.respond("黑杆多少钱")
        with patch("app.service.dialogue.time.time", return_value=first["state"]["last_activity"] + 7200):
            result = self.service.respond("这个多少钱", first["state"])
        self.assertIsNone(result["product_id"])
        self.assertIn("product", result["missing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
