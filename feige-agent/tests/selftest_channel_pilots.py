"""Synthetic protocol/account/transport tests; no real merchant API calls."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.channels.pinduoduo import Account, Incoming, PinduoduoPilot, normalize_frame
from app.channels.ledger import LiveLedger
from app.engine.cdp import EventStream, CDPClient
from app.config import StoreRegistry
from app.service.workbench import Workbench
from app.service.workbench_api import WorkbenchAPI

ACCOUNT = Account('100', '200', '构造的拼多多测试店')


def frame(mid='m1', role='buyer', text='黑杆多少钱', **changes):
    msg = {'msg_id': mid, 'from': {'role': 'user', 'uid': '300'},
           'to': {'role': 'mall_cs', 'uid': '200'}, 'type': 0, 'content': text, 'ts': 1800000000000}
    if role == 'agent':
        msg['from'], msg['to'] = msg['to'], msg['from']
    msg.update(changes)
    return json.dumps({'response': 'push', 'message': msg}, ensure_ascii=False)


class FakeCDP:
    """Captured-schema fixture, with controllable identity and response ordering."""
    def __init__(self, port=1):
        self.alive, self.origin, self.account = False, 'https://mms.pinduoduo.com', ACCOUNT
        self.s = None
        self.send_calls = 0
        self.send_response = {'success': True, 'result': {'msg_id': 'out1'}}
        self.fail_send = False
        self.echo_id = 'out1'
        self.targets = [{'id': 'fixture', 'type': 'page', 'url': 'https://mms.pinduoduo.com/chat-merchant/index.html'}]

    def list_targets(self): return self.targets
    def connect_target(self, target): self.alive = True
    def is_alive(self): return self.alive
    def close(self): self.alive = False
    def subscribe(self, methods):
        stream = EventStream(methods)
        if 'Network.webSocketFrameReceived' in methods:self.s=stream
        return stream
    def call(self, method, params): return {}
    def evaluate(self, expression, **kwargs):
        if expression == 'location.origin': return self.origin
        if '/janus/api/new/userinfo' in expression:
            return {'success': True, 'result': {'id': self.account.user_id, 'mall_id': self.account.mall_id}}
        if '/earth/api/merchant/' in expression:
            return {'success': True, 'result': {'mallId': self.account.mall_id, 'mallName': self.account.name}}
        if '/plateau/chat/send_message' in expression:
            self.send_calls += 1
            if self.echo_id:
                self.push(frame(self.echo_id, 'agent', '这款149元。', ts=1800000001000))
            if self.fail_send:
                raise TimeoutError('fixture response interrupted')
            return self.send_response
        raise AssertionError('Unexpected browser operation')
    def socket(self, url='wss://m-ws.pinduoduo.com/?access_token=PRIVATE-FIXTURE'):
        self.s.publish({'method': 'Network.webSocketCreated', 'params': {'requestId': 'socket1', 'url': url}})
    def push(self, text):
        self.s.publish({'method': 'Network.webSocketFrameReceived', 'params': {'requestId': 'socket1', 'response': {'opcode': 1, 'payloadData': text}}})


class ParsingTests(unittest.TestCase):
    def test_buyer_and_agent_roles_derive_from_both_parties(self):
        buyer, _ = normalize_frame(frame(), ACCOUNT)
        agent, _ = normalize_frame(frame(role='agent'), ACCOUNT)
        self.assertEqual((buyer.conversation_id, buyer.role, agent.role), ('300', 'buyer', 'agent'))

    def test_account_mismatch_is_not_a_buyer_question(self):
        msg, reason = normalize_frame(frame(to={'role': 'mall_cs', 'uid': '999'}), ACCOUNT)
        self.assertIsNone(msg); self.assertEqual(reason, 'other_account')

    def test_unknown_roles_missing_ids_and_shapes_never_trigger_replies(self):
        for raw in ['[]', '{}', 'bad', frame(msg_id=None), frame(**{'from': {'role': 'robot', 'uid': '300'}}), frame(ts=None)]:
            self.assertIsNone(normalize_frame(raw, ACCOUNT)[0])

    def test_card_media_retraction_and_transfer_block_stale_drafts(self):
        for change in [{'type': 8}, {'type': 1}, {'type': 1002}, {'type': 24}, {'sub_type': 0}, {'type': 999}]:
            msg, _ = normalize_frame(frame(**change), ACCOUNT)
            self.assertEqual(msg.action, 'pause')

    def test_duplicate_text_still_has_distinct_platform_ids(self):
        a, _ = normalize_frame(frame('1'), ACCOUNT); b, _ = normalize_frame(frame('2'), ACCOUNT)
        self.assertEqual(a.content, b.content); self.assertNotEqual(a.message_id, b.message_id)

    def test_timestamp_units_and_text_bounds(self):
        msg, _ = normalize_frame(frame(ts=1800000000), ACCOUNT)
        self.assertEqual(msg.timestamp, 1800000000000)
        self.assertIsNone(normalize_frame(frame(text='x' * 8001), ACCOUNT)[0])

    def test_event_overflow_is_observable_without_blocking_cdp_replies(self):
        stream = EventStream(['x'], capacity=1)
        stream.publish({'method': 'other'})
        stream.publish({'method': 'x'}); stream.publish({'method': 'x'})
        self.assertTrue(stream.overflowed); self.assertEqual(stream.events.qsize(), 1)

    def test_event_queue_bounds_total_bytes_and_releases_consumed_memory(self):
        stream = EventStream(['x'], byte_capacity=120)
        item = {'method':'x','body':'a'*55}
        stream.publish(item); stream.publish(item)
        self.assertTrue(stream.overflowed)
        self.assertEqual(stream.events.qsize(),1)
        self.assertEqual(stream.events.get_nowait(),item)
        self.assertEqual(stream.events.buffered_bytes,0)
        stream.publish(item)
        self.assertEqual(stream.events.qsize(),1)


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.w = Workbench(self.root/'reception', self.root/'stores', StoreRegistry(self.root/'registry.json', self.root/'stores'))
        for key in ('p1', 'p2'):
            self.w.create_store(key, key, platform='pinduoduo')
        self.spec = self.w.spec('p1')
        self.cdp = FakeCDP()
        self.p = PinduoduoPilot(self.spec, self.root/'profiles', self.w.live, cdp_factory=lambda _: self.cdp)
        self.w._pilots['p1'] = self.p

    def tearDown(self):
        self.w.close_connections(); self.temp.cleanup()

    def connect(self):
        self.p.inspect(); self.p.bind(ACCOUNT.key, True); self.cdp.socket()

    def ingest(self, **kwargs):
        self.cdp.push(frame(**kwargs)); return self.p.poll()

    def draft(self):
        thread = self.w.inbox.get(self.spec, '300')
        self.w.inbox.save_draft(self.spec, '300', thread['messages'][-1]['sequence'],
                               {'action': 'reply', 'draft_reply': '这款149元。', 'knowledge_versions': self.w.service('p1', offline=True).versions()})
        return self.w.inbox.get(self.spec, '300')

    def test_login_uses_isolated_shop_profile_and_does_not_start_auto_reply(self):
        with patch('app.channels.pinduoduo.browser.ensure_browser') as launch:
            self.p.open_login()
        self.assertEqual(launch.call_args.args[1], self.root/'profiles'/'p1')
        self.assertFalse(self.p.status()['automatic_sending'])

    def test_binding_requires_current_reviewed_account(self):
        self.p.inspect()
        for key, reviewed in [(ACCOUNT.key, False), ('100:999', True)]:
            with self.assertRaises(ValueError): self.p.bind(key, reviewed)
        self.assertFalse(self.p.bound)

    def test_first_login_observes_socket_before_navigation_and_retains_it_at_inspect(self):
        self.cdp.targets[0]['url'] = 'about:blank'
        calls = []
        original_call = self.cdp.call
        def call(method, params):
            calls.append(method)
            if method == 'Page.navigate':
                self.assertIsNotNone(self.cdp.s)
                self.assertIn('Network.enable', calls)
                self.cdp.targets[0]['url'] = params['url']
                self.cdp.socket()
                self.cdp.push(frame('early-question'))
            return original_call(method, params)
        self.cdp.call = call
        with patch('app.channels.pinduoduo.browser.ensure_browser', return_value={'status':'launched'}) as launch:
            self.p.open_login()
        self.assertEqual(launch.call_args.args[2], 'about:blank')
        stream = self.cdp.s
        self.assertFalse(self.p.bound)
        self.assertEqual(self.w.inbox.list(self.spec), [])
        self.p.inspect()
        self.assertIs(self.cdp.s, stream)
        self.p.bind(ACCOUNT.key, True)
        self.assertEqual(self.p.poll()['inserted'], 1)
        self.assertEqual(self.w.live.head(self.spec, '300')['message_id'], 'early-question')
        self.assertNotIn('300', self.p.eligible_heads)
        self.cdp.push(frame('after-bind', ts=1800000001000))
        self.assertEqual(self.p.poll()['inserted'], 1)
        self.assertEqual(self.p.eligible_heads['300'], 'after-bind')
        self.assertEqual(calls.count('Network.enable'), 1)
        self.assertEqual(calls.count('Page.navigate'), 1)

    def prepare_login(self):
        with patch('app.channels.pinduoduo.browser.ensure_browser', return_value={'status':'reused'}):
            self.p.open_login()

    def test_prepared_login_does_not_follow_replaced_chat_tab(self):
        self.prepare_login()
        self.cdp.targets[0]['id'] = 'different-tab'
        with self.assertRaisesRegex(ValueError, '其他标签'):
            self.p.inspect()
        self.assertFalse(self.p.bound)
        self.assertFalse(self.cdp.alive)
        self.assertIsNone(self.p.lease.handle)

    def test_login_socket_replacement_and_old_human_reply_are_context_not_new_controls(self):
        self.prepare_login()
        self.cdp.socket()
        self.cdp.push(frame('prior-agent', 'agent', '此前人工回复'))
        self.cdp.s.publish({'method':'Network.webSocketClosed','params':{'requestId':'socket1'}})
        self.cdp.socket()
        self.p.inspect(); self.p.bind(ACCOUNT.key, True)
        self.cdp.push(frame('after-login', ts=1800000001000))
        self.assertEqual(self.p.poll()['inserted'], 2)
        self.assertTrue(self.p.bound)
        self.assertFalse(self.w.inbox.get(self.spec, '300')['paused'])
        self.assertEqual(self.p.eligible_heads['300'], 'after-login')

    def test_socket_closed_after_binding_still_stops_even_with_prelogin_events_queued(self):
        self.prepare_login(); self.cdp.socket()
        self.p.inspect(); self.p.bind(ACCOUNT.key, True)
        self.cdp.s.publish({'method':'Network.webSocketClosed','params':{'requestId':'socket1'}})
        with self.assertRaisesRegex(ValueError, '通道已中断'):
            self.p.poll()
        self.assertFalse(self.p.bound)

    def test_prepared_login_overflow_cannot_silently_discard_questions(self):
        self.prepare_login()
        self.cdp.s.overflowed = True
        with self.assertRaisesRegex(ValueError, '缓冲已满'):
            self.p.inspect()
        self.assertEqual(self.w.inbox.list(self.spec), [])
        self.assertIsNone(self.p.lease.handle)

    def test_reinspect_bound_login_invalidates_drafts_instead_of_reusing_prelogin_state(self):
        self.prepare_login()
        self.p.inspect(); self.p.bind(ACCOUNT.key, True)
        self.cdp.socket(); self.ingest(); self.draft()
        stream = self.cdp.s
        self.p.inspect()
        self.assertIsNot(self.cdp.s, stream)
        self.assertFalse(self.p.bound)
        self.assertFalse(self.w.inbox.get(self.spec, '300').get('draft'))

    def test_failed_first_navigation_releases_the_connection(self):
        self.cdp.targets[0]['url'] = 'about:blank'
        original_call = self.cdp.call
        def call(method, params):
            if method == 'Page.navigate':
                raise OSError('fixture navigation failed')
            return original_call(method, params)
        self.cdp.call = call
        with patch('app.channels.pinduoduo.browser.ensure_browser', return_value={'status':'launched'}):
            with self.assertRaises(OSError): self.p.open_login()
        self.assertFalse(self.cdp.alive)
        self.assertIsNone(self.p.lease.handle)
        self.assertFalse(self.p.status()['automatic_sending'])

    def test_http_chat_is_not_a_trusted_login_target(self):
        self.cdp.targets[0]['url'] = 'http://mms.pinduoduo.com/chat-merchant/index.html'
        with patch('app.channels.pinduoduo.browser.ensure_browser', return_value={'status':'reused'}):
            with self.assertRaises(ValueError): self.p.open_login()
        self.assertFalse(self.cdp.alive)

    def test_reused_merchant_login_page_can_prepare_but_cannot_bind_as_chat(self):
        self.cdp.targets[0]['url'] = 'https://mms.pinduoduo.com/login'
        self.prepare_login()
        self.assertEqual(self.p.state, 'login_required')
        self.assertFalse(self.p.bound)
        self.cdp.targets[0]['url'] = 'https://mms.pinduoduo.com/chat-merchant/index.html'
        self.cdp.socket()
        self.p.inspect(); self.p.bind(ACCOUNT.key, True)
        self.cdp.push(frame('after-login'))
        self.assertEqual(self.p.poll()['inserted'], 1)
        self.assertEqual(self.p.eligible_heads['300'], 'after-login')

    def test_different_remote_shop_cannot_overwrite_existing_binding(self):
        self.connect(); self.p.close(); self.cdp.account = Account('101', '200', '其他店')
        self.p.inspect()
        with self.assertRaises(ValueError): self.p.bind('101:200', True)

    def test_multiple_or_wrong_origin_tabs_refused(self):
        for targets in [[], self.cdp.targets * 2, [{'type': 'page', 'url': 'https://mms.pinduoduo.com.evil.test/chat-merchant/'}]]:
            self.cdp.targets = targets
            with self.assertRaises(ValueError): self.p.inspect()
            self.assertFalse(self.cdp.alive)

    def test_identity_switch_stops_before_ingestion(self):
        self.connect(); self.cdp.push(frame()); self.cdp.account = Account('999', '200', '他店')
        with self.assertRaises(ValueError): self.p.poll()
        self.assertEqual(self.w.inbox.list(self.spec), [])
        self.assertFalse(self.p.bound)

    def test_redirect_or_logout_stops_before_ingestion(self):
        self.connect(); self.cdp.origin = 'https://passport.pinduoduo.com'
        with self.assertRaises(ValueError): self.p.poll()
        self.assertFalse(self.p.bound)

    def test_exact_socket_host_only_and_no_credentials_in_public_status(self):
        self.connect(); self.cdp.socket('wss://m-ws.pinduoduo.com.evil.test/?x=y')
        self.cdp.push(frame()); self.p.poll()
        self.assertEqual(self.w.inbox.list(self.spec), [])
        self.assertNotIn('PRIVATE-FIXTURE', json.dumps(self.p.status()))

    def test_live_messages_deduplicate_and_do_not_cross_stores(self):
        self.connect(); self.assertEqual(self.ingest()['inserted'], 1)
        self.draft(); self.assertEqual(self.ingest()['inserted'], 0)
        self.assertTrue(self.w.inbox.get(self.spec, '300')['draft'])
        self.assertEqual(self.w.inbox.list(self.w.spec('p2')), [])

    def test_new_message_invalidates_draft_even_when_same_text(self):
        self.connect(); self.ingest(); self.draft(); self.ingest(mid='m2', ts=1800000001000)
        self.assertIsNone(self.w.inbox.get(self.spec, '300')['draft'])

    def test_human_reply_cancels_draft_and_pauses(self):
        self.connect(); self.ingest(); self.draft(); self.ingest(mid='human', role='agent', text='人工答复')
        row = self.w.inbox.get(self.spec, '300')
        self.assertTrue(row['paused']); self.assertIsNone(row['draft'])

    def test_out_of_order_event_and_media_pause_the_conversation(self):
        self.connect(); self.ingest(); self.ingest(mid='older', ts=1700000000000)
        self.assertTrue(self.w.inbox.get(self.spec, '300')['paused'])
        with self.assertRaises(ValueError): self.w.generate_imported_draft('p1', '300')

    def test_imported_history_is_never_promoted_to_live_history(self):
        self.w.import_conversations('p1', json.dumps({'store_id': 'p1', 'conversations': [{'conversation_id': '300', 'messages': [{'message_id': 'imported', 'role': 'buyer', 'content': '导入文字'}]}]}))
        self.connect()
        with self.assertRaises(ValueError): self.ingest()
        self.assertFalse(self.w.live.is_live(self.spec, '300'))

    def test_manual_import_cannot_contaminate_live_history(self):
        self.connect(); self.ingest()
        with self.assertRaises(ValueError):
            self.w.import_conversations('p1', json.dumps({'store_id':'p1','conversations':[{'conversation_id':'300','messages':[{'message_id':'fake','role':'buyer','content':'导出记录'}]}]}))
        self.assertEqual(len(self.w.inbox.get(self.spec, '300')['messages']), 1)

    def test_unknown_and_overflow_stop_instead_of_losing_messages_silently(self):
        self.connect(); self.cdp.push('malformed')
        with self.assertRaises(ValueError): self.p.poll()
        self.assertFalse(self.p.bound)
        self.connect(); self.cdp.s.overflowed = True
        with self.assertRaises(ValueError): self.p.poll()

    def test_only_reviewed_live_draft_can_send(self):
        self.connect(); self.ingest(); self.draft()
        with self.assertRaises(ValueError): self.w.send_live_draft('p1', '300', False)
        self.assertEqual(self.cdp.send_calls, 0)
        row = self.draft()
        with self.assertRaises(ValueError): self.p.send_reviewed('300', row['draft_head'], '擅自修改的话术')
        self.assertEqual(self.cdp.send_calls, 0)

    def test_confirmed_send_requires_exact_remote_echo(self):
        self.connect(); self.ingest(); self.draft()
        result = self.w.send_live_draft('p1', '300', True)
        self.assertTrue(result['sent']); self.assertEqual(result['state'], 'confirmed')
        self.assertEqual(self.cdp.send_calls, 1)
        self.assertFalse(self.w.inbox.get(self.spec, '300')['paused'])

    def test_same_text_with_different_message_id_cannot_confirm_send(self):
        self.connect(); self.ingest(); self.draft(); self.cdp.echo_id = 'manual-message'
        result = self.w.send_live_draft('p1', '300', True)
        self.assertFalse(result['sent']); self.assertTrue(self.w.inbox.get(self.spec, '300')['paused'])

    def test_timeout_is_durable_unknown_and_never_automatically_retried(self):
        self.connect(); self.ingest(); row = self.draft(); self.cdp.fail_send = True; self.cdp.echo_id = None
        result = self.w.send_live_draft('p1', '300', True)
        self.assertEqual(result['state'], 'delivery_unknown'); self.assertEqual(self.cdp.send_calls, 1)
        self.w.inbox.pause(self.spec, '300', False); self.draft()
        with self.assertRaises(ValueError): self.p.send_reviewed('300', row['draft_head'], '这款149元。')
        self.assertEqual(self.cdp.send_calls, 1)

    def test_http_success_without_platform_id_is_not_confirmed(self):
        self.connect(); self.ingest(); self.draft(); self.cdp.echo_id = None
        self.cdp.send_response = {'success': True, 'result': {}}
        self.assertEqual(self.w.send_live_draft('p1', '300', True)['state'], 'delivery_unknown')

    def test_platform_nested_error_is_not_confirmed(self):
        self.connect(); self.ingest(); self.draft(); self.cdp.echo_id = None
        self.cdp.send_response = {'success': True, 'result': {'error_code': 10002}}
        self.assertFalse(self.w.send_live_draft('p1', '300', True)['sent'])

    def test_new_buyer_message_before_send_aborts(self):
        self.connect(); self.ingest(); self.draft(); self.cdp.push(frame('new', ts=1800000001000))
        with self.assertRaises(ValueError): self.w.send_live_draft('p1', '300', True)
        self.assertEqual(self.cdp.send_calls, 0)

    def test_knowledge_update_before_send_aborts(self):
        self.connect(); self.ingest(); self.draft()
        kb = self.w.knowledge('p1')
        kb.activate(kb.stage('products', [{'product_id': 'A', 'title': '台球杆', 'price': 169}], '构造资料变更'), reviewed=True)
        with self.assertRaises(ValueError): self.w.send_live_draft('p1', '300', True)
        self.assertEqual(self.cdp.send_calls, 0)

    def test_crash_after_reservation_blocks_replay(self):
        self.connect(); self.ingest(); row = self.draft()
        token = self.w.live.reserve(self.spec, ACCOUNT.key, '300', row['draft_head'], '这款149元。')
        reopened = LiveLedger(self.w._workspace)
        self.assertEqual(reopened.delivery(token)['state'], 'attempting')
        # Opening a view must not alter the live owner's in-flight send.
        self.p.close(); self.p.inspect()
        self.assertEqual(reopened.delivery(token)['state'], 'delivery_unknown')
        with self.assertRaises(ValueError): reopened.reserve(self.spec, ACCOUNT.key, '300', row['draft_head'], '这款149元。')

    def test_second_connection_cannot_pause_or_take_over_existing_owner(self):
        self.connect();self.ingest();self.draft()
        other = PinduoduoPilot(self.spec,self.root/'profiles',self.w.live,cdp_factory=lambda _:FakeCDP())
        try:
            with self.assertRaises(ValueError):other.inspect()
            other.close()
            self.assertTrue(self.p.bound)
            self.assertFalse(self.w.inbox.get(self.spec,'300')['paused'])
            self.assertTrue(self.w.inbox.get(self.spec,'300')['draft'])
            self.p.close()
            self.assertEqual(other.inspect()['state'],'awaiting_confirmation')
        finally:other.close()

    def test_reconnect_cannot_send_old_draft_without_observing_a_new_consultation(self):
        self.connect(); self.ingest(); self.draft(); self.p.close(); self.connect()
        self.assertTrue(self.w.inbox.get(self.spec, '300')['paused'])
        self.w.inbox.pause(self.spec, '300', False); row = self.draft()
        with self.assertRaises(ValueError): self.p.send_reviewed('300', row['draft_head'], '这款149元。')
        self.assertEqual(self.cdp.send_calls, 0)

    def test_socket_disconnect_invalidates_all_pending_drafts(self):
        self.connect(); self.ingest(); self.draft()
        self.cdp.s.publish({'method':'Network.webSocketClosed','params':{'requestId':'socket1'}})
        with self.assertRaises(ValueError): self.p.poll()
        row = self.w.inbox.get(self.spec, '300')
        self.assertTrue(row['paused']); self.assertIsNone(row['draft'])

    def test_closed_cdp_cannot_remain_marked_as_bound(self):
        self.connect(); self.ingest(); self.cdp.alive = False
        with self.assertRaises(ValueError): self.p.poll()
        self.assertFalse(self.p.bound)

    def test_taobao_probe_is_readonly_and_not_a_production_connection(self):
        self.w.create_store('tb', '淘宝测试', platform='taobao')
        with patch('app.channels.taobao.inspect_qianniu', return_value={'state': 'mapping_required', 'automatic_sending': False}) as probe:
            result = WorkbenchAPI(self.w).pilot_command('tb', 'inspect_qianniu')
        probe.assert_called_once(); self.assertTrue(result['ok'])
        self.assertFalse(self.w.info('tb')['connection']['runtime_available'])
        self.assertEqual(type(self.w._pilot('tb')).__name__, 'QianniuPilot')


if __name__ == '__main__':
    unittest.main(verbosity=2)
