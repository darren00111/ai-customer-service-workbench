"""P0 acceptance: 漏答—补齐—测试—发布—再回答, with rollback, overrides and isolation.

Constructed stores only. Nothing here talks to a real platform or customer.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import StoreRegistry
from app.service.gapcards import GapBoard
from app.service.workbench import Workbench

PRODUCTS = [
    {"product_id": "A1", "title": "黑杆", "aliases": ["黑色这款"], "price": 199, "stock": 5, "attributes": {"长度": "145cm"}},
    {"product_id": "B1", "title": "白杆", "price": 239, "stock": 3, "attributes": {"长度": "147cm"}},
]
BUSINESS = {"shipping": {"lead_time": "48小时内发货", "delivery_by_region": {"广东": "揽收后1–2天", "湖南": "揽收后2–4天"}}}


class P0Loop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.wb = Workbench(root / "r", root / "s", StoreRegistry(root / "reg.json", root / "s"))
        self.addCleanup(self.wb.close_connections)
        for store in ("shop_a", "shop_b"):
            self.wb.create_store(store, store + "店")
            self.publish(store, "products", PRODUCTS)
            self.publish(store, "business", BUSINESS)

    def publish(self, store, kind, payload):
        revision = self.wb.stage(store, kind, json.dumps(payload, ensure_ascii=False), "商家核准资料")["id"]
        self.wb.activate(store, revision, True)
        return revision

    def cards(self, store="shop_a"):
        return self.wb.cards(store)["cards"]

    def material_card(self):
        return next(c for c in self.cards() if c["fields"] == ["attributes.材质"])

    def test_full_loop_gap_supplement_retest_publish_reanswer(self):
        first = self.wb.chat("shop_a", "黑杆什么材质", session="buyer-1")
        # Ordinary inquiry missing store data: answered honestly, not handed to a human.
        self.assertEqual(first["action"], "ask_followup")
        self.assertIn("材质", first["draft_reply"])
        card = self.material_card()
        self.assertEqual((card["card_type"], card["status"], card["product_id"]), ("knowledge_missing", "open", "A1"))
        self.assertIn("supplement", card["allowed"])
        self.wb.chat("shop_a", "黑杆的材质是什么", session="buyer-2")
        self.assertEqual(self.material_card()["occurrences"], 2, "same product + field aggregates across phrasings")
        self.assertEqual(len([c for c in self.cards() if c["fields"] == ["attributes.材质"]]), 1)

        filled = self.wb.card_action("shop_a", card["id"], "supplement", {
            "kind": "products", "source": "商家补充",
            "content": json.dumps({"upserts": [{"product_id": "A1", "attributes": {"长度": "145cm", "材质": "枫木"}}]}, ensure_ascii=False)})
        self.assertEqual(filled["status"], "retest", "uploading data alone is not completion")
        self.assertEqual(self.wb.cards("shop_a")["summary"]["done"], 0)
        with self.assertRaisesRegex(ValueError, "尚未通过复测"):
            self.wb.activate("shop_a", filled["revision_id"], True)
        # Not published yet: live answer is still unknown.
        self.assertIn("attributes.材质", self.wb.chat("shop_a", "黑杆什么材质", session="buyer-3")["missing"])

        failed = self.wb.card_action("shop_a", card["id"], "retest", {"variants": [
            {"question": "白杆什么材质", "product_id": "B1", "expect": "not_answered", "must_not_contain": "枫木"},
            {"question": "黑杆什么材质", "expect": "answered", "must_contain": "不存在的词"}]})
        self.assertFalse(failed["report"]["passed"])
        self.assertEqual(failed["status"], "retest")
        passed = self.wb.card_action("shop_a", card["id"], "retest", {"variants": [
            {"question": "白杆什么材质", "product_id": "B1", "expect": "not_answered", "must_not_contain": "枫木"}]})
        self.assertTrue(passed["report"]["passed"], passed["report"])
        self.assertEqual(passed["status"], "publish")
        self.assertIn("边界复测", {c["origin"] for c in passed["report"]["cases"]})

        self.wb.activate("shop_a", passed["revision_id"], True)
        self.assertEqual(self.wb.card("shop_a", card["id"])["status"], "observing")
        # A later turn of an original session is not a new-session observation.
        self.wb.chat("shop_a", "黑杆什么材质", session="buyer-1")
        self.assertEqual(self.wb.card("shop_a", card["id"])["status"], "observing")
        again = self.wb.chat("shop_a", "黑杆什么材质", session="buyer-9")
        self.assertEqual(again["action"], "reply")
        self.assertIn("枫木", again["draft_reply"])
        done = self.wb.card("shop_a", card["id"])
        self.assertEqual(done["status"], "observing")
        self.assertFalse(done["observation"]["real_reception"], "simulation must not be reported as real effect")
        self.assertIn("尚非真实接待效果", done["note"])

        # Cross-store isolation: shop B neither sees the card nor the knowledge.
        self.assertEqual(self.cards("shop_b"), [])
        self.assertIn("attributes.材质", self.wb.chat("shop_b", "黑杆什么材质", session="buyer-1")["missing"])

    def test_rollback_is_audited_and_reopens_card(self):
        self.wb.chat("shop_a", "黑杆什么材质", session="s1")
        card = self.material_card()
        rev = self.wb.card_action("shop_a", card["id"], "supplement", {"kind": "products", "content": {"upserts": [
            {"product_id": "A1", "attributes": {"长度": "145cm", "材质": "枫木"}}]}})["revision_id"]
        self.wb.card_action("shop_a", card["id"], "retest", {})
        self.wb.activate("shop_a", rev, True)
        self.assertIn("枫木", self.wb.chat("shop_a", "黑杆什么材质", session="s2")["draft_reply"])
        with self.assertRaisesRegex(ValueError, "原因"):
            self.wb.rollback("shop_a", "products", "")
        result = self.wb.rollback("shop_a", "products", "补充的材质有误，先恢复上一版")
        self.assertLess(result["restored"], rev)
        self.assertIn(card["id"], result["reopened_cards"])
        latest = result["history"][0]
        self.assertEqual((latest["action"], latest["from_revision"], latest["to_revision"]), ("rollback", rev, result["restored"]))
        self.assertIn("attributes.材质", self.wb.chat("shop_a", "黑杆什么材质", session="s3")["missing"])
        self.assertIn(self.wb.card("shop_a", card["id"])["status"], {"open", "assigned"})
        with self.assertRaisesRegex(ValueError, "曾经生效"):
            unpublished = self.wb.stage("shop_a", "faq", json.dumps([{"question": "q", "answer": "a"}]), "x")["id"]
            self.wb.knowledge("shop_a").activate(unpublished, rollback=True, reason="x")

    def test_stale_draft_after_publish_and_override(self):
        spec_store = "shop_a"
        self.wb.import_conversations(spec_store, json.dumps({"store_id": spec_store, "conversations": [
            {"conversation_id": "c1", "messages": [{"message_id": "m1", "role": "buyer", "content": "黑杆什么材质"}]}]}, ensure_ascii=False))
        draft = self.wb.generate_imported_draft(spec_store, "c1")
        self.assertFalse(draft["sent"])
        self.assertTrue(self.wb.imported_conversation(spec_store, "c1")["draft"])
        self.publish(spec_store, "faq", [{"question": "能刻字吗", "answer": "本店暂不提供刻字服务"}])
        thread = self.wb.imported_conversation(spec_store, "c1")
        self.assertIsNone(thread["draft"])
        self.assertTrue(thread["draft_stale"], "knowledge change invalidates the old draft")

        draft = self.wb.generate_imported_draft(spec_store, "c1")
        before = self.wb.inbox.get(self.wb.spec(spec_store), "c1")["context_revision"]
        record = self.wb.override(spec_store, draft["interaction_id"], {"action": "handoff", "answer": "我来人工核对材质"},
                                  "knowledge_missing", "session_only", conversation_id="c1")
        self.assertFalse(record["sent"])
        thread = self.wb.inbox.get(self.wb.spec(spec_store), "c1")
        self.assertIsNone(thread["draft"])
        self.assertTrue(thread["paused"])
        self.assertEqual(thread["context_revision"], before + 1)
        # A generation that started before the override cannot be saved.
        stale = dict(draft, context_revision=before)
        with self.assertRaises(ValueError):
            self.wb.inbox.save_draft(self.wb.spec(spec_store), "c1", thread["messages"][-1]["sequence"], stale)

    def test_override_scopes_and_session_correction(self):
        first = self.wb.chat("shop_a", "黑杆寄湖南多久", session="o1")
        self.assertEqual(first["state"]["destination"], "湖南")
        cards_before = len(self.cards())
        with self.assertRaisesRegex(ValueError, "特殊优惠"):
            self.wb.override("shop_a", first["interaction_id"], {"answer": "给您便宜20元"}, "wording", "store_candidate", special_offer=True)
        only = self.wb.override("shop_a", first["interaction_id"], {"answer": "给您便宜20元"}, "wording", "session_only", special_offer=True)
        self.assertIsNone(only["feedback_id"])
        self.assertEqual(len(self.cards()), cards_before, "session-only override files no knowledge")
        moved = self.wb.override("shop_a", first["interaction_id"], {"product_id": "B1"}, "misunderstood", "session_only")
        self.assertEqual(moved["changes"], {"product_id": "B1"})
        follow = self.wb.chat("shop_a", "多少钱", session="o1")
        self.assertIn("白杆", follow["draft_reply"])
        self.assertEqual(follow["state"]["destination"], "湖南", "other confirmed conditions survive the correction")

        long_term = self.wb.override("shop_a", first["interaction_id"], {"answer": "揽收后2–4天送达湖南", "action": "reply"},
                                     "misunderstood", "store_candidate")
        self.assertTrue(long_term["feedback_id"])
        card = self.wb.card("shop_a", long_term["card_id"])
        self.assertEqual(card["card_type"], "semantic")
        self.assertNotIn("supplement", card["allowed"])
        with self.assertRaisesRegex(ValueError, "不能靠新增标准答案"):
            self.wb.card_action("shop_a", card["id"], "supplement", {"kind": "faq", "content": []})
        dev = self.wb.card_action("shop_a", card["id"], "dev", {"reason": "离线规则把指代识别错了"})
        self.assertEqual(dev["status"], "dev")
        self.assertEqual(self.wb.cards("shop_a")["summary"]["done"], 0, "转开发 is not counted as completed")

        self.wb.override("shop_a", first["interaction_id"], {"action": "handoff"}, "wording", "session_only")
        taken = self.wb.chat("shop_a", "还有货吗", session="o1")
        self.assertEqual(taken["action"], "handoff")
        self.assertFalse(taken["can_send"])

    def test_unmapped_product_card_is_linked_then_answers(self):
        kb = self.wb.knowledge("shop_a")
        card_ref = {"platform": "pinduoduo", "remote_product_id": "889900", "message_id": "pm1", "latest_is_card": True}
        result = self.wb.service("shop_a").respond("这个几钱", {}, platform_product=card_ref, session_id="p1")
        self.assertIn("platform_product", result["missing"])
        self.assertNotIn("元", result["draft_reply"], "no price is invented for an unmapped card")
        kb.save_turn("p1", "这个几钱", {}, result)
        card = next(c for c in self.cards() if c["card_type"] == "product_link")
        self.assertEqual(card["remote_product"], "pinduoduo:889900")
        with self.assertRaisesRegex(ValueError, "已生效的商品"):
            self.wb.card_action("shop_a", card["id"], "map_product", {"product_id": "NOPE"})
        mapped = self.wb.card_action("shop_a", card["id"], "map_product", {"product_id": "A1"})
        report = self.wb.card_action("shop_a", card["id"], "retest", {})["report"]
        self.assertTrue(report["passed"], report)
        self.wb.activate("shop_a", mapped["revision_id"], True)
        self.assertEqual(self.wb.card("shop_a", card["id"])["status"], "observing")
        again = self.wb.service("shop_a").respond("这个几钱", {}, platform_product=dict(card_ref, message_id="pm2"), session_id="p2")
        self.assertIn("199", again["draft_reply"])
        kb.save_turn("p2", "这个几钱", {}, again)
        self.assertEqual(self.wb.card("shop_a", card["id"])["status"], "observing")

    def test_platform_correction_survives_replay_restart_and_new_followup(self):
        w=self.wb;s='shop_a';cid='corrected-buyer'
        def append(mid,text):
            w.import_conversations(s,json.dumps({'store_id':s,'conversations':[{'conversation_id':cid,
                'messages':[{'message_id':mid,'role':'buyer','content':text}]}]},ensure_ascii=False))
        append('m1','黑杆多少钱')
        draft=w.generate_imported_draft(s,cid)
        w.override(s,draft['interaction_id'],{'product_id':'B1'},'misunderstood')
        corrected=w.generate_imported_draft(s,cid)
        self.assertEqual(corrected['product_id'],'B1');self.assertIn('239',corrected['draft_reply'])
        reopened=Workbench(w.root,w.stores_dir,w.registry)
        self.addCleanup(reopened.close_connections)
        w=reopened
        append('m2','多少钱')
        self.assertEqual(w.generate_imported_draft(s,cid)['product_id'],'B1')
        append('m3','换回黑杆，多少钱')
        self.assertEqual(w.generate_imported_draft(s,cid)['product_id'],'A1')

    def test_mismatched_conversation_is_rejected_before_any_override(self):
        s='shop_a'
        self.wb.import_conversations(s,json.dumps({'store_id':s,'conversations':[{'conversation_id':c,
            'messages':[{'message_id':c,'role':'buyer','content':'黑杆多少钱'}]} for c in ('one','two')]},ensure_ascii=False))
        draft=self.wb.generate_imported_draft(s,'one')
        with self.assertRaisesRegex(ValueError,'不一致'):
            self.wb.override(s,draft['interaction_id'],{'action':'handoff'},'misunderstood',conversation_id='two')
        self.assertEqual(self.wb.overrides(s),[])
        self.assertFalse(self.wb.inbox.get(self.wb.spec(s),'one')['paused'])
        self.assertFalse(self.wb.inbox.get(self.wb.spec(s),'two')['paused'])

    def test_unrelated_attribute_does_not_complete_material_card(self):
        w=self.wb;s='shop_a'
        w.chat(s,'黑杆什么材质',session='original')
        card=self.material_card()
        revision=w.card_action(s,card['id'],'supplement',{'kind':'products','content':{'upserts':[
            {'product_id':'A1','attributes':{'材质':'枫木','长度':'145cm'}}]}})['revision_id']
        w.card_action(s,card['id'],'retest',{});w.activate(s,revision,True)
        w.chat(s,'黑杆长度多少',session='length-buyer')
        result=w.card(s,card['id'])
        self.assertEqual(result['status'],'observing');self.assertIsNone(result['observation'])
        w.chat(s,'黑杆什么材质',session='material-buyer')
        result=w.card(s,card['id'])
        self.assertEqual(result['status'],'observing');self.assertFalse(result['observation']['real_reception'])

    def test_live_takeover_without_manual_conversation_id_blocks_old_send(self):
        import selftest_store_automation as fixtures
        f=fixtures.StoreAutomationTests();f.setUp()
        try:
            f.f.worker('draft').tick();draft=f.w.inbox.get(f.spec,'300')['draft']
            f.w.override('p1',draft['interaction_id'],{'action':'handoff'},'misunderstood')
            thread=f.w.inbox.get(f.spec,'300')
            self.assertTrue(thread['paused']);self.assertIsNone(thread['draft'])
            with self.assertRaises(ValueError):f.w.send_live_draft('p1','300',True,automatic=True)
            self.assertEqual(f.cdp.send_calls,0)
        finally:f.tearDown()

    def test_takeover_during_generation_rejects_old_result(self):
        import selftest_store_automation as fixtures
        from unittest.mock import patch
        f=fixtures.StoreAutomationTests();f.setUp()
        try:
            f.f.worker('draft').tick();draft=f.w.inbox.get(f.spec,'300')['draft']
            original=f.models.understand
            def understand(request):
                f.w.override('p1',draft['interaction_id'],{'action':'handoff'},'misunderstood')
                return original(request)
            with patch.object(f.models,'understand',side_effect=understand):
                with self.assertRaisesRegex(ValueError,'会话已更新或已交人工'):
                    f.w.generate_imported_draft('p1','300')
            self.assertTrue(f.w.inbox.get(f.spec,'300')['paused']);self.assertEqual(f.cdp.send_calls,0)
        finally:f.tearDown()

    def test_manual_answer_remains_preview_until_human_review(self):
        s='shop_a';cid='manual-answer'
        self.wb.import_conversations(s,json.dumps({'store_id':s,'conversations':[{'conversation_id':cid,
            'messages':[{'message_id':'manual-m1','role':'buyer','content':'黑杆多少钱'}]}]},ensure_ascii=False))
        draft=self.wb.generate_imported_draft(s,cid)
        result=self.wb.override(s,draft['interaction_id'],{'answer':'请以结算页价格为准'},'wording')
        self.assertFalse(result['sent']);self.assertTrue(self.wb.inbox.get(self.wb.spec(s),cid)['paused'])
        with self.assertRaises(ValueError):self.wb.generate_imported_draft(s,cid)
        self.wb.override(s,draft['interaction_id'],{'action':'resume'},'wording')
        self.assertFalse(self.wb.inbox.get(self.wb.spec(s),cid)['paused'])

    def test_confirmed_delivery_completes_card_but_live_draft_does_not(self):
        import selftest_store_automation as fixtures
        f=fixtures.StoreAutomationTests();f.setUp()
        try:
            w=f.w;kb=w.knowledge('p1');products=kb.active('products')['payload'];products[0].pop('price')
            kb.activate(kb.stage('products',products,'构造的价格缺口'),reviewed=True)
            f.f.worker('draft').tick()
            card=next(c for c in w.cards('p1')['cards'] if c['fields']==['price'])
            revision=w.card_action('p1',card['id'],'supplement',{'kind':'products','content':{'upserts':[
                {'product_id':'A','price':149}]}})['revision_id']
            report=w.card_action('p1',card['id'],'retest',{})
            self.assertTrue(report['report']['passed'],report['report'])
            w.activate('p1',revision,True)
            f.question('301');f.p.poll();draft=w.generate_imported_draft('p1','301')
            self.assertEqual(w.card('p1',card['id'])['status'],'observing')
            delivery=w.send_live_draft('p1','301',True,automatic=True)
            self.assertTrue(delivery['sent'])
            completed=w.card('p1',card['id'])
            self.assertEqual(completed['status'],'done');self.assertTrue(completed['observation']['real_reception'])
        finally:f.tearDown()

    def test_merge_rules_and_expired_knowledge(self):
        self.wb.chat("shop_a", "黑杆什么材质", session="m1")
        self.wb.chat("shop_a", "白杆什么材质", session="m2")
        a, b = [c for c in self.cards() if c["fields"] == ["attributes.材质"]]
        with self.assertRaisesRegex(ValueError, "不能合并"):
            self.wb.card_action("shop_a", a["id"], "merge", {"target_id": b["id"]})
        with self.assertRaisesRegex(ValueError, "原因"):
            self.wb.card_action("shop_a", a["id"], "not_applicable", {"reason": ""})

        self.publish("shop_a", "faq", [
            {"question": "双十一有活动吗", "answer": "满300减30", "expires_at": "2020-11-12"},
            {"question": "元旦有活动吗", "answer": "元旦满减", "effective_from": "2099-01-01"}])
        expired = self.wb.chat("shop_a", "双十一有活动吗", session="e1")
        self.assertNotIn("满300减30", expired["draft_reply"])
        self.assertIn("faq_expired", expired["missing"])
        self.assertTrue(any(c["card_type"] == "knowledge_error" for c in self.cards()))
        future = self.wb.chat("shop_a", "元旦有活动吗", session="e2")
        self.assertNotIn("元旦满减", future["draft_reply"])

    def test_summary_counts_closed_states_separately(self):
        self.wb.chat("shop_a", "黑杆什么材质", session="x1")
        card = self.material_card()
        self.wb.card_action("shop_a", card["id"], "assign", {"owner": "运营小王"})
        self.assertEqual(self.wb.card("shop_a", card["id"])["status"], "assigned")
        self.wb.card_action("shop_a", card["id"], "not_applicable", {"reason": "该款下架，不再补充"})
        summary = self.wb.cards("shop_a")["summary"]
        self.assertEqual((summary["not_applicable"], summary["done"]), (1, 0))
        with self.assertRaisesRegex(ValueError, "不能再操作"):
            self.wb.card_action("shop_a", card["id"], "assign", {"owner": "x"})
        events = [e["action"] for e in self.wb.card("shop_a", card["id"])["events"]]
        self.assertEqual(events, ["created", "assigned", "not_applicable"])

    def test_guided_attribute_form_stages_only_then_retests(self):
        self.wb.chat('shop_a','黑杆什么材质',session='guided')
        card=self.material_card()
        staged=self.wb.card_action('shop_a',card['id'],'fill_form',{'form':'attribute','field':'材质','value':'枫木','source':'已核对的规格表'})
        self.assertEqual(staged['status'],'retest')
        products=self.wb.knowledge('shop_a').preview(staged['revision_id'])['payload']
        self.assertEqual(products[0]['attributes'],{'长度':'145cm','材质':'枫木'})
        self.assertNotIn('材质',self.wb.knowledge('shop_a').active('products')['payload'][0]['attributes'])
        self.assertTrue(self.wb.card_action('shop_a',card['id'],'retest',{})['report']['passed'])

    def test_guided_faq_and_shipping_forms_cannot_publish_directly(self):
        self.wb.chat('shop_a','能刻字吗',session='faq')
        card=next(c for c in self.cards() if c['question_norm'])
        staged=self.wb.card_action('shop_a',card['id'],'fill_form',{'form':'faq','question':'能刻字吗','value':'本店暂不提供刻字服务'})
        self.assertEqual(staged['status'],'retest')
        self.assertIsNone(self.wb.knowledge('shop_a').active('faq'))
        with self.assertRaises(ValueError):self.wb.card_action('shop_a',card['id'],'fill_form',{'form':'faq','value':''})
        with self.assertRaises(ValueError):self.wb.card_action('shop_a',card['id'],'fill_form',{'form':'shipping','field':'refund','value':'自动退款'})
        shipping=self.wb.card_action('shop_a',card['id'],'fill_form',{'form':'shipping','field':'lead_time','value':'24小时内发货'})
        proposed=self.wb.knowledge('shop_a').preview(shipping['revision_id'])['payload']
        self.assertEqual(proposed['shipping']['lead_time'],'24小时内发货')
        self.assertEqual(self.wb.knowledge('shop_a').active('business')['payload']['shipping']['lead_time'],'48小时内发货')

    def test_guided_multiple_attributes_accumulate_before_publish(self):
        products=json.loads(json.dumps(PRODUCTS,ensure_ascii=False));products[0]['attributes']={}
        self.publish('shop_a','products',products)
        self.wb.chat('shop_a','黑杆材质和成分',session='multiple')
        card=next(c for c in self.cards() if c['product_id']=='A1' and any(f.startswith('attributes') for f in c['fields']))
        for field,value in [('材质','枫木'),('成分','木材')]:
            staged=self.wb.card_action('shop_a',card['id'],'fill_form',{'form':'attribute','field':field,'value':value})
        proposed=self.wb.knowledge('shop_a').preview(staged['revision_id'])['payload']
        self.assertEqual(proposed[0]['attributes'],{'材质':'枫木','成分':'木材'})
        self.assertTrue(self.wb.card_action('shop_a',card['id'],'retest',{})['report']['passed'])


if __name__ == "__main__":
    unittest.main(verbosity=1)
