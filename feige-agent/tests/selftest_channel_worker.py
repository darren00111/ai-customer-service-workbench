"""Full receive -> model contract -> store facts -> delivery -> learning records."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import selftest_channel_pilots as fixture
from app.channels.reception_worker import ChannelReceptionWorker
from app.notifications import HandoffOutbox
from app.multistore import MultiStoreSupervisor


class TestModel:
    name='synthetic_contract'
    def __init__(self):self.calls=0;self.fail=False
    def status(self):return {'enabled':True,'connected':True,'configured':True,'model':'test','error':''}
    def interpreter(self):return self
    def understand(self,request):
        self.calls+=1
        if self.fail:raise ValueError('fixture model down')
        return {'intents':['after_sale' if '退货' in request['latest'] else 'price'],'product_id':'A'}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PilotTests();self.f.setUp()
        self.w,self.p,self.cdp,self.spec=self.f.w,self.f.p,self.f.cdp,self.f.spec
        self.models=TestModel();self.w.models=self.models
        kb=self.w.knowledge('p1')
        kb.activate(kb.stage('products',[{'product_id':'A','title':'黑色台球杆','aliases':['黑杆'],'price':149,'stock':5}], '构造店铺资料'),reviewed=True)
        self.f.connect();self.cdp.push(fixture.frame())
        self.echo=True
        original=self.p._post
        def post(path,body):
            if path!='/plateau/chat/send_message':return original(path,body)
            self.cdp.send_calls+=1
            mid='out'+str(self.cdp.send_calls)
            if self.echo:self.cdp.push(fixture.frame(mid,'agent',body['data']['message']['content'],ts=1800000001000))
            return {'success':True,'result':{'msg_id':mid}}
        self.p._post=post

    def tearDown(self):self.f.tearDown()

    def worker(self,mode='draft',conversations=('300',)):
        return ChannelReceptionWorker(self.w,'p1',mode,conversations)

    def test_continuous_draft_uses_store_facts_and_no_send(self):
        worker=self.worker();worker.tick()
        thread=self.w.inbox.get(self.spec,'300')
        self.assertIn('149',thread['draft']['draft_reply'])
        self.assertEqual(self.cdp.send_calls,0)
        self.assertFalse(thread['draft']['sent'])

    def test_one_message_does_not_repeat_model_calls_on_polling(self):
        worker=self.worker();worker.tick();calls=self.models.calls
        worker.tick();worker.tick()
        self.assertEqual(self.models.calls,calls)

    def test_automatic_send_is_limited_to_designated_conversation(self):
        worker=self.worker('automatic',('another-buyer',));worker.tick()
        self.assertEqual(self.cdp.send_calls,0)
        self.assertTrue(self.w.inbox.get(self.spec,'300')['draft'])

    def test_confirmed_automatic_reply_updates_actual_interaction_record(self):
        worker=self.worker('automatic');worker.tick()
        self.assertEqual(self.cdp.send_calls,1);self.assertEqual(worker.confirmed,1)
        with self.w.knowledge('p1').connect() as db:
            result=json.loads(db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertTrue(result['sent']);self.assertTrue(result['automatic_resolution'])
        self.assertEqual(result['delivery_channel'],'live_confirmed')

    def test_delayed_echo_is_reconciled_without_resending(self):
        self.echo=False;worker=self.worker('automatic');worker.tick()
        self.assertEqual(len(worker.pending),1);self.assertEqual(worker.confirmed,0)
        draft=self.w.inbox.get(self.spec,'300')['draft']['draft_reply']
        self.cdp.push(fixture.frame('out1','agent',draft,ts=1800000001000));worker.tick()
        self.assertEqual(worker.confirmed,1);self.assertEqual(self.cdp.send_calls,1)
        self.assertEqual(worker.pending,{})

    def test_model_failure_cannot_send_or_loop_requests(self):
        self.models.fail=True;worker=self.worker('automatic');worker.tick();calls=self.models.calls;worker.tick()
        self.assertEqual(self.cdp.send_calls,0);self.assertEqual(calls,self.models.calls)

    def test_after_sales_pauses_and_enters_real_human_queue(self):
        self.cdp.push(fixture.frame('m2',text='我要退货',ts=1800000000500))
        worker=self.worker('automatic');worker.tick()
        self.assertEqual(self.cdp.send_calls,0);self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        events=HandoffOutbox(self.w.root.parent/'notifications.jsonl').list_pending()
        self.assertEqual(len(events),1);self.assertTrue(events[0]['external_manual'])
        factory=Mock()
        with patch('app.multistore.DATA_DIR',self.w.root.parent):
            supervisor=MultiStoreSupervisor(specs=[self.spec],agent_factory=factory)
        try:
            self.assertFalse(supervisor.send_handoff_reply(events[0]['event_id'],'人工答复')['ok'])
            self.assertTrue(supervisor.resolve_handoff(events[0]['event_id']))
            factory.assert_not_called()
        finally:supervisor.shutdown()

    def test_manual_takeover_does_not_get_replaced_by_an_automatic_answer(self):
        self.cdp.push(fixture.frame('human','agent','人工正在处理',ts=1800000001000))
        worker=self.worker('automatic');worker.tick()
        self.assertEqual(self.cdp.send_calls,0);self.assertEqual(self.models.calls,0)

    def test_no_model_or_no_test_scope_cannot_enable_automatic_mode(self):
        self.p.poll()
        with self.assertRaises(ValueError):self.w.start_channel_monitor('p1','automatic',[],True)
        with self.assertRaises(ValueError):self.w.start_channel_monitor('p1','automatic',['300'],False)
        self.w.models=None
        with self.assertRaises(ValueError):self.w.start_channel_monitor('p1','automatic',['300'],True)

    def test_only_observed_live_conversation_can_be_the_test_target(self):
        with self.assertRaises(ValueError):self.w.start_channel_monitor('p1','automatic',['not-observed'],True)

    def test_manual_review_send_does_not_claim_automatic_resolution(self):
        worker=self.worker();worker.tick();self.w.send_live_draft('p1','300',True)
        with self.w.knowledge('p1').connect() as db:
            result=json.loads(db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertTrue(result['sent']);self.assertFalse(result['automatic_resolution'])
        self.assertEqual(result['delivery_channel'],'live_reviewed')

    def test_stopped_worker_leaves_new_messages_untouched(self):
        worker=self.worker('automatic');worker.stop();worker.tick()
        self.assertEqual(self.cdp.send_calls,0);self.assertEqual(self.models.calls,0)

    def test_stop_during_understanding_prevents_the_later_send(self):
        worker=self.worker('automatic')
        original=self.models.understand
        def understand(request):
            result=original(request);worker.stop_event.set();return result
        self.models.understand=understand
        worker.tick()
        self.assertEqual(self.models.calls,1);self.assertEqual(self.cdp.send_calls,0)

    def test_final_send_guard_honors_cancellation(self):
        self.worker().tick()
        with self.assertRaises(ValueError):
            self.w.send_live_draft('p1','300',True,automatic=True,cancelled=lambda:True)
        self.assertEqual(self.cdp.send_calls,0)

    def test_restart_cannot_overlap_a_stopping_model_task(self):
        entered,release=threading.Event(),threading.Event()
        original=self.models.understand
        def understand(request):
            entered.set();release.wait(8);return original(request)
        self.models.understand=understand
        worker=self.worker('automatic');self.w._channel_workers['p1']=worker
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError):
                self.w.start_channel_monitor('p1','automatic',['300'],True)
            self.assertTrue(worker.status()['stopping'])
            self.assertIs(self.w._channel_workers['p1'],worker)
        finally:
            release.set();worker.stop()
        self.assertEqual(self.cdp.send_calls,0)

    def test_reconfirm_repairs_a_learning_write_interruption_without_duplicate_events(self):
        self.worker().tick()
        draft=self.w.inbox.get(self.spec,'300')['draft']
        with patch('app.service.workbench.CommerceLearning.record_reception',side_effect=OSError('fixture crash')):
            with self.assertRaises(OSError):self.w.send_live_draft('p1','300',True)
        with self.w._workspace.connect() as db:
            token=db.execute('SELECT token FROM live_deliveries').fetchone()[0]
        delivery=self.w.live.delivery(token)
        with patch('app.service.workbench.CommerceLearning.record_reception') as record:
            self.w.confirm_channel_delivery('p1',draft,delivery,True)
            record.assert_called_once()
            self.assertFalse(record.call_args.args[2]['automatic_resolution'])
        with self.w.knowledge('p1').connect() as db:
            count=db.execute("SELECT COUNT(*) FROM conversation_events WHERE reference='platform:out1'").fetchone()[0]
        self.assertEqual(count,1);self.assertEqual(self.cdp.send_calls,1)

    def test_delayed_receipt_survives_loss_of_worker_memory(self):
        self.echo=False;old=self.worker('automatic');old.tick()
        draft=self.w.inbox.get(self.spec,'300')['draft']['draft_reply']
        # A new worker has no in-memory pending map.
        old.stop();new=self.worker('automatic')
        self.cdp.push(fixture.frame('out1','agent',draft,ts=1800000001000));new.tick()
        with self.w.knowledge('p1').connect() as db:
            result=json.loads(db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertTrue(result['sent']);self.assertTrue(result['automatic_resolution'])
        self.assertEqual(self.w.live.unreconciled(self.spec),[])
        self.assertEqual(self.cdp.send_calls,1)

    def test_missing_echo_does_not_enter_learning_as_a_sent_reply(self):
        self.echo=False;worker=self.worker('automatic');worker.tick()
        for pending in worker.pending.values():pending['started']-=16
        worker.tick();self.w.reconcile_channel_deliveries('p1')
        with self.w.knowledge('p1').connect() as db:
            result=json.loads(db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertFalse(result['sent']);self.assertFalse(result['automatic_resolution'])
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused']);self.assertEqual(self.cdp.send_calls,1)

    def test_manual_closure_never_resends_old_head_but_allows_new_consultation(self):
        self.echo=False;worker=self.worker('automatic');worker.tick()
        token=next(iter(worker.pending))
        for pending in worker.pending.values():pending['started']-=16
        worker.tick()
        for store,reviewed in [('p1',False),('p2',True)]:
            with self.assertRaises(ValueError):self.w.close_uncertain_delivery(store,'300',token,reviewed)
        self.w.close_uncertain_delivery('p1','300',token,True)
        self.w.close_uncertain_delivery('p1','300',token,True)
        self.w.live.finish(token,'awaiting_echo','late-response')
        self.assertEqual(self.w.live.delivery(token)['state'],'manually_closed')
        worker.tick();self.assertEqual(self.cdp.send_calls,1)
        with self.w._workspace.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM live_delivery_reviews').fetchone()[0],1)
        self.assertFalse(self.w.inbox.get(self.spec,'300')['paused'])
        self.echo=True;self.cdp.push(fixture.frame('new-after-review',text='黑杆多少钱'))
        worker.tick();self.assertEqual(self.cdp.send_calls,2)
        self.assertEqual(worker.confirmed,1)
        with self.w.knowledge('p1').connect() as db:
            rows=[json.loads(r[0]) for r in db.execute('SELECT result FROM interactions ORDER BY id')]
        self.assertFalse(rows[0]['sent']);self.assertTrue(rows[-1]['sent'])

    def test_reviewing_old_unknown_cannot_clear_a_newer_pause_reason(self):
        self.echo=False;worker=self.worker('automatic');worker.tick();token=next(iter(worker.pending))
        self.cdp.push(fixture.frame('control',type=1002,ts=1800000000500));self.p.poll()
        self.w.close_uncertain_delivery('p1','300',token,True)
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        worker.tick();self.assertEqual(self.cdp.send_calls,1)
        self.assertNotIn(token,worker.pending)

    def test_manual_delayed_receipt_reconciles_on_poll_after_workbench_restart(self):
        from app.service.workbench import Workbench
        from app.channels.pinduoduo import PinduoduoPilot
        self.echo=False;self.worker().tick()
        draft=self.w.inbox.get(self.spec,'300')['draft']['draft_reply']
        result=self.w.send_live_draft('p1','300',True)
        self.assertEqual(result['state'],'awaiting_echo')
        self.w.close_connections()
        reopened=Workbench(self.w.root,self.w.stores_dir,self.w.registry)
        cdp=fixture.FakeCDP();pilot=PinduoduoPilot(self.spec,self.f.root/'profiles',reopened.live,lambda _:cdp)
        reopened._pilots['p1']=pilot
        try:
            pilot.inspect();pilot.bind(fixture.ACCOUNT.key,True);cdp.socket()
            cdp.push(fixture.frame('out1','agent',draft,ts=1800000001000))
            reopened.pilot_command('p1','poll')
            with reopened.knowledge('p1').connect() as db:
                saved=json.loads(db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1').fetchone()[0])
            self.assertTrue(saved['sent']);self.assertFalse(saved['automatic_resolution'])
            self.assertEqual(saved['delivery_channel'],'live_reviewed')
            self.assertEqual(reopened.live.unreconciled(self.spec),[])
        finally:reopened.close_connections()


if __name__=='__main__':unittest.main(verbosity=1)
