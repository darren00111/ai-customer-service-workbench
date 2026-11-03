"""Simple merchant saves must actually affect answers without extra UI stages."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import StoreRegistry
from app.service.workbench import Workbench
from app.service.merchant_edits import MerchantEdits
from app.service.workbench_api import WorkbenchAPI


class MerchantEditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.w=Workbench(self.root/'runtime',self.root/'shops',StoreRegistry(self.root/'registry.json',self.root/'shops'))
        self.edit=MerchantEdits(self.w);self.sid='demo_billiards'

    def turn(self,text='能刻字吗',sid=None):
        sid=sid or self.sid;r=self.w.chat(sid,text,session='merchant')
        return r['interaction_id'],self.edit.context(sid,r['interaction_id'])

    def save(self,text,value,kind='faq',pid='',index=None):
        mid,context=self.turn(text)
        return self.edit.save(self.sid,mid,kind,value,pid,index,context['versions'])

    def test_one_save_publishes_and_next_answer_uses_it(self):
        result=self.save('能刻字吗','本店不提供刻字服务')
        self.assertTrue(result['applied']);self.assertFalse(result['sent'])
        self.assertIn('不提供刻字',self.w.chat(self.sid,'能刻字吗',session='new')['draft_reply'])

    def test_replacing_same_question_retires_old_expectation_only_after_success(self):
        self.save('能刻字吗','本店不提供刻字服务')
        result=self.save('能刻字吗','本店只提供激光刻字服务')
        self.assertTrue(result['applied'])
        self.assertIn('只提供激光',self.w.chat(self.sid,'能刻字吗',session='new')['draft_reply'])

    def test_failed_activation_restores_previous_answer_validation(self):
        self.save('能刻字吗','本店不提供刻字服务')
        kb=self.w.knowledge(self.sid)
        with kb.connect() as db:
            previous=dict(db.execute('SELECT id,status FROM feedback').fetchone())
        with patch.object(self.w,'activate',side_effect=RuntimeError('构造发布失败')):
            with self.assertRaises(RuntimeError):self.save('能刻字吗','本店只提供激光刻字服务')
        with kb.connect() as db:
            self.assertEqual(previous['status'],db.execute('SELECT status FROM feedback WHERE id=?',(previous['id'],)).fetchone()['status'])
        self.assertIn('不提供刻字',self.w.chat(self.sid,'能刻字吗',session='new')['draft_reply'])

    def test_imported_feedback_reference_does_not_prevent_edit(self):
        kb=self.w.knowledge(self.sid)
        rev=kb.stage('faq',[{'question':'能刻字吗','answer':'原导入答案','feedback_id':987654}], '构造导入',demo=True)
        kb.activate(rev,allow_demo=True)
        self.assertTrue(self.save('能刻字吗','本店不提供刻字服务')['applied'])

    def test_product_scope_is_preserved_across_new_dialogues(self):
        self.w.chat(self.sid,'黑杆多少钱',session='merchant')
        self.assertTrue(self.save('能刻字吗','这款可以刻字',pid='CUE-B')['applied'])
        self.assertNotIn('可以刻字',self.w.chat(self.sid,'能刻字吗',session='other')['draft_reply'])
        self.w.chat(self.sid,'白杆多少钱',session='other')
        self.assertNotIn('可以刻字',self.w.chat(self.sid,'能刻字吗',session='other')['draft_reply'])

    def test_scope_can_be_chosen_for_previously_unbound_question(self):
        self.assertTrue(self.save('能刻字吗','这款可以刻字',pid='CUE-B')['applied'])
        self.w.chat(self.sid,'黑杆多少钱',session='new')
        self.assertIn('可以刻字',self.w.chat(self.sid,'能刻字吗',session='new')['draft_reply'])

    def test_stock_updates_structured_fact_without_fixed_sold_out_faq(self):
        self.assertTrue(self.save('巧粉还有货吗','0','stock','CHALK')['applied'])
        self.assertIn('无货',self.w.chat(self.sid,'巧粉还有货吗',session='new')['draft_reply'])
        self.assertIsNone(self.w.knowledge(self.sid).active('faq'))
        self.assertTrue(self.save('巧粉还有货吗','6','stock','CHALK')['applied'])
        self.assertIn('6件',self.w.chat(self.sid,'巧粉还有货吗',session='newer')['draft_reply'])

    def test_price_edit_preserves_other_properties(self):
        self.assertTrue(self.save('黑杆多少钱','123.5','price','CUE-B')['applied'])
        self.assertIn('123.5',self.w.chat(self.sid,'黑杆多少钱',session='new')['draft_reply'])
        product=self.w.knowledge(self.sid).active('products')['payload'][0]
        self.assertEqual(8,product['stock']);self.assertEqual('枫木',product['attributes']['材质'])

    def test_invalid_numbers_and_foreign_product_change_nothing(self):
        mid,ctx=self.turn('黑杆多少钱')
        for kind,value,pid in [('stock','-1','CUE-B'),('stock','1.5','CUE-B'),('price','NaN','CUE-B'),('price','2','foreign')]:
            with self.assertRaises(ValueError):self.edit.save(self.sid,mid,kind,value,pid,None,ctx['versions'])
        self.assertEqual(199,self.w.knowledge(self.sid).active('products')['payload'][0]['price'])

    def test_stock_and_promise_cannot_be_saved_as_faq(self):
        for value in ('已经卖完了哦','保证明天到','再优惠20元'):
            with self.assertRaises(ValueError):self.save('能刻字吗',value)
        self.assertIsNone(self.w.knowledge(self.sid).active('faq'))

    def test_stale_versions_do_not_overwrite_new_data(self):
        mid,ctx=self.turn('黑杆多少钱')
        self.save('黑杆多少钱','222','price','CUE-B')
        with self.assertRaises(ValueError):self.edit.save(self.sid,mid,'price','1','CUE-B',None,ctx['versions'])

    def test_sku_stock_only_updates_selected_variant(self):
        kb=self.w.knowledge(self.sid);payload=copy.deepcopy(kb.active('products')['payload'])
        payload[0]['variants']=[{'sku_id':'A','color':'黑色','stock':4,'price':199},{'sku_id':'B','color':'白色','stock':9,'price':210}]
        rev=kb.stage('products',payload,'合成规格',demo=True);kb.activate(rev,allow_demo=True)
        self.assertTrue(self.save('黑杆还有货吗','0','stock','CUE-B',0)['applied'])
        variants=kb.active('products')['payload'][0]['variants']
        self.assertEqual(0,variants[0]['stock']['value']);self.assertEqual(9,variants[1]['stock'])
        with self.assertRaises(ValueError):self.save('黑杆还有货吗','3','stock','CUE-B')

    def test_simple_card_supplement_checks_and_publishes(self):
        sid='merchant_test';self.w.create_store(sid,'测试店')
        self.w.chat(sid,'能刻字吗')
        card=self.w.cards(sid)['cards'][0]
        result=self.edit.save_card(sid,card['id'],'本店不提供刻字服务')
        self.assertTrue(result['applied'])
        self.assertIn('不提供刻字',self.w.chat(sid,'能刻字吗',session='new')['draft_reply'])
        self.assertEqual('observing',self.w.card(sid,card['id'])['status'])

    def test_restart_keeps_saved_answer_and_records(self):
        self.save('能刻字吗','不提供刻字服务')
        reopened=Workbench(self.root/'runtime',self.root/'shops',self.w.registry)
        self.assertIn('不提供刻字',reopened.chat(self.sid,'能刻字吗')['draft_reply'])
        self.assertTrue(reopened.publish_history(self.sid)['history'])

    def test_bridge_returns_clear_applied_status(self):
        api=WorkbenchAPI(self.w)
        mid,ctx=self.turn()
        self.assertTrue(api.save_reply_edit(self.sid,mid,'faq','不提供刻字服务','',None,ctx['versions'])['data']['applied'])


if __name__=='__main__':unittest.main()
