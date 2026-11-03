"""Exercise the new reception through the actual send/state/notification pipeline."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Config
from app.engine import pipeline
from app.engine.store import ConversationStore
from app.service import KnowledgeStore
from merchant import default_manifest
import yaml


class Page:
    def __init__(self):
        self.messages = []
        self.seq = 0
        self.sent = []
        self.send_ok = True

    def buyer(self, text):
        self.seq += 1
        self.messages.append({"role": "buyer", "content": text, "time": str(self.seq)})

    def read(self):
        buyer = next((m for m in reversed(self.messages) if m["role"] == "buyer"), {})
        return {"buyerName": "顾客", "conversationKey": "c1", "latestBuyerMessage": buyer.get("content"),
                "latestBuyerKey": "m%d" % self.seq, "buyerMessageCount": self.seq,
                "latestBuyerTime": buyer.get("time"), "lastRole": self.messages[-1]["role"],
                "messages": list(self.messages), "bubbleCount": len(self.messages)}

    def send_text(self, text):
        if not self.send_ok:
            return {"ok": False, "safe_to_retry": True, "reason": "test rejected"}
        self.sent.append(text)
        self.messages.append({"role": "agent", "content": text})
        return {"ok": True}


class Model:
    name = "scripted_pipeline_contract"

    def __init__(self):
        self.requests = []

    def understand(self, request):
        self.requests.append(request)
        text = request["latest"]
        if "多少钱" in text:
            return {"intents": ["price"], "product_id": "A"}
        if "到" in text:
            return {"intents": ["delivery"]}
        if "广东" in text:
            return {"intents": ["delivery"], "destination": "广东"}
        return {"intents": ["unknown"]}


class PipelineAcceptance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stores = self.root / "stores"
        manifest = default_manifest("client", "测试店铺", 13001)
        manifest["business"].update({"category": "用品", "reception": {"enabled": True}})
        manifest["business"]["shipping"]["delivery_by_region"] = {"广东": "揽收后1–2天"}
        manifest["catalog"]["products"] = [{"product_id": "A", "title": "测试台灯", "price": 99,
                                              "stock": 5, "attributes": {"规格": "8W"}, "source": "测试构造资料"}]
        target = self.stores / "client" / "merchant.yaml"
        target.parent.mkdir(parents=True)
        target.write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
        self.reception_root = self.root / "reception"
        for name, value in (("STORES_KB_DIR", self.stores), ("RECEPTION_DIR", self.reception_root), ("LOGS_DIR", self.root / "logs")):
            patcher = patch.object(pipeline, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(pipeline.KB, "STORES_DIR", str(self.stores))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.page, self.model, self.events = Page(), Model(), []
        self.state_path = self.root / "state.json"
        self.state = ConversationStore(self.state_path)
        self.cfg = Config(self.root / "config.json", overrides={"auto_send": True, "gap_min": 0, "gap_max": 0})
        self.engine = pipeline.ReplyEngine(self.cfg, self.page, self.state, "client", interpreter=self.model,
                                           event_sink=lambda event: self.events.append(event) or {"queued": True})

    def test_send_confirmed_and_context_survives_restart(self):
        self.page.buyer("测试台灯多少钱")
        self.assertTrue(self.engine.process_once(ensure_live=False)["sent"])
        reloaded = ConversationStore(self.state_path)
        self.assertEqual(reloaded.get("顾客", "c1")["dialogue"]["product_id"], "A")
        self.assertEqual(reloaded.summary()["total_replies"], 1)

    def test_followup_state(self):
        self.page.buyer("啥时候到")
        one = self.engine.process_once(ensure_live=False)
        self.assertTrue(one["sent"])
        self.page.buyer("广东")
        two = self.engine.process_once(ensure_live=False)
        self.assertIn("1–2天", two["draft"])
        self.assertTrue(two["sent"])

    def test_adjacent_buyer_messages_are_not_lost(self):
        self.page.buyer("测试台灯")
        self.page.buyer("多少钱")
        self.engine.process_once(ensure_live=False)
        self.assertIn("测试台灯", self.model.requests[-1]["latest"])

    def test_dry_run_has_no_handoff_or_state_side_effects(self):
        self.page.buyer("我要退货")
        before = json.dumps(self.state.data, sort_keys=True)
        result = self.engine.process_once(dry_run=True, ensure_live=False)
        self.assertEqual(result["action"], "handoff")
        self.assertEqual(self.events, [])
        # get() creates a default in-memory row, but does not mutate durable state.
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.state.get("顾客", "c1").get("paused"))
        self.assertEqual(self.page.sent, [])

    def test_auto_send_false_is_enforced_at_single_round_entry(self):
        self.cfg.data["auto_send"] = False
        self.page.buyer("多少钱")
        result = self.engine.process_once(ensure_live=False)
        self.assertFalse(result["sent"])
        self.assertEqual(self.page.sent, [])
        self.assertIn("关闭", result["skip"])

    def test_model_missing_blocks_live_sending(self):
        self.engine.interpreter = None
        self.page.buyer("测试台灯多少钱")
        result = self.engine.process_once(ensure_live=False)
        self.assertFalse(result["sent"])
        self.assertFalse(result["can_send"])
        self.assertEqual(self.page.sent, [])

    def test_active_catalog_changes_next_answer_without_restart(self):
        kb = KnowledgeStore(self.reception_root / "shops", "client")
        products = [{"product_id": "A", "title": "测试台灯", "price": 299}]
        revision = kb.stage("products", products, "商家更新")
        kb.activate(revision)
        self.page.buyer("测试台灯多少钱")
        result = self.engine.process_once(ensure_live=False)
        self.assertIn("299", result["draft"])
        self.assertEqual(result["evidence"][0]["revision"], revision)
        self.assertEqual(result["evidence"][0]["source"], "商家更新")

    def test_demo_catalog_is_blocked_even_with_model(self):
        kb = KnowledgeStore(self.reception_root / "shops", "client")
        revision = kb.stage("products", [{"product_id": "A", "title": "测试台灯", "price": 1}], "模拟", demo=True)
        kb.activate(revision, allow_demo=True)
        self.page.buyer("测试台灯多少钱")
        self.assertFalse(self.engine.process_once(ensure_live=False)["sent"])

    def test_failed_send_does_not_commit_proposed_dialogue(self):
        self.page.send_ok = False
        self.page.buyer("测试台灯多少钱")
        self.assertFalse(self.engine.process_once(ensure_live=False)["sent"])
        self.assertNotIn("dialogue", self.state.get("顾客", "c1"))

    def test_after_sale_handoff_persists_and_manual_resolution_clears(self):
        self.page.buyer("我要退货")
        result = self.engine.process_once(ensure_live=False)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.page.sent, [])
        self.assertTrue(self.state.get("顾客", "c1")["dialogue"]["after_sale_open"])
        self.state.mark_handled("顾客", result["fingerprint"], "我要退货", conversation_id="c1")
        self.assertNotIn("after_sale_open", self.state.get("顾客", "c1")["dialogue"])

    def test_cross_store_knowledge_mapping_rejected(self):
        with self.assertRaises(ValueError):
            pipeline.ReplyEngine(self.cfg, self.page, self.state, "client", runtime_store_id="another")

    def test_new_buyer_message_during_understanding_cancels_old_draft(self):
        original = self.model.understand
        def delayed(request):
            response = original(request)
            self.page.buyer("等等，我要退货")
            return response
        self.model.understand = delayed
        self.page.buyer("测试台灯多少钱")
        result = self.engine.process_once(ensure_live=False)
        self.assertFalse(result["sent"])
        self.assertIn("会话已变化", result["skip"])
        self.assertEqual(self.page.sent, [])

    def test_knowledge_changed_during_understanding_cancels_old_draft(self):
        original = self.model.understand
        def delayed(request):
            response = original(request)
            kb = KnowledgeStore(self.reception_root / "shops", "client")
            kb.activate(kb.stage("products", [{"product_id": "A", "title": "测试台灯", "price": 299}], "新价格"))
            return response
        self.model.understand = delayed
        self.page.buyer("测试台灯多少钱")
        result = self.engine.process_once(ensure_live=False)
        self.assertFalse(result["sent"])
        self.assertIn("资料已更新", result["skip"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
