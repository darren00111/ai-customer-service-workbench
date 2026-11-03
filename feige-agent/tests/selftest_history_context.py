"""Passive history must enrich context without replaying traffic or receipts."""
import json
from pathlib import Path
import sys
import sqlite3
from contextlib import closing
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import selftest_channel_pilots as fixture
from app.channels.history_context import parse_history
from app.channels.pinduoduo import normalize_frame

BASE = 1800000000000


def raw(mid, role='buyer', text='看看黑杆', ts=BASE-1000, **changes):
    return json.loads(fixture.frame(mid, role, text, ts=ts, **changes))['message']


def messages(*rows):
    return parse_history({'success':True,'result':{'messages':list(rows)}}, fixture.ACCOUNT)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PilotTests(); self.f.setUp()
        self.w,self.p,self.cdp,self.spec = self.f.w,self.f.p,self.f.cdp,self.f.spec
        kb = self.w.knowledge('p1')
        kb.activate(kb.stage('products',[{'product_id':'A','title':'黑色台球杆','aliases':['黑杆'],
            'price':149,'stock':3,'platform_ids':{'pinduoduo':'11'}}], '构造资料'), reviewed=True)
        self.f.connect()
        self.cdp.push(fixture.frame('current',text='这个多少米',ts=BASE))
        self.p.poll()

    def tearDown(self):
        self.f.tearDown()

    def test_existing_workspace_schema_migrates_without_resetting_threads(self):
        from app.service.multichannel import WorkspaceData
        path=self.f.root/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE channel_threads(scope_key TEXT PRIMARY KEY,merchant_id TEXT,platform TEXT,store_id TEXT,conversation_id TEXT,paused INTEGER DEFAULT 0,draft TEXT DEFAULT '',draft_head INTEGER,updated_at TEXT)")
            db.execute("INSERT INTO channel_threads(scope_key,paused,draft) VALUES('legacy',1,'preserved')")
        migrated=WorkspaceData(path)
        with migrated.connect() as db:
            row=db.execute("SELECT * FROM channel_threads WHERE scope_key='legacy'").fetchone()
        self.assertEqual(row['context_revision'],0)
        self.assertEqual(row['draft'],'preserved')
        self.assertEqual(row['paused'],1)

    def ingest(self,*rows):
        return self.w.history.ingest(self.spec,fixture.ACCOUNT.key,messages(*rows))

    def card(self,ts=BASE-1000):
        return raw('old-card',text='',ts=ts,sub_type=0,info={'goodsID':'11'})

    def test_history_card_resolves_current_question_without_becoming_live_head(self):
        head = self.w.live.head(self.spec,'300')
        self.ingest(self.card(),raw('old-agent','agent','您可以看看这一款',BASE-500))
        result = self.w.generate_imported_draft('p1','300')
        self.assertIn('149',result['draft_reply'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.w.live.head(self.spec,'300')['sequence'],head['sequence'])
        self.assertEqual(self.w.live.head(self.spec,'300')['message_id'],'current')
        self.assertEqual(self.p.eligible_heads['300'],'current')
        self.assertFalse(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(len(self.w.imported_conversation('p1','300')['observed_history']),2)

    def test_repeat_page_preserves_new_draft_and_revision(self):
        self.ingest(self.card())
        self.w.generate_imported_draft('p1','300')
        before = self.w.inbox.get(self.spec,'300')
        self.assertEqual(self.ingest(self.card()),0)
        after = self.w.inbox.get(self.spec,'300')
        self.assertEqual(before['context_revision'],after['context_revision'])
        self.assertEqual(before['draft'],after['draft'])

    def test_history_replay_on_socket_does_not_replace_live_head_or_trigger_reply(self):
        self.ingest(raw('known-old',text='之前的问题'))
        self.cdp.push(fixture.frame('known-old',text='之前的问题',ts=BASE-1000))
        self.assertEqual(self.p.poll()['inserted'],0)
        self.assertEqual(self.w.live.head(self.spec,'300')['message_id'],'current')
        self.assertEqual(self.p.eligible_heads['300'],'current')

    def test_socket_history_id_conflict_stops_before_recording_bad_message(self):
        self.ingest(raw('known-old',text='之前的问题'))
        self.cdp.push(fixture.frame('known-old',text='伪造的新正文',ts=BASE-1000))
        with self.assertRaisesRegex(ValueError,'历史冲突'):self.p.poll()
        self.assertEqual(self.w.live.head(self.spec,'300')['message_id'],'current')
        self.assertFalse(self.p.bound)

    def test_newer_history_reveals_a_stream_gap_and_pauses_without_promoting_it(self):
        self.ingest(raw('missed-human','agent','人工已处理',BASE+1000))
        self.assertTrue(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.w.live.head(self.spec,'300')['message_id'],'current')
        self.assertEqual(self.w.live.history(self.spec,'300'),[])

    def test_history_alone_does_not_create_new_reply_targets(self):
        row = raw('another-customer',**{'from':{'role':'user','uid':'301'}})
        self.ingest(row)
        self.assertFalse(self.w.live.is_live(self.spec,'301'))
        self.assertNotIn('301',self.p.eligible_heads)
        with self.assertRaises(ValueError):self.w.inbox.get(self.spec,'301')

    def test_history_does_not_confirm_a_pending_delivery(self):
        self.f.draft()
        head = self.w.live.head(self.spec,'300')['sequence']
        token = self.w.live.reserve(self.spec,fixture.ACCOUNT.key,'300',head,'这款149元。')
        self.w.live.finish(token,'awaiting_echo','history-echo')
        self.ingest(raw('history-echo','agent','这款149元。',BASE+1000))
        self.assertEqual(self.w.live.delivery(token)['state'],'awaiting_echo')
        self.assertFalse(self.w.live.delivery(token)['sent'])
        # Independent, matching live echo still repairs a receipt after history.
        self.cdp.push(fixture.frame('history-echo','agent','这款149元。',ts=BASE+1000))
        self.p.poll()
        self.assertTrue(self.w.live.delivery(token)['sent'])

    def test_manual_unpause_cannot_make_the_old_head_sendable_after_a_gap(self):
        self.ingest(raw('missing-new-question',text='另一个问题',ts=BASE+1000))
        self.w.inbox.pause(self.spec,'300',False)
        thread=self.w.inbox.get(self.spec,'300')
        self.w.inbox.save_draft(self.spec,'300',thread['messages'][-1]['sequence'],
            {'action':'reply','draft_reply':'这款149元。','context_revision':thread['context_revision']})
        with self.assertRaisesRegex(ValueError,'可回复的实时'):
            self.w.live.reserve(self.spec,fixture.ACCOUNT.key,'300',self.w.live.head(self.spec,'300')['sequence'],'这款149元。')

    def test_live_id_conflict_rolls_back_the_entire_history_page(self):
        with self.assertRaisesRegex(ValueError,'冲突'):
            self.ingest(self.card(),raw('current',text='不同正文',ts=BASE))
        self.assertEqual(self.w.history.list(self.spec,'300'),[])
        self.assertEqual(self.w.inbox.get(self.spec,'300')['context_revision'],0)

    def test_other_store_cannot_read_or_write_this_stores_history(self):
        self.ingest(self.card())
        other = self.w.spec('p2')
        self.assertEqual(self.w.history.list(other,'300'),[])
        with self.assertRaises(ValueError):self.w.history.ingest(other,fixture.ACCOUNT.key,messages(self.card()))

    def test_history_parser_rejects_wrong_account_mixed_buyers_and_conflicting_ids(self):
        cases = [raw('other-account',to={'role':'mall_cs','uid':'999'})]
        with self.assertRaises(ValueError):messages(*cases)
        with self.assertRaises(ValueError):messages(raw('a'),raw('b',**{'from':{'role':'user','uid':'301'}}))
        with self.assertRaises(ValueError):messages(raw('a'),raw('a',text='冲突'))
        with self.assertRaisesRegex(ValueError,'同刻消息顺序'):messages(raw('a'),raw('b'))

    def candidate_rows(self):
        secret='sk-'+'x'*32
        return messages(self.card(BASE-3000),raw('care-q',text='这个怎么保养',ts=BASE-2000),
                        raw('care-a','agent','用干布擦拭，手机号13812345678，API key: '+secret,BASE-1000))

    def test_observed_history_creates_redacted_review_candidates_without_activation(self):
        rows=self.candidate_rows()
        self.w.history.ingest(self.spec,fixture.ACCOUNT.key,rows)
        revisions=self.w.stage_observed_history('p1',rows)
        self.assertEqual(len(revisions),1)
        kb=self.w.knowledge('p1')
        self.assertIsNone(kb.active('faq'))
        with kb.connect() as db:
            row=db.execute('SELECT payload,active FROM revisions WHERE id=?',(revisions[0],)).fetchone()
        payload=json.loads(row['payload'])
        self.assertEqual(row['active'],0)
        self.assertTrue(payload[0]['review_required'])
        self.assertEqual(payload[0]['product_ids'],['A'])
        self.assertIn('[凭证]',payload[0]['answer'])
        self.assertNotIn('13812345678',payload[0]['answer'])
        self.assertNotIn('x'*32,payload[0]['answer'])
        self.assertEqual(self.w.stage_observed_history('p1',rows),revisions)

    def test_unobserved_or_altered_history_cannot_create_candidates(self):
        rows=self.candidate_rows()
        with self.assertRaisesRegex(ValueError,'已观察原文'):self.w.stage_observed_history('p1',rows)
        self.w.history.ingest(self.spec,fixture.ACCOUNT.key,rows)
        from dataclasses import replace
        altered=[replace(rows[0],product_id='999'),*rows[1:]]
        with self.assertRaisesRegex(ValueError,'已观察原文'):self.w.stage_observed_history('p1',altered)
        with self.assertRaises(ValueError):self.w.stage_observed_history('p2',rows)

    def test_unparsed_media_does_not_join_unrelated_question_and_answer(self):
        rows=messages(raw('before-picture',text='这个怎么样',ts=BASE-3000),
                      raw('picture',text='',ts=BASE-2000,type=1),raw('after-picture','agent','照片看起来可以',BASE-1000))
        self.w.history.ingest(self.spec,fixture.ACCOUNT.key,rows)
        self.assertEqual(self.w.stage_observed_history('p1',rows),[])

    def test_passive_capture_automatically_stages_once_but_does_not_activate(self):
        payload=json.dumps({'success':True,'result':{'messages':[self.card(BASE-3000),
            raw('auto-q',text='这个怎么保养',ts=BASE-2000),raw('auto-a','agent','干布擦拭',BASE-1000)]}})
        self.cdp.call=lambda *args,**kwargs:{'body':payload}
        self.p.history.on_history=lambda rows:self.w.stage_observed_history('p1',rows)
        for rid in ('page1','page2'):
            self.p.history.stream.publish({'method':'Network.responseReceived','params':{'requestId':rid,'type':'Fetch',
                'response':{'url':'https://mms.pinduoduo.com/plateau/chat/list','status':200,'mimeType':'application/json'}}})
            self.p.history.stream.publish({'method':'Network.loadingFinished','params':{'requestId':rid}})
            self.p.poll()
        status=self.p.status()['history_capture']
        self.assertEqual(status['pages'],2)
        self.assertEqual(status['candidate_versions'],1)
        self.assertFalse(status['learning_error'])
        self.assertIsNone(self.w.knowledge('p1').active('faq'))

    def test_candidate_failure_keeps_context_and_reports_error_without_stopping_messages(self):
        self.cdp.call=lambda *args,**kwargs:{'body':json.dumps({'success':True,'result':{'messages':[self.card()]}})}
        def fail(rows):raise OSError('fixture learning unavailable')
        self.p.history.on_history=fail
        self.p.history.stream.publish({'method':'Network.responseReceived','params':{'requestId':'page','type':'Fetch',
            'response':{'url':'https://mms.pinduoduo.com/plateau/chat/list','status':200,'mimeType':'application/json'}}})
        self.p.history.stream.publish({'method':'Network.loadingFinished','params':{'requestId':'page'}})
        self.p.poll()
        self.assertTrue(self.p.bound)
        self.assertTrue(self.p.status()['history_capture']['learning_error'])
        self.assertIn('149',self.w.generate_imported_draft('p1','300')['draft_reply'])

    def test_history_is_sorted_and_exact_duplicates_do_not_repeat(self):
        result = messages(raw('late',ts=BASE-100),raw('early',ts=BASE-200),raw('early',ts=BASE-200))
        self.assertEqual([m.message_id for m in result],['early','late'])

    def test_old_card_outside_session_gap_cannot_revive_old_product(self):
        self.ingest(self.card(BASE-7200000))
        result = self.w.generate_imported_draft('p1','300')
        self.assertNotIn('149',result['draft_reply'])
        self.assertIsNone(result['state'].get('product_id'))

    def test_live_card_also_expires_after_inactivity(self):
        self.cdp.push(fixture.frame('new-card',text='',ts=BASE+1000,sub_type=0,info={'goodsID':'11'}))
        self.p.poll()
        self.cdp.push(fixture.frame('next-day',text='这个多少米',ts=BASE+86400000))
        self.p.poll()
        result = self.w.generate_imported_draft('p1','300')
        self.assertNotIn('149',result['draft_reply'])

    def test_history_arriving_during_model_call_cancels_the_old_draft(self):
        test = self
        class Model:
            name='synthetic_history_race'
            def status(self):return {'enabled':True,'connected':True,'configured':True,'model':'fixture'}
            def interpreter(self):return self
            def understand(self,request):
                test.ingest(test.card())
                return {'intents':['price'],'product_id':'A'}
        self.w.models = Model()
        with self.assertRaisesRegex(ValueError,'会话已更新'):
            self.w.generate_imported_draft('p1','300')
        self.assertIsNone(self.w.inbox.get(self.spec,'300')['draft'])

    def test_passive_history_reads_only_exact_json_endpoint_after_binding(self):
        calls = []
        payload = json.dumps({'success':True,'result':{'messages':[self.card()]}})
        def call(method,params,**kwargs):
            calls.append(method)
            self.assertEqual(method,'Network.getResponseBody')
            return {'body':payload,'base64Encoded':False}
        self.cdp.call = call
        stream = self.p.history.stream
        for rid,url in [('wrong','https://mms.pinduoduo.com.evil.test/plateau/chat/list'),
                        ('right','https://mms.pinduoduo.com/plateau/chat/list')]:
            stream.publish({'method':'Network.responseReceived','params':{'requestId':rid,'type':'Fetch',
                'response':{'url':url,'status':200,'mimeType':'application/json'}}})
            stream.publish({'method':'Network.loadingFinished','params':{'requestId':rid,'encodedDataLength':len(payload)}})
        self.p.poll()
        self.assertEqual(calls,['Network.getResponseBody'])
        self.assertIn('149',self.w.generate_imported_draft('p1','300')['draft_reply'])

    def test_unrecognized_history_response_stops_instead_of_inventing_roles(self):
        self.cdp.call = lambda *args,**kwargs:{'body':'{"success":true,"result":{"messages":[{}]}}'}
        stream = self.p.history.stream
        stream.publish({'method':'Network.responseReceived','params':{'requestId':'bad','type':'XHR',
            'response':{'url':'https://mms.pinduoduo.com/plateau/chat/list','status':200,'mimeType':'application/json'}}})
        stream.publish({'method':'Network.loadingFinished','params':{'requestId':'bad'}})
        with self.assertRaisesRegex(ValueError,'身份或消息标识'):self.p.poll()
        self.assertFalse(self.p.bound)

    def test_worker_rebuilds_unchanged_live_head_after_context_update(self):
        import selftest_channel_worker as workers
        from app.channels.reception_worker import ChannelReceptionWorker
        f = workers.WorkerTests(); f.setUp()
        try:
            worker = ChannelReceptionWorker(f.w,'p1','draft')
            worker.tick(); count = f.models.calls
            worker.tick();self.assertEqual(f.models.calls,count)
            f.w.history.ingest(f.spec,fixture.ACCOUNT.key,messages(raw('earlier',ts=BASE-1000)))
            worker.tick();self.assertEqual(f.models.calls,count+1)
        finally:
            f.tearDown()


if __name__=='__main__':unittest.main(verbosity=1)
