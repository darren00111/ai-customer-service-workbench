"""Inject transport faults through actual persisted inbox and ledger gates."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.channels.sandbox import Sandbox


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.s=Sandbox(self.root)
        self.sid='sandbox_a';self.spec=self.s.add_store(self.sid)

    def incoming(self,mid='m1',text='黑杆多少钱',**kwargs):
        self.s.enqueue(self.sid,self.s.packet(mid,text,**kwargs))
        return self.s.drain()

    def test_received_draft_send_and_receipt_really_match(self):
        self.incoming();draft=self.s.draft(self.sid)
        self.assertIn('199',draft['draft_reply'])
        receipt=self.s.send(self.sid)
        self.assertTrue(receipt['sent']);self.assertEqual('confirmed',receipt['state'])
        self.assertFalse(self.s.summary()['customer_messages_sent'])

    def test_duplicate_message_and_conflicting_id(self):
        self.assertEqual('received',self.incoming()[0]['state'])
        self.assertEqual('duplicate',self.incoming()[0]['state'])
        self.assertEqual('rejected',self.incoming(text='白杆多少钱')[0]['state'])
        self.assertEqual(1,len(self.s.workbench.inbox.get(self.spec,'same_buyer')['messages']))

    def test_queue_survives_restart_and_drains_once(self):
        packet=self.s.packet('m1','黑杆多少钱')
        a=self.s.enqueue(self.sid,packet,'event1')
        self.assertEqual(a,self.s.enqueue(self.sid,packet,'event1'))
        resumed=Sandbox(self.root);resumed.add_store(self.sid)
        self.assertEqual('received',resumed.drain()[0]['state']);self.assertEqual([],resumed.drain())

    def test_new_message_invalidates_draft(self):
        self.incoming();self.s.draft(self.sid)
        self.incoming('m2','白杆多少钱',timestamp=1800000000001)
        with self.assertRaises(ValueError): self.s.send(self.sid)

    def test_human_reply_and_manual_pause_prevent_send(self):
        self.incoming();self.s.draft(self.sid)
        self.incoming('h1','我来处理',role='agent',timestamp=1800000000001)
        with self.assertRaises(ValueError): self.s.send(self.sid)
        self.assertTrue(self.s.workbench.inbox.get(self.spec,'same_buyer')['paused'])

    def test_timeout_not_retried_even_after_restart(self):
        self.incoming();self.s.draft(self.sid);receipt=self.s.send(self.sid,fault='timeout')
        self.assertEqual('delivery_unknown',receipt['state'])
        resumed=Sandbox(self.root);resumed.add_store(self.sid)
        with self.assertRaises(ValueError): resumed.draft(self.sid)
        with self.assertRaises(ValueError): resumed.send(self.sid)

    def test_delayed_echo_confirms_after_restart(self):
        self.incoming();self.s.draft(self.sid);receipt=self.s.send(self.sid,fault='delayed')
        self.assertFalse(receipt['sent'])
        resumed=Sandbox(self.root);resumed.add_store(self.sid)
        self.assertTrue(resumed.echo(receipt['token'])['sent'])

    def test_wrong_echo_cannot_confirm(self):
        self.incoming();self.s.draft(self.sid);receipt=self.s.send(self.sid,fault='delayed')
        self.assertFalse(self.s.echo(receipt['token'],correct=False)['sent'])

    def test_crash_after_attempt_becomes_unknown(self):
        self.incoming();self.s.draft(self.sid);receipt=self.s.send(self.sid,fault='crash')
        self.assertEqual('attempting',receipt['state'])
        resumed=Sandbox(self.root);resumed.add_store(self.sid)
        self.assertEqual('delivery_unknown',resumed.workbench.live.delivery(receipt['token'])['state'])

    def test_unknown_media_withdraw_and_out_of_order_pause(self):
        for i,kind in enumerate(('image','video','unknown','withdraw')):
            buyer='buyer%d' % i
            self.incoming('media%d' % i,'[媒体]',conversation_id=buyer,kind=kind)
            self.assertTrue(self.s.workbench.inbox.get(self.spec,buyer)['paused'])
        self.incoming(timestamp=1800000000002)
        self.incoming('m2','旧问题',timestamp=1800000000001)
        self.assertEqual('message_order',self.s.workbench.inbox.get(self.spec,'same_buyer')['pause_reason'])

    def test_aftersale_enters_handoff_and_blocks_send(self):
        self.incoming(text='我要退货');result=self.s.draft(self.sid)
        self.assertEqual('handoff',result['action'])
        with self.assertRaises(ValueError):self.s.send(self.sid)

    def test_knowledge_update_invalidates_old_draft(self):
        self.incoming();self.s.draft(self.sid)
        kb=self.s.workbench.knowledge(self.sid)
        row=kb.active('products')['payload'];row[0]['price']=299
        rev=kb.stage('products',row,'合成更新',demo=True);kb.activate(rev,allow_demo=True)
        with self.assertRaises(ValueError):self.s.send(self.sid)

    def test_three_platforms_same_buyer_and_product_have_different_prices(self):
        for index,platform in enumerate(('feige','pinduoduo','taobao')):
            sid='sandbox_'+platform;self.s.add_store(sid,platform,price_delta=index*10)
            self.s.enqueue(sid,self.s.packet('same-id','黑杆多少钱'))
        self.s.drain()
        for index,platform in enumerate(('feige','pinduoduo','taobao')):
            sid='sandbox_'+platform
            self.assertIn(str(199+index*10),self.s.draft(sid)['draft_reply'])
            self.assertTrue(self.s.send(sid)['sent'])

    def test_no_production_ids_and_invalid_packets_are_audited(self):
        with self.assertRaises(ValueError): self.s.add_store('shop_001')
        self.assertEqual('rejected',self.incoming(role='unknown')[0]['state'])
        self.assertEqual(1,self.s.summary()['events']['rejected'])


if __name__=='__main__':unittest.main()
