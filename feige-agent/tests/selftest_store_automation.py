"""Constructed store-wide lifecycle tests; no real shop or model calls."""
from pathlib import Path
import json
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import selftest_channel_pilots as fixture
import selftest_channel_worker as workers
from app.channels.reception_worker import ChannelReceptionWorker
from app.desktop import DesktopAPI
from app.notifications import HandoffOutbox
from app.platforms import platform_info
from app.service.workbench import Workbench
from selftest_desktop import FakeEmbedder

BASE=1800000000000


class StoreAutomationTests(unittest.TestCase):
    def setUp(self):
        self.f=workers.WorkerTests();self.f.setUp()
        self.w,self.p,self.cdp,self.spec,self.models=self.f.w,self.f.p,self.f.cdp,self.f.spec,self.f.models
        self.models.name='qwen/test'  # Protocol fixture, not an actual paid model.
        kb=self.w.knowledge('p1');kb.activate(kb.stage('business',{},'构造的已核对政策'),reviewed=True)
        self.clock=BASE
        original=self.p._post
        def post(path,body):
            if path!='/plateau/chat/send_message':return original(path,body)
            self.cdp.send_calls+=1;self.clock+=1000
            msg=body['data']['message'];mid='store-out-'+str(self.cdp.send_calls)
            self.cdp.push(fixture.frame(mid,'agent',msg['content'],ts=self.clock,
                to={'role':'user','uid':msg['to']['uid']}))
            return {'success':True,'result':{'msg_id':mid}}
        self.p._post=post

    def tearDown(self):self.f.tearDown()

    def complete_test(self):
        worker=self.f.worker('automatic');worker.tick()
        self.assertEqual(worker.confirmed,1)

    def enable(self):
        self.complete_test()
        return self.w.set_store_automation('p1',True,True)

    def question(self,cid='301',text='黑杆多少钱'):
        self.clock+=1000
        self.cdp.push(fixture.frame('new-'+str(self.clock),text=text,ts=self.clock,
            **{'from':{'role':'user','uid':cid}}))

    def worker(self,grant):
        return ChannelReceptionWorker(self.w,'p1','store',[],grant['generation'])

    def latest_result(self):
        with self.w.knowledge('p1').connect() as db:
            row=db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1').fetchone()
        return json.loads(row['result'])

    def desktop(self):
        specs=[self.w.spec('p1'),self.w.spec('p2')]
        supervisor=SimpleNamespace(specs=specs,notifications=HandoffOutbox(self.w.root.parent/'notifications.jsonl'),
            boot_all=Mock(return_value={'configured':2,'attempted':0,'online':0,'results':[]}),
            boot_store=Mock(side_effect=AssertionError('PDD must not use the Feige adapter')),
            start_monitor=Mock(),shutdown=Mock())
        supervisor.status=lambda:{'configured':2,'online':0,'needs_attention':2,'ts':'2026-11-02T00:00:00','shops':[
            {'store_id':s.store_id,'name':s.name,'platform':s.platform,'connection':platform_info(s.platform),
             'state':'connection_required'} for s in specs]}
        supervisor.resolve_handoff=supervisor.notifications.mark_done
        return DesktopAPI(supervisor,FakeEmbedder(),SimpleNamespace(_workbench=self.w))

    def test_default_off_without_connection_or_worker_auto_start(self):
        self.assertFalse(self.w.automation.status(self.spec)['enabled'])
        self.assertEqual(self.w._channel_workers,{})
        with self.assertRaises(ValueError):self.w.start_channel_monitor('p1','store',[],True)
        self.assertEqual(self.cdp.send_calls,0)

    def test_checkbox_alone_cannot_enable_storewide_without_confirmed_test(self):
        with self.assertRaisesRegex(ValueError,'指定测试顾客'):self.w.set_store_automation('p1',True,True)
        self.assertFalse(self.w.automation.status(self.spec)['enabled'])

    def test_reviewed_manual_reply_is_not_an_automatic_test(self):
        self.f.worker().tick();self.w.send_live_draft('p1','300',True)
        self.assertEqual(self.cdp.send_calls,1)
        with self.assertRaisesRegex(ValueError,'指定测试顾客'):self.w.set_store_automation('p1',True,True)

    def test_enabling_requires_review_and_does_not_start_a_worker_by_itself(self):
        self.complete_test()
        with self.assertRaises(ValueError):self.w.set_store_automation('p1',True,False)
        grant=self.w.set_store_automation('p1',True,True)
        self.assertTrue(grant['enabled']);self.assertFalse(grant['production_verified'])
        self.assertEqual(self.w._channel_workers,{})
        self.assertEqual(self.cdp.send_calls,1)

    def test_storewide_replies_to_new_customer_outside_the_test_list(self):
        worker=self.worker(self.enable());self.question('301');worker.tick()
        self.assertEqual(worker.confirmed,1)
        self.assertEqual(self.cdp.send_calls,2)
        self.assertTrue(self.latest_result()['model_connected'])
        self.assertEqual(self.w.live.history(self.spec,'301')[0]['state'],'confirmed')
        self.assertIn('149',self.latest_result()['draft_reply'])

    def test_disabling_during_model_call_cancels_send(self):
        worker=self.worker(self.enable());self.question()
        original=self.models.understand
        def understand(request):
            self.w.set_store_automation('p1',False)
            return original(request)
        with patch.object(self.models,'understand',side_effect=understand):worker.tick()
        self.assertEqual(self.cdp.send_calls,1)
        self.assertFalse(self.w.automation.status(self.spec)['enabled'])

    def test_reception_is_not_limited_by_the_ui_first_100_conversations(self):
        worker=self.worker(self.enable())
        for uid in range(500,601):self.question(str(uid))
        worker.tick()
        self.assertEqual(worker.confirmed,101)
        self.assertEqual(self.cdp.send_calls,102)
        self.assertEqual(self.w.live.history(self.spec,'500')[0]['state'],'confirmed')
        self.assertEqual(self.w.live.history(self.spec,'600')[0]['state'],'confirmed')

    def test_approved_knowledge_update_rebuilds_unanswered_head_without_new_customer_message(self):
        worker=self.f.worker('draft');worker.tick()
        before=self.models.calls
        worker.tick();self.assertEqual(self.models.calls,before)
        kb=self.w.knowledge('p1');products=kb.active('products')['payload'];products[0]['price']=159
        kb.activate(kb.stage('products',products,'商家核对后的新价格'),reviewed=True)
        worker.tick()
        self.assertEqual(self.models.calls,before+1)
        self.assertIn('159',self.w.inbox.get(self.spec,'300')['draft']['draft_reply'])

    def test_regrant_does_not_revalidate_old_inflight_generation(self):
        old=self.enable();worker=self.worker(old)
        new=self.w.set_store_automation('p1',True,True)
        self.assertGreater(new['generation'],old['generation'])
        self.question()
        with self.assertRaises(ValueError):worker.tick()
        self.assertEqual(self.cdp.send_calls,1)

    def test_changed_model_stops_old_storewide_permission(self):
        worker=self.worker(self.enable());self.question()
        status=self.models.status();status['model']='other-model'
        with patch.object(self.models,'status',return_value=status):
            with self.assertRaisesRegex(ValueError,'模型配置已变化'):worker.tick()
        self.assertEqual(self.cdp.send_calls,1)

    def test_reviewed_product_update_changes_next_storewide_reply(self):
        worker=self.worker(self.enable())
        kb=self.w.knowledge('p1');products=kb.active('products')['payload'];products[0]['price']=159
        kb.activate(kb.stage('products',products,'商家核对后的新标价'),reviewed=True)
        self.question();worker.tick()
        self.assertIn('159',self.latest_result()['draft_reply'])
        self.assertTrue(self.w.automation.status(self.spec)['enabled'])

    def test_new_customer_after_sales_enters_human_queue_without_sending(self):
        worker=self.worker(self.enable());self.question(text='我要退货');worker.tick()
        self.assertEqual(self.cdp.send_calls,1)
        self.assertTrue(self.w.inbox.get(self.spec,'301')['paused'])
        self.assertEqual(self.w.inbox.get(self.spec,'301')['pause_reason'],'handoff')
        self.assertEqual(worker.handoffs,1)

    def test_handled_aftersales_allows_future_sales_but_never_replies_to_the_handled_head(self):
        worker=self.worker(self.enable());self.question('300','我要退货');worker.tick()
        api=self.desktop();event=api._supervisor.notifications.list_pending()[0]
        result=api.mark_notification_done(event['event_id'])
        self.assertTrue(result['ok']);self.assertTrue(result['conversation_resumed'])
        worker.tick();self.assertEqual(self.cdp.send_calls,1)
        self.question('300');worker.tick()
        self.assertEqual(self.cdp.send_calls,2)
        self.assertIn('149',self.latest_result()['draft_reply'])
        self.assertEqual(self.latest_result()['action'],'reply')

    def test_failed_handoff_notification_is_durable_and_retried_without_model_call(self):
        worker=self.worker(self.enable());self.question('300','我要退货')
        with patch.object(HandoffOutbox,'publish',side_effect=OSError('constructed disk error')):
            worker.tick()
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertIn('p1',self.w._handoff_errors)
        with self.w._workspace.connect() as db:
            self.assertEqual(db.execute('SELECT published FROM channel_handoff_outbox').fetchone()[0],0)
        calls=self.models.calls
        worker.tick()
        self.assertEqual(self.models.calls,calls)
        self.assertEqual(len(self.desktop()._supervisor.notifications.list_pending()),1)
        self.assertNotIn('p1',self.w._handoff_errors)

    def test_republishing_after_crash_never_reopens_completed_handoff(self):
        worker=self.worker(self.enable());self.question('300','我要退货');worker.tick()
        api=self.desktop();event=api._supervisor.notifications.list_pending()[0]
        api.mark_notification_done(event['event_id'])
        with self.w._workspace.connect() as db:
            db.execute('UPDATE channel_handoff_outbox SET published=0')
        self.assertEqual(self.w.reconcile_channel_handoffs('p1'),1)
        self.assertEqual(api._supervisor.notifications.list_pending(),[])
        self.assertFalse(self.w.inbox.get(self.spec,'300')['paused'])

    def test_restart_repairs_handoff_without_platform_login(self):
        worker=self.worker(self.enable());self.question('300','我要退货')
        with patch.object(HandoffOutbox,'publish',side_effect=OSError('constructed disk error')):
            worker.tick()
        self.w.close_connections()
        reopened=Workbench(self.w.root,self.w.stores_dir,self.w.registry,self.models)
        try:
            reopened.channel_runtime()
            self.assertEqual(reopened._pilots,{})
            self.assertEqual(reopened._channel_workers,{})
            self.assertEqual(len(HandoffOutbox(self.w.root.parent/'notifications.jsonl').list_pending()),1)
            self.assertTrue(reopened.inbox.get(self.spec,'300')['paused'])
        finally:reopened.close_connections()

    def test_second_aftersales_from_same_customer_has_a_new_human_alert(self):
        worker=self.worker(self.enable());self.question('300','我要退货');worker.tick()
        api=self.desktop();first=api._supervisor.notifications.list_pending()[0]
        api.mark_notification_done(first['event_id'])
        self.question('300','我要退货');worker.tick()
        pending=api._supervisor.notifications.list_pending()
        self.assertEqual(len(pending),1)
        self.assertNotEqual(pending[0]['event_id'],first['event_id'])
        self.assertFalse(self.w.complete_channel_handoff(first))
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])

    def test_completing_handoff_preserves_a_later_media_pause(self):
        worker=self.worker(self.enable());self.question('300','我要退货');worker.tick()
        api=self.desktop();event=api._supervisor.notifications.list_pending()[0]
        self.clock+=1000;self.cdp.push(fixture.frame('photo-after-handoff',type=1,ts=self.clock));self.p.poll()
        result=api.mark_notification_done(event['event_id'])
        self.assertTrue(result['ok']);self.assertFalse(result['conversation_resumed'])
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.w.inbox.get(self.spec,'300')['pause_reason'],'unsupported_content')

    def test_restart_preserves_permission_but_does_not_start_or_bind(self):
        self.enable();self.w.close_connections()
        reopened=Workbench(self.w.root,self.w.stores_dir,self.w.registry,self.models)
        try:
            self.assertTrue(reopened.automation.status(self.spec)['enabled'])
            self.assertEqual(reopened._pilots,{})
            self.assertEqual(reopened._channel_workers,{})
            with self.assertRaises(ValueError):reopened.require_store_automation('p1')
        finally:reopened.close_connections()

    def reconnect(self):
        self.p.inspect();self.p.bind(fixture.ACCOUNT.key,True);self.cdp.socket()

    def test_new_live_question_resumes_a_connection_only_pause(self):
        grant=self.enable();self.p.close()
        self.assertEqual(self.w.inbox.get(self.spec,'300')['pause_reason'],'connection')
        self.reconnect();self.question('300');worker=self.worker(grant);worker.tick()
        self.assertFalse(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.cdp.send_calls,2)

    def test_prebinding_replay_cannot_resume_connection_pause(self):
        grant=self.enable();self.p.close();self.p.inspect();self.cdp.socket()
        self.question('300');self.p.bind(fixture.ACCOUNT.key,True)
        self.worker(grant).tick()
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.cdp.send_calls,1)

    def test_manual_pause_survives_reconnection_and_new_question(self):
        grant=self.enable();self.w.inbox.pause(self.spec,'300',True);self.p.close()
        self.reconnect();self.question('300');self.worker(grant).tick()
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.w.inbox.get(self.spec,'300')['pause_reason'],'manual')
        self.assertEqual(self.cdp.send_calls,1)

    def test_aftersales_pause_survives_reconnection_and_new_question(self):
        grant=self.enable();worker=self.worker(grant);self.question('300','我要退货');worker.tick()
        self.p.close();self.reconnect();self.question('300');self.worker(grant).tick()
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.w.inbox.get(self.spec,'300')['pause_reason'],'handoff')
        self.assertEqual(self.cdp.send_calls,1)

    def test_unconfirmed_delivery_pause_survives_reconnection_and_new_question(self):
        grant=self.enable();self.question('300');self.f.worker().tick()
        thread=self.w.inbox.get(self.spec,'300')
        token=self.w.live.reserve(self.spec,fixture.ACCOUNT.key,'300',thread['draft_head'],thread['draft']['draft_reply'])
        self.w.live.finish(token,'delivery_unknown')
        self.p.close();self.reconnect();self.question('300');self.worker(grant).tick()
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.w.inbox.get(self.spec,'300')['pause_reason'],'unconfirmed_delivery')
        self.assertEqual(self.cdp.send_calls,1)

    def test_unsupported_message_pause_is_not_just_a_connection_pause(self):
        grant=self.enable();self.clock+=1000
        self.cdp.push(fixture.frame('media',type=1,ts=self.clock));self.p.poll();self.p.close()
        self.reconnect();self.question('300');self.worker(grant).tick()
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.w.inbox.get(self.spec,'300')['pause_reason'],'unsupported_content')
        self.assertEqual(self.cdp.send_calls,1)

    def test_qianniu_checkbox_cannot_bypass_unverified_message_metadata(self):
        from app.channels.pinduoduo import Account
        self.w.create_store('tb','构造千牛资料',platform='taobao')
        spec=self.w.spec('tb');account=Account('taobao','qn200','构造账号')
        native=SimpleNamespace(spec=spec,bound=True,account=account,lock=threading.RLock(),poll=lambda:None,
            status=lambda:{'verified_message_metadata':False},close=lambda:None)
        self.w._pilots['tb']=native
        with self.assertRaisesRegex(ValueError,'千牛逐条消息身份'):
            self.w.set_store_automation('tb',True,True)
        self.assertFalse(self.w.automation.status(spec)['enabled'])

    def test_other_store_cannot_use_this_stores_test_receipt(self):
        self.enable()
        with self.assertRaises(ValueError):self.w.set_store_automation('p2',True,True)
        self.assertFalse(self.w.automation.status(self.w.spec('p2'))['enabled'])

    def test_same_remote_account_cannot_be_bound_to_two_local_shop_profiles(self):
        self.p.poll()
        with self.assertRaisesRegex(ValueError,'不能重复接待'):
            self.w.live.bind(self.w.spec('p2'),fixture.ACCOUNT.key)

    def test_desktop_start_all_routes_authorized_shop_without_enabling_other_shop(self):
        self.enable();api=self.desktop()
        result=api.start_all()
        self.assertEqual(result['attempted'],1);self.assertEqual(result['online'],1)
        self.assertFalse(self.w.automation.status(self.w.spec('p2'))['enabled'])
        self.assertNotIn('p2',self.w._pilots)
        state=api.get_state()
        self.assertEqual(state['online'],1)
        self.assertEqual(state['shops'][0]['state'],'store_monitoring')
        self.assertEqual(api._embedder.attached,[])
        self.assertTrue(api.stop_store('p1')['ok'])
        self.assertFalse(self.p.bound)

    def test_desktop_counts_only_confirmed_automatic_replies(self):
        self.f.worker().tick();self.w.send_live_draft('p1','300',True)
        state=self.desktop().get_state()
        self.assertEqual(state['shops'][0]['channel_runtime']['confirmed_deliveries'],1)
        self.assertEqual(state['shops'][0]['total_replies'],0)

    def test_desktop_reception_keeps_authorized_export_sync_without_child_window(self):
        from app.service.sync import SyncWorker
        self.enable()
        path=self.f.f.root/'approved-products.json'
        products=self.w.knowledge('p1').active('products')['payload'];products[0]['price']=159
        path.write_text(json.dumps(products),encoding='utf-8')
        self.w.sync('p1').register(str(path),auto_publish=True)
        sync=SyncWorker(self.w);api=self.desktop();api._workbench_api._sync_worker=sync
        try:
            self.assertTrue(api.start_store('p1')['ok'])
            self.assertIsNone(api._workbench_window)
            deadline=time.monotonic()+10
            while self.w.knowledge('p1').active('products')['payload'][0]['price']!=159 and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertEqual(self.w.knowledge('p1').active('products')['payload'][0]['price'],159)
            self.question()
            while self.w._channel_workers['p1'].confirmed<1 and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertEqual(self.w._channel_workers['p1'].confirmed,1)
            self.assertIn('159',self.latest_result()['draft_reply'])
        finally:
            sync.stop();self.w.stop_store_reception('p1')

    def test_stop_waits_for_inflight_model_then_disconnects_without_sending(self):
        self.enable();self.question()
        entered,release=threading.Event(),threading.Event()
        original=self.models.understand
        def understand(request):
            entered.set();release.wait(6);return original(request)
        with patch.object(self.models,'understand',side_effect=understand):
            self.w.start_channel_monitor('p1','store',[],True)
            try:
                self.assertTrue(entered.wait(2))
                result=self.desktop().stop_store('p1')
                self.assertTrue(result['stopping']);self.assertTrue(self.p.bound)
            finally:
                release.set();self.w._channel_workers['p1'].thread.join(4)
        self.w.channel_runtime()
        self.assertFalse(self.p.bound)
        self.assertEqual(self.cdp.send_calls,1)


if __name__=='__main__':unittest.main(verbosity=1)
