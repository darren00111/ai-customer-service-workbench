"""User outcomes in a disposable experience; never platform login or model calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.service.experience import Experience, ExperienceAPI


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.e = Experience(self.root)
        self.sid = 'demo_billiards'

    def prepared(self):
        return self.e.prepare(self.sid)

    def correction(self, answer='本店暂不提供刻字服务'):
        turn = self.e.chat(self.sid, '能刻字吗')
        return self.e.preview_correction(self.sid, turn['interaction_id'], answer)

    def test_start_without_account_key_or_seeded_facts(self):
        with patch('app.engine.browser.ensure_browser') as browser, patch('app.service.qwen.model_manager') as models:
            info = self.e.status()
            self.assertFalse(info['prepared']); self.assertFalse(info['model_calls'])
            self.assertFalse(info['customer_messages_sent'])
            self.assertIsNone(self.e.workbench.registry); self.assertIsNone(self.e.workbench.models)
            browser.assert_not_called(); models.assert_not_called()
        self.assertFalse((self.root / 'stores.json').exists())

    def test_prepare_all_three_categories_and_repeat_is_idempotent(self):
        for sid in self.e.sources:
            info = self.e.prepare(sid)
            self.assertTrue(info['prepared']); self.assertGreater(info['facts'], 0)
            count = len(info['history'])
            self.assertEqual(count, len(self.e.prepare(sid)['history']))

    def test_price_inventory_and_source(self):
        self.prepared()
        reply = self.e.chat(self.sid, '黑杆多少钱，还有货吗')
        self.assertIn('199', reply['reply']); self.assertTrue(reply['evidence'])
        self.assertFalse(reply['sent'])
        self.assertTrue(all(row['demo'] for row in reply['evidence']))

    def test_cold_start_requires_preparation(self):
        with self.assertRaises(ValueError): self.e.chat(self.sid, '黑杆多少钱')

    def test_aftersale_does_not_become_automatic_reply(self):
        self.prepared()
        self.assertEqual('handoff', self.e.chat(self.sid, '我要退货')['action'])

    def test_correction_preview_does_not_publish_then_confirmation_teaches(self):
        self.prepared()
        proposed = self.correction()
        self.assertTrue(proposed['report']['passed'])
        self.assertNotIn('不提供刻字', self.e.chat(self.sid, '能刻字吗')['reply'])
        self.e.confirm_correction(self.sid, proposed['revision'])
        self.e.clear_chat(self.sid)
        self.assertIn('不提供刻字', self.e.chat(self.sid, '能刻字吗')['reply'])

    def test_rollback_changes_actual_answer_and_is_audited(self):
        self.prepared(); candidate = self.correction()
        self.e.confirm_correction(self.sid, candidate['revision'])
        self.assertTrue(self.e.status()['rollback_available'])
        self.e.rollback(self.sid); self.e.clear_chat(self.sid)
        self.assertNotIn('不提供刻字', self.e.chat(self.sid, '能刻字吗')['reply'])
        self.assertEqual('rollback', self.e.status()['history'][0]['action'])

    def test_product_correction_keeps_scope_and_explains_it(self):
        self.prepared();self.e.chat(self.sid,'黑杆多少钱')
        proposed=self.correction()
        self.assertIn('入门黑色台球杆',proposed['items'][-1]['scope_label'])
        self.e.confirm_correction(self.sid,proposed['revision'])
        self.e.clear_chat(self.sid)
        self.assertNotIn('不提供刻字',self.e.chat(self.sid,'能刻字吗')['reply'])
        self.e.chat(self.sid,'白杆多少钱')
        self.assertNotIn('不提供刻字',self.e.chat(self.sid,'能刻字吗')['reply'])
        self.e.chat(self.sid,'黑杆多少钱')
        self.assertIn('不提供刻字',self.e.chat(self.sid,'能刻字吗')['reply'])

    def test_correction_cannot_overwrite_price(self):
        self.prepared(); turn = self.e.chat(self.sid, '黑杆多少钱')
        with self.assertRaises(ValueError):
            self.e.preview_correction(self.sid, turn['interaction_id'], '只要1元')

    def test_stale_correction_and_cross_store_interaction_rejected(self):
        self.prepared(); old = self.e.chat(self.sid, '能刻字吗')
        self.e.chat(self.sid, '你好')
        with self.assertRaises(ValueError): self.e.preview_correction(self.sid, old['interaction_id'], '不支持')
        self.e.prepare('demo_fashion')
        with self.assertRaises(ValueError): self.e.preview_correction('demo_fashion', old['interaction_id'], '不支持')

    def test_no_cross_store_learning_and_restart_preserves_knowledge(self):
        self.prepared(); proposed = self.correction()
        self.e.confirm_correction(self.sid, proposed['revision'])
        other = Experience(self.root); other.prepare('demo_fashion')
        self.assertIn('不提供刻字', other.chat(self.sid, '能刻字吗')['reply'])
        self.assertNotIn('不提供刻字', other.chat('demo_fashion', '能刻字吗')['reply'])

    def test_clear_chat_keeps_knowledge_and_hides_old_messages(self):
        self.prepared(); self.e.chat(self.sid, '你好')
        self.assertTrue(self.e.status()['transcript'])
        self.e.clear_chat(self.sid)
        self.assertFalse(self.e.status()['transcript']); self.assertTrue(self.e.status()['prepared'])

    def test_chinese_csv_auto_ids_and_repeat_import_no_duplicate_revision(self):
        self.prepared()
        data = '商品名称,价格,库存,材质,规格,别名\n测试黑杆,25,8,枫木,145cm,黑杆\n'
        result = self.e.import_products(self.sid, data)
        self.assertEqual(1, result['products'])
        count = len(result['history'])
        self.assertEqual(count, len(self.e.import_products(self.sid,data)['history']))
        self.assertIn('25', self.e.chat(self.sid, '测试黑杆多少钱')['reply'])
        self.e.prepare(self.sid)
        self.assertEqual(1, self.e.status()['products'])

    def test_import_validates_before_changing_existing_knowledge(self):
        self.prepared()
        for content in ('商品名称,价格,库存\n错误,NaN,2', '商品名称,价格,库存\n错误,1,-2',
                        '商品名称,价格,库存\n同款,1,1\n同款,2,2', '商品名称,价格,库存\n', '名称,价格\n错误,1'):
            with self.subTest(content=content):
                with self.assertRaises(ValueError): self.e.import_products(self.sid,content)
                self.assertEqual(3,self.e.status()['products'])

    def test_catalog_sizes_20_200_1000(self):
        for size in (20,200,1000):
            csv = '商品名称,价格,库存\n' + '\n'.join('测试商品%04d,%d,2' % (i,i+1) for i in range(size))
            self.assertEqual(size,self.e.import_products(self.sid,csv)['products'])
            self.e.clear_chat(self.sid)
            self.assertIn(str(size),self.e.chat(self.sid,'测试商品%04d多少钱' % (size-1))['reply'])

    def test_invalid_store_and_unknown_confirm_do_not_open_real_workbench(self):
        api = ExperienceAPI(self.root)
        self.assertFalse(api.status('../production')['ok'])
        self.assertFalse(api.confirm_correction(self.sid,9999)['ok'])
        self.assertFalse(api.chat(self.sid,'')['ok'])
        self.assertFalse(hasattr(api,'send_live_draft')); self.assertFalse(hasattr(api,'pilot_command'))

    def test_desktop_entry_reuses_window_and_never_opens_live_services(self):
        from app.desktop import DesktopAPI
        from types import SimpleNamespace
        supervisor=SimpleNamespace(shutdown=Mock())
        shell=DesktopAPI(supervisor,SimpleNamespace(detach=Mock()))
        window=MagicMock()
        with patch('app.config.DATA_DIR',self.root), patch('app.service.experience.open_experience',return_value=window) as factory:
            self.assertTrue(shell.open_experience()['ok'])
            self.assertTrue(shell.open_experience()['ok'])
            factory.assert_called_once_with(self.root/'sandbox')
            window.show.assert_called_once()
            self.assertIsNone(shell._workbench_api)
            shell.shutdown();window.destroy.assert_called_once()


if __name__ == '__main__': unittest.main()
