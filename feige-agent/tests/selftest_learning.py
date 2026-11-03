"""Offline acceptance for context, retrieval, incremental synchronization and feedback."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import StoreRegistry
from app.service import KnowledgeStore, ReceptionService
from app.service.context import extract_conditions
from app.service.data import SnapshotDataSource
from app.service.learning import LearningLoop
from app.service.retrieval import search_faq
from app.service.sync import SyncManager, SyncWorker
from app.service.workbench import Workbench


class LearningAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kb = KnowledgeStore(self.root, "shop")
        self.products = [
            {"product_id": "A", "title": "黑杆", "price": 199, "stock": 8, "attributes": {"长度": "145cm"}},
            {"product_id": "B", "title": "白杆", "price": 239, "stock": 4, "attributes": {"长度": "145cm"}}]
        self.business = {"shipping": {"lead_time": "48小时内发货", "delivery_by_region": {"湖南": "揽收后2–4天", "广东": "揽收后1–2天"}}}
        self.source = SnapshotDataSource("shop", self.business, self.products)
        self.service = ReceptionService(self.source, self.kb, as_of=date(2026, 10, 17))

    def publish(self, kind, payload):
        revision = self.kb.stage(kind, payload, "商家核准")
        self.kb.activate(revision)
        return revision

    def workbench(self):
        wb = Workbench(self.root / "reception", self.root / "stores", StoreRegistry(self.root / "registry.json", self.root / "stores"))
        wb.create_store("client", "测试店铺")
        return wb

    def test_relative_and_week_dates(self):
        today = date(2026, 10, 17)
        for text, wanted in (("明天比赛用", "2026-10-18"), ("周五用得上吗", "2026-10-23"),
                             ("下周一用", "2026-10-19"), ("10月20号之前用", "2026-10-20")):
            self.assertEqual(extract_conditions(text, today)["use_date"], wanted)

    def test_invalid_and_past_dates(self):
        for text in ("2月30号要用", "2026年10月1号要用"):
            self.assertTrue(extract_conditions(text, date(2026, 10, 17))["date_needs_confirmation"])

    def test_dispatch_and_transit_are_not_usage_date_or_budget(self):
        first = self.service.respond("湖南多久到，周五要用")
        second = self.service.respond("明天发货吗", first["state"])
        self.assertEqual(second["state"]["use_date"], "2026-10-23")
        self.assertNotIn("budget", extract_conditions("最多两天发货，2到4天到", date.today()))

    def test_budget_range_chinese_and_clear(self):
        self.assertEqual(extract_conditions("两百以内推荐一根", date.today())["budget"], {"max": 200})
        self.assertEqual(extract_conditions("预算100到300元", date.today())["budget"], {"min": 100, "max": 300})
        self.assertTrue(extract_conditions("不限预算", date.today())["clear_budget"])
        self.assertEqual(extract_conditions("预算1.5万以内", date.today())["budget"], {"max": 15000})

    def test_ambiguous_spoken_budget_is_not_misread_as_105(self):
        result = self.service.respond("预算一百五推荐一根")
        self.assertIn("budget", result["missing"])
        self.assertNotIn("budget", result["state"])

    def test_explicit_unlimited_budget_does_not_ask_again(self):
        result = self.service.respond("预算不限，推荐一根")
        self.assertEqual(result["action"], "reply")
        self.assertIn("黑杆", result["draft_reply"])

    def test_recommendation_respects_budget(self):
        result = self.service.respond("两百以内推荐一根")
        self.assertIn("黑杆", result["draft_reply"])
        self.assertNotIn("白杆", result["draft_reply"])
        self.assertEqual(result["state"]["budget"], {"max": 200})

    def test_recommendation_filters_expired_and_out_of_stock(self):
        self.products[0]["stock"] = 0
        self.products[1]["price"] = {"value": 99, "expires_at": "2020-01-01"}
        result = self.service.respond("预算300以内推荐一根")
        self.assertIn("没有找到", result["draft_reply"])
        self.assertNotIn("白杆", result["draft_reply"])

    def test_comparison_uses_both_products(self):
        result = self.service.respond("黑杆和白杆比较一下")
        self.assertIn("199", result["draft_reply"])
        self.assertIn("239", result["draft_reply"])
        self.assertEqual(result["intents"], ["compare"])

    def test_delivery_deadline_impossible(self):
        result = self.service.respond("寄湖南，明天比赛来得及不")
        self.assertIn("赶不上", result["draft_reply"])
        self.assertEqual(result["state"]["use_date"], "2026-10-18")

    def test_delivery_date_correction_and_uncertainty(self):
        first = self.service.respond("湖南多久到，周五用得上吗")
        second = self.service.respond("不是寄湖南，改寄广东，明天就要用", first["state"])
        self.assertEqual(second["state"]["destination"], "广东")
        self.assertIn("2026-10-18", second["draft_reply"])
        self.assertNotIn("保证明天", second["draft_reply"])

    def test_missing_preparation_time_does_not_guess_deadline(self):
        self.business["shipping"].pop("lead_time")
        result = self.service.respond("湖南多久到，周五要用")
        self.assertIn("shipping_window", result["missing"])
        self.assertFalse(result["automatic_resolution"])

    def test_summary_keeps_conditions_beyond_recent_messages(self):
        state = self.service.respond("预算200以内，湖南多久到，周五要用")["state"]
        for _ in range(20):
            state = self.service.respond("谢谢", state)["state"]
        self.assertLessEqual(len(state["messages"]), 24)
        self.assertEqual(state["summary"]["confirmed"]["destination"], "湖南")
        self.assertEqual(state["summary"]["confirmed"]["budget"]["max"], 200)
        self.assertIn("buyer", state["summary"]["references"]["destination"])

    def test_transcript_and_sessions_survive_restart(self):
        wb = self.workbench()
        result = wb.chat("demo_billiards", "黑杆多少钱", "persistent")
        another = Workbench(wb.root, wb.stores_dir, wb.registry)
        result = another.chat("demo_billiards", "这个还有货吗", "persistent")
        self.assertIn("库存8", result["draft_reply"])
        events = another.knowledge("demo_billiards").transcript("persistent")
        self.assertEqual(len(events), 4)
        self.assertTrue(all(e["reference"] for e in events))

    def test_retrieval_candidates_are_not_automatically_answers(self):
        rows = [{"question": "支持刻字服务吗", "answer": "不支持"}]
        candidates = search_faq("能提供刻字服务不", rows)
        self.assertTrue(candidates)
        self.assertFalse(candidates[0]["answerable"])

    def test_merchant_synonyms_allow_equivalent_forms(self):
        rows = [{"question": "可以刻字吗", "answer": "不提供"}]
        result = search_faq("能刻名字吗", rows, synonyms={"可以": "能", "刻名字": "刻字"})
        self.assertTrue(result[0]["answerable"])

    def test_conflicting_faq_does_not_pick_first(self):
        rows = [{"question": "能刻字吗", "answer": "可以"}, {"question": "能刻字吗", "answer": "不可以"}]
        self.assertFalse(any(r["answerable"] for r in search_faq("能刻字吗", rows)))

    def test_faq_scope_stays_with_product(self):
        rows = [{"question": "能刻字吗", "answer": "可以", "product_ids": ["A"]}]
        self.assertEqual(search_faq("能刻字吗", rows, "B"), [])

    def test_incremental_product_patch_preserves_other_fields_and_products(self):
        self.publish("products", self.products)
        version = self.kb.stage_incremental("products", [{"product_id": "A", "stock": 0}], "库存更新")
        self.kb.activate(version)
        rows = self.kb.active("products")["payload"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["price"], 199)
        self.assertEqual(rows[0]["stock"], 0)

    def test_incremental_explicit_deletion(self):
        self.publish("products", self.products)
        version = self.kb.stage_incremental("products", {"delete_ids": ["A", "B"]}, "全部下架")
        self.kb.activate(version)
        self.assertEqual(self.kb.active("products")["payload"], [])

    def test_incremental_conflict_is_rejected(self):
        self.publish("products", self.products)
        one = self.kb.stage_incremental("products", [{"product_id": "A", "stock": 0}], "更新1")
        two = self.kb.stage_incremental("products", [{"product_id": "B", "stock": 0}], "更新2")
        self.kb.activate(one)
        with self.assertRaises(ValueError):
            self.kb.activate(two)

    def test_business_nested_patch_preserves_other_policy(self):
        self.publish("business", self.business)
        version = self.kb.stage_incremental("business", {"shipping": {"origin": "广东"}}, "更新")
        self.kb.activate(version)
        self.assertIn("湖南", self.kb.active("business")["payload"]["shipping"]["delivery_by_region"])

    def test_watched_export_stages_then_updates_changed_content(self):
        self.publish("products", self.products)
        file = self.root / "export.json"
        file.write_text(json.dumps([{"product_id": "A", "stock": 1}]), encoding="utf-8")
        sync = SyncManager(self.kb)
        sync.register(str(file), auto_publish=True)
        self.assertEqual(sync.poll(force=True)[0]["status"], "published")
        self.assertEqual(self.kb.active("products")["payload"][0]["stock"], 1)
        self.assertEqual(sync.poll(force=True)[0]["status"], "unchanged")
        file.write_text(json.dumps([{"product_id": "A", "stock": 2}]), encoding="utf-8")
        sync.poll(force=True)
        self.assertEqual(self.kb.active("products")["payload"][0]["stock"], 2)

    def test_sync_failure_keeps_current_revision_and_reports_error(self):
        current = self.publish("products", self.products)
        file = self.root / "export.json"
        file.write_text("broken", encoding="utf-8")
        sync = SyncManager(self.kb)
        sync.register(str(file), auto_publish=True)
        self.assertEqual(sync.poll(force=True)[0]["status"], "error")
        self.assertEqual(self.kb.active("products")["id"], current)
        self.assertTrue(sync.list()[0]["last_error"])

    def test_history_cannot_auto_publish(self):
        file = self.root / "history.json"
        file.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            SyncManager(self.kb).register(str(file), kind="history", auto_publish=True)

    def test_disabled_source_does_not_sync(self):
        file = self.root / "export.json"
        file.write_text(json.dumps(self.products), encoding="utf-8")
        sync = SyncManager(self.kb)
        rows = sync.register(str(file))
        sync.enable(rows[0]["id"], False)
        self.assertEqual(sync.poll(force=True), [])

    def test_active_sync_lease_prevents_parallel_force(self):
        file = self.root / "export.json"
        file.write_text(json.dumps(self.products), encoding="utf-8")
        sync = SyncManager(self.kb)
        sync.register(str(file))
        with self.kb.connect() as db:
            db.execute("UPDATE sync_sources SET lease_until=99999999999")
        self.assertEqual(sync.poll(force=True), [])

    def test_history_groups_messages_and_keeps_original_positions(self):
        history = [{"product_id": "A", "messages": [{"role": "buyer", "content": "这根杆"},
                    {"role": "buyer", "content": "有刻字服务吗"}, {"role": "agent", "content": "没有刻字服务"},
                    {"role": "agent", "content": "支持标准礼盒"}]}]
        revision = self.kb.learn_history(json.dumps(history), "商家历史导出")
        candidate = self.kb.preview(revision)["payload"][0]
        self.assertIn("这根杆\n有刻字", candidate["question"])
        self.assertIn("没有刻字服务\n支持标准礼盒", candidate["answer"])
        self.assertEqual(candidate["history_reference"]["message_end"], 3)
        self.assertEqual(candidate["product_ids"], ["A"])

    def test_context_dependent_history_without_product_is_not_global_faq(self):
        self.assertEqual(search_faq("这个能用吗", [{"question": "这个能用吗", "answer": "能用", "requires_context": True}]), [])

    def test_feedback_requires_evaluation_then_publishes_without_losing_faq(self):
        wb = self.workbench()
        kb = wb.knowledge("client")
        kb.activate(kb.stage("faq", [{"question": "支持礼盒吗", "answer": "提供标准礼盒"}], "原问答"))
        result = wb.chat("client", "能刻字吗", "c1")
        correction = wb.correct("client", result["interaction_id"], "暂不提供刻字服务")
        version = wb.propose_correction("client", correction["feedback_id"])
        with self.assertRaises(ValueError):
            wb.activate("client", version["id"], True)
        current = kb.active("faq")["id"]
        report = wb.evaluate("client", version["id"])
        self.assertTrue(report["passed"])
        self.assertEqual(kb.active("faq")["id"], current)
        wb.activate("client", version["id"], True)
        self.assertEqual(len(kb.active("faq")["payload"]), 2)
        self.assertIn("不提供刻字", wb.chat("client", "能刻字吗", "c2")["draft_reply"])
        self.assertEqual(kb.tasks(), [])

    def test_feedback_cases_export_and_retirement(self):
        wb = self.workbench()
        result = wb.chat("client", "能刻字吗", "c1")
        feedback = wb.correct("client", result["interaction_id"], "不提供刻字服务")
        loop = LearningLoop(wb.knowledge("client"))
        self.assertEqual(loop.export_cases()["cases"][0]["expected_action"], "reply")
        loop.retire(feedback["feedback_id"])
        self.assertEqual(loop.export_cases()["cases"], [])

    def test_background_worker_polls_registered_sources(self):
        wb = self.workbench()
        file = self.root / "watched.json"
        file.write_text(json.dumps(self.products), encoding="utf-8")
        wb.sync("client").register(str(file), auto_publish=True)
        worker = SyncWorker(wb)
        with patch.object(worker.stop_event, "wait", side_effect=[False, True]):
            worker.start()
            worker.thread.join(timeout=5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(wb.knowledge("client").active("products")["payload"][0]["price"], 199)

    def test_transcript_pagination_does_not_drop_earlier_turns(self):
        state = {}
        for _ in range(55):
            result = self.service.respond("你好", state)
            self.kb.save_turn("long", "你好", state, result)
            state = result["state"]
        recent = self.kb.transcript("long")
        earlier = self.kb.transcript("long", recent[0]["id"])
        self.assertEqual(len(recent), 100)
        self.assertEqual(len(earlier), 10)
        self.assertEqual(len({e["reference"] for e in earlier + recent}), 110)

    def test_volatile_price_correction_is_not_faq(self):
        wb = self.workbench()
        result = wb.chat("demo_billiards", "黑杆多少钱", "c1")
        feedback = wb.correct("demo_billiards", result["interaction_id"], "100元")
        with self.assertRaises(ValueError):
            wb.propose_correction("demo_billiards", feedback["feedback_id"])

    def test_regression_fails_on_wrong_expected_action(self):
        wb = self.workbench()
        result = wb.chat("client", "我要退货", "c1")
        wb.correct("client", result["interaction_id"], "随便自动退", "reply")
        self.assertFalse(wb.evaluate("client")["passed"])

    def test_metrics_and_resolve_gap(self):
        wb = self.workbench()
        wb.chat("client", "一个未知问题", "c1")
        info = wb.info("client")
        self.assertEqual(info["metrics"]["followups"], 1)
        wb.knowledge("client").resolve_gap(info["gaps"][0]["id"])
        self.assertEqual(wb.info("client")["gaps"], [])

    def test_missing_facts_create_specific_tasks(self):
        result = self.service.respond("黑杆会不会震手")
        task = self.kb.tasks()[0]
        self.assertEqual(task["product_id"], "A")
        self.assertIn("attributes.震手", task["missing"])
        self.kb.resolve_task(task["id"])
        self.assertEqual(self.kb.tasks(), [])

    def test_feedback_requires_new_evaluation_when_other_facts_change(self):
        wb = self.workbench()
        result = wb.chat("client", "能刻字吗", "c1")
        feedback = wb.correct("client", result["interaction_id"], "暂不刻字")
        revision = wb.propose_correction("client", feedback["feedback_id"])
        self.assertTrue(wb.evaluate("client", revision["id"])["passed"])
        kb = wb.knowledge("client")
        kb.activate(kb.stage("business", {"category": "用品"}, "政策变更"))
        with self.assertRaises(ValueError):
            wb.activate("client", revision["id"], True)

    def test_regression_date_is_fixed_to_original_interaction(self):
        wb = self.workbench()
        with patch("app.service.dialogue.store_today", return_value=date(2026, 10, 17)):
            result = wb.chat("demo_billiards", "湖南明天比赛来得及不", "c1")
        feedback = wb.correct("demo_billiards", result["interaction_id"], "2026-10-18")
        with patch("app.service.dialogue.store_today", return_value=date(2027, 1, 1)):
            report = wb.evaluate("demo_billiards")
        self.assertTrue(report["passed"])

    def test_old_regression_retains_original_active_product_context(self):
        wb = self.workbench()
        wb.chat("demo_billiards", "黑杆多少钱", "c1")
        result = wb.chat("demo_billiards", "能刻字吗", "c1")
        feedback = wb.correct("demo_billiards", result["interaction_id"], "本款暂不刻字")
        revision = wb.propose_correction("demo_billiards", feedback["feedback_id"])
        with patch("app.service.dialogue.time.time", return_value=result["state"]["last_activity"] + 7200):
            self.assertTrue(wb.evaluate("demo_billiards", revision["id"])["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
