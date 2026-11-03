"""Native-client contracts with a fake UI transport; no real Windows UI access."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.channels.qianniu_native import NativeSnapshot, NativeCompatibilityError, QianniuNative, parse_transcript
from app.channels.taobao import QianniuPilot
from app.config import StoreRegistry
from app.service.workbench import Workbench

NOW = datetime(2026, 11, 2, 12, 30, tzinfo=timezone(timedelta(hours=8)))
SELLER = '测试店:小一'
BUYER = '测试买家甲'
BASE = f'{BUYER} 2026-11-02 12:00:01\n黑杆多少钱'


class LauncherTests(unittest.TestCase):
    def setUp(self):
        from app.channels import qianniu_launcher
        self.launcher = qianniu_launcher
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = self.root / '千牛 带空格' / 'AliWorkbench.exe'
        self.client.parent.mkdir(); self.client.write_bytes(b'fixture, never executed')
        self.config = self.root / 'client.json'

    def test_launcher_needs_no_uia_or_logged_in_reception_window(self):
        with patch.object(QianniuNative, 'windows', side_effect=AssertionError('must not inspect UI')), \
             patch.object(self.launcher, 'discover', return_value=self.client), \
             patch.object(self.launcher.subprocess, 'Popen') as start:
            result = QianniuNative().launch()
        self.assertEqual(result['state'], 'login_required')
        self.assertIn('尚未核对登录状态', result['detail'])
        self.assertEqual(start.call_args.args, ([str(self.client)],))
        self.assertEqual(start.call_args.kwargs['cwd'], str(self.client.parent))

    def test_display_icon_and_install_directory_support_spaces_and_commas(self):
        self.assertEqual(self.launcher.executable(f'"{self.client}",0'), self.client.resolve())
        self.assertEqual(self.launcher.executable(self.client.parent), self.client.resolve())
        for bad in (self.root/'uninstall.exe', f'"{self.client}" --unknown', 'AliWorkbench.exe'):
            self.assertIsNone(self.launcher.executable(bad))

    def test_saved_path_persists_and_takes_precedence(self):
        self.launcher.save_client(self.client, self.config)
        with patch.object(self.launcher, 'registry_candidates', side_effect=AssertionError('saved first')):
            self.assertEqual(self.launcher.discover(self.config), self.client.resolve())

    def test_stale_or_broken_config_falls_back_to_registry(self):
        for text in ('invalid json', '[]', '{"executable":"missing"}'):
            self.config.write_text(text)
            with patch.object(self.launcher, 'registry_candidates', return_value=iter([self.client.parent])):
                self.assertEqual(self.launcher.discover(self.config), self.client.resolve())

    def test_no_install_and_start_failure_are_not_reported_as_opened(self):
        with patch.object(self.launcher, 'discover', return_value=None), \
             patch.object(self.launcher.subprocess, 'Popen') as start:
            with self.assertRaises(self.launcher.QianniuClientNotFound):
                self.launcher.launch_client()
            start.assert_not_called()
        with patch.object(self.launcher, 'discover', return_value=self.client), \
             patch.object(self.launcher.subprocess, 'Popen', side_effect=OSError('denied')):
            with self.assertRaises(OSError): self.launcher.launch_client()

    def test_picker_cancel_or_wrong_file_does_not_change_saved_path(self):
        self.launcher.save_client(self.client, self.config)
        original = self.config.read_bytes()
        window = Mock(); window.create_file_dialog.return_value = []
        with patch.object(self.launcher, 'settings_path', return_value=self.config):
            self.assertTrue(self.launcher.choose_client(window)['cancelled'])
            window.create_file_dialog.return_value = [str(self.root/'uninstall.exe')]
            with self.assertRaises(ValueError): self.launcher.choose_client(window)
        self.assertEqual(self.config.read_bytes(), original)

    def test_picker_uses_supported_native_filter_and_saves_selection(self):
        from webview.util import parse_file_type
        def dialog(*args, **kwargs):
            for item in kwargs['file_types']: parse_file_type(item)
            return [str(self.client)]
        window = Mock(); window.create_file_dialog.side_effect = dialog
        with patch.object(self.launcher, 'settings_path', return_value=self.config):
            self.assertTrue(self.launcher.choose_client(window)['ok'])
            self.assertEqual(self.launcher.discover(self.config), self.client.resolve())


def snapshot(text=BASE, buyer=BUYER, editor='', seller=SELLER):
    return NativeSnapshot(12, seller, buyer, text, editor, tuple(parse_transcript(text, seller, buyer, NOW)))


class FakeNative:
    def __init__(self):
        self.current = snapshot()
        self.targets = [{'window': 12, 'seller': SELLER, 'title': SELLER+'-接待中心'}]
        self.sent = 0
        self.timeout = False
        self.read_fail = False

    def windows(self): return self.targets
    def launch(self): return {'state':'login_required'}
    def snapshot(self, window, seller):
        if self.read_fail or window != self.current.window or seller != self.current.seller:
            raise NativeCompatibilityError('fixture identity changed')
        return self.current
    def preflight(self, expected):
        if self.current.fingerprint != expected.fingerprint or self.current.editor:
            raise NativeCompatibilityError('fixture human input or stale conversation')
    def select_buyer(self, window, seller, buyer):
        raise NativeCompatibilityError('fixture target not present')
    def submit(self, expected, text):
        self.preflight(expected)
        self.sent += 1
        if self.timeout:
            raise NativeCompatibilityError('fixture no echo')
        self.current = snapshot(expected.text + f'\n{SELLER} 2026-11-02 12:00:03\n{text}', expected.buyer)
        return self.current, self.current.messages[-1].message_id


class TranscriptTests(unittest.TestCase):
    def test_flat_text_alone_cannot_authorize_real_native_sends(self):
        native=QianniuNative()
        with self.assertRaisesRegex(NativeCompatibilityError,'作者与标识'):
            native.preflight(snapshot())
        self.assertFalse(native.verified_message_metadata)
    def test_authors_times_and_multiline_messages(self):
        rows = parse_transcript(BASE + f'\n{SELLER} 12:00:02\n这款149元。\n有黑色。', SELLER, BUYER, NOW)
        self.assertEqual([r.role for r in rows], ['buyer', 'agent'])
        self.assertEqual(rows[1].content, '这款149元。\n有黑色。')
        self.assertLess(rows[0].timestamp, rows[1].timestamp)

    def test_read_status_does_not_change_message_identity(self):
        a = parse_transcript(BASE, SELLER, BUYER, NOW)
        b = parse_transcript(BASE+'\n已读', SELLER, BUYER, NOW)
        self.assertEqual(a, b)

    def test_missing_identity_or_unstructured_text_is_not_guessed(self):
        for text, seller, buyer in [('你好', SELLER, BUYER), (BASE, '', BUYER), (BASE, BUYER, BUYER), ('消息记录\n只读到了零散文字', SELLER, BUYER)]:
            with self.assertRaises(NativeCompatibilityError): parse_transcript(text, seller, buyer, NOW)

    def test_bad_dates_retractions_and_ambiguous_duplicate_rows_are_rejected(self):
        for text in [f'{BUYER} 2026-99-02 12:00:00\n你好', BASE+'\n对方撤回了一条消息', BASE+'\n'+BASE]:
            with self.assertRaises(NativeCompatibilityError): parse_transcript(text, SELLER, BUYER, NOW)

    def test_same_text_at_different_times_has_different_identity(self):
        rows = parse_transcript(BASE+f'\n{BUYER} 12:00:02\n黑杆多少钱', SELLER, BUYER, NOW)
        self.assertNotEqual(rows[0].message_id, rows[1].message_id)

    def test_native_snapshot_fingerprint_includes_store_and_buyer(self):
        a = snapshot()
        b = snapshot(BASE.replace(BUYER,'买家乙'), buyer='买家乙')
        self.assertNotEqual(a.fingerprint, b.fingerprint)


class QianniuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        self.w=Workbench(root/'reception',root/'stores',StoreRegistry(root/'registry.json',root/'stores'))
        self.w.create_store('tb','构造淘宝店',platform='taobao')
        self.spec=self.w.spec('tb')
        self.native=FakeNative()
        self.p=QianniuPilot(self.spec,self.w.live,self.native)
        self.w._pilots['tb']=self.p

    def tearDown(self):
        self.w.close_connections();self.temp.cleanup()

    def connect(self):
        info=self.p.inspect();self.p.bind(self.p.account.key,True);return info

    def draft(self):
        row=self.w.inbox.get(self.spec,BUYER)
        self.w.inbox.save_draft(self.spec,BUYER,row['messages'][-1]['sequence'],{
            'action':'reply','draft_reply':'这款149元。','knowledge_versions':self.w.service('tb',offline=True).versions()})
        return self.w.inbox.get(self.spec,BUYER)

    def test_empty_or_ambiguous_account_windows_refuse_connection(self):
        for targets in ([],self.native.targets*2):
            self.native.targets=targets
            with self.assertRaises(ValueError):self.p.inspect()

    def test_binding_requires_matching_current_account_and_review(self):
        info=self.p.inspect()
        self.assertEqual(info['buyer'],BUYER)
        for key,reviewed in [('wrong',True),(self.p.account.key,False)]:
            with self.assertRaises(ValueError):self.p.bind(key,reviewed)

    def test_reading_messages_deduplicates_without_cross_store_merging(self):
        self.connect();self.assertEqual(self.p.poll()['inserted'],1)
        self.assertEqual(self.p.poll()['inserted'],0)
        self.w.create_store('tb2','另一淘宝店',platform='taobao')
        self.assertEqual(self.w.inbox.list(self.w.spec('tb2')),[])

    def test_historical_service_answer_does_not_trigger_manual_takeover(self):
        self.native.current=snapshot(f'{SELLER} 12:00:00\n您好\n'+BASE)
        self.connect();self.p.poll()
        self.assertFalse(self.w.inbox.get(self.spec,BUYER)['paused'])

    def test_new_human_reply_pauses_and_invalidates_draft(self):
        self.connect();self.p.poll();self.draft()
        self.native.current=snapshot(BASE+f'\n{SELLER} 12:00:02\n人工正在处理')
        self.p.poll();row=self.w.inbox.get(self.spec,BUYER)
        self.assertTrue(row['paused']);self.assertIsNone(row['draft'])

    def test_latest_buyer_message_invalidates_previous_draft(self):
        self.connect();self.p.poll();self.draft()
        self.native.current=snapshot(BASE+f'\n{BUYER} 12:00:02\n有货没')
        with self.assertRaises(ValueError):self.w.send_live_draft('tb',BUYER,True)
        self.assertEqual(self.native.sent,0)

    def test_editor_with_manual_draft_is_not_overwritten(self):
        self.connect();self.p.poll();self.draft();self.native.current=snapshot(editor='人工未发送的内容')
        with self.assertRaises(ValueError):self.w.send_live_draft('tb',BUYER,True)
        self.assertEqual(self.native.sent,0)

    def test_send_requires_visible_new_agent_echo_and_records_once(self):
        self.connect();self.p.poll();self.draft()
        result=self.w.send_live_draft('tb',BUYER,True)
        self.assertTrue(result['sent']);self.assertEqual(self.native.sent,1)
        self.assertFalse(self.w.inbox.get(self.spec,BUYER)['paused'])

    def test_no_echo_is_unknown_and_cannot_be_retried(self):
        self.connect();self.p.poll();row=self.draft();self.native.timeout=True
        result=self.w.send_live_draft('tb',BUYER,True)
        self.assertEqual(result['state'],'delivery_unknown')
        self.w.inbox.pause(self.spec,BUYER,False);self.draft()
        with self.assertRaises(ValueError):self.p.send_reviewed(BUYER,row['draft_head'],'这款149元。')
        self.assertEqual(self.native.sent,1)

    def test_client_identity_failure_cancels_all_pending_drafts(self):
        self.connect();self.p.poll();self.draft();self.native.read_fail=True
        with self.assertRaises(ValueError):self.p.poll()
        self.assertFalse(self.p.bound);self.assertIsNone(self.w.inbox.get(self.spec,BUYER)['draft'])

    def test_old_customer_cannot_be_sent_to_after_conversation_switch(self):
        self.connect();self.p.poll();self.draft()
        self.native.current=snapshot(BASE.replace(BUYER,'买家乙'),buyer='买家乙')
        with self.assertRaises(ValueError):self.w.send_live_draft('tb',BUYER,True)
        self.assertEqual(self.native.sent,0)


if __name__=='__main__':unittest.main(verbosity=1)
