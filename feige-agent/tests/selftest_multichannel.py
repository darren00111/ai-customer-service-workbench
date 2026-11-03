"""Multi-platform preparation with real registries, knowledge and inbox storage."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import StoreRegistry
from app.multistore import MultiStoreSupervisor
from app.platforms import ConversationScope, list_platforms
from app.service.workbench import Workbench
from merchant import read_manifest, validate_manifest


class MultichannelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='feige-multichannel-')
        self.root=Path(self.temp.name)
        self.registry=StoreRegistry(self.root/'registry.json',self.root/'stores')
        self.workbench=Workbench(self.root/'reception',self.root/'stores',self.registry)
        for store,platform,merchant,price in [('tb','taobao','brand',149),('pdd','pinduoduo','brand',129),
                                               ('xhs','xiaohongshu','other',169),('dy','feige','brand',159)]:
            self.workbench.create_store(store,store,'台球用品',platform,merchant)
            product={'product_id':'SAME','title':'黑色台球杆','aliases':['黑杆'], 'price':price,'stock':5,
                     'attributes':{'长度':'145cm'},'variants':[{'name':'标准款','price':price+10}]}
            kb=self.workbench.knowledge(store)
            revision=kb.stage('products',[product],'当前店铺商品中心')
            kb.activate(revision,reviewed=True)
            revision=kb.stage('business',{'shipping':{'lead_time':'48小时内发货'},'promotions':{'note':store+'专属优惠'}},'当前店铺政策')
            kb.activate(revision,reviewed=True)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self,store='tb',conversation='同名顾客',messages=None):
        return json.dumps({'store_id':store,'conversations':[{'conversation_id':conversation,'messages':messages or [
            {'message_id':'m1','role':'buyer','content':'我看看黑杆'},
            {'message_id':'m2','role':'agent','content':'您想了解哪方面？'},
            {'message_id':'m3','role':'buyer','content':'多少钱'}]}]},ensure_ascii=False)

    def common(self,attributes=None):
        return self.workbench.save_common_product('tb','CUE',json.dumps(attributes or {'材质':'枫木'}),'厂家规格表',True)['revision']

    def test_platform_manifest_and_legacy_defaults(self):
        self.assertEqual({row.platform for row in self.registry.load()}, {'feige','taobao','pinduoduo','xiaohongshu'})
        self.assertTrue(all(not row.auto_start for row in self.registry.load()))
        raw=read_manifest(self.root/'stores/tb/merchant.yaml')
        raw['automation']['auto_send']=True
        with self.assertRaises(ValueError): validate_manifest(raw)
        raw=read_manifest(self.root/'stores/dy/merchant.yaml')
        raw.pop('merchant_id'); validate_manifest(raw)
        self.assertEqual([row['platform'] for row in list_platforms() if row['runtime_available']],['feige'])
        with self.assertRaises(ValueError): self.workbench.create_store('../escape','bad',platform='taobao')

    def test_pending_platform_never_creates_agent_or_starts_browser(self):
        factory=Mock()
        supervisor=MultiStoreSupervisor(specs=[self.workbench.spec('tb')],agent_factory=factory)
        try:
            result=supervisor.boot_store('tb')
            self.assertFalse(result['ok']); self.assertEqual(result['state'],'connection_required')
            self.assertFalse(supervisor.runtime['tb']['desired'])
            self.assertEqual(supervisor.boot_all()['attempted'],0)
            factory.assert_not_called()
            self.assertFalse(supervisor.status()['shops'][0]['connection']['capabilities']['send_messages'])
        finally: supervisor.shutdown()

    def test_shared_stage_preserves_local_commercial_facts(self):
        revision=self.common({'材质':'枫木','长度':'150cm'})
        for store,price in [('tb',149),('pdd',129)]:
            before=copy.deepcopy(self.workbench.knowledge(store).active('products')['payload'])
            result=self.workbench.apply_common_product(store,'SAME',revision,True)
            product=result['revision']['payload'][0]
            self.assertEqual(product['price'],price)
            self.assertEqual(product['stock'],5)
            self.assertEqual(product['variants'],before[0]['variants'])
            self.assertEqual(product['attributes'],{'长度':'145cm','材质':'枫木'})
            self.assertIn('长度',result['preserved_local'])
            self.assertEqual(self.workbench.knowledge(store).active('products')['payload'],before)
            self.workbench.activate(store,result['revision']['id'],True)
            self.assertIn('枫木',self.workbench.chat(store,'黑杆什么材质')['draft_reply'])
        self.assertEqual(self.workbench.service('pdd',offline=True).source.business['promotions']['note'],'pdd专属优惠')

    def test_shared_versions_update_only_inherited_attributes(self):
        first=self.workbench.apply_common_product('tb','SAME',self.common({'材质':'枫木','保养':'干擦'}),True)
        self.workbench.activate('tb',first['revision']['id'],True)
        second=self.workbench.apply_common_product('tb','SAME',self.common({'材质':'白蜡木'}),True)
        self.assertEqual(second['revision']['payload'][0]['attributes'],{'长度':'145cm','材质':'白蜡木'})
        kb=self.workbench.knowledge('tb')
        changed=copy.deepcopy(kb.active('products')['payload']);changed[0]['attributes']['材质']='本店特殊款'
        current=kb.stage('products',changed,'商家纠正当前规格');kb.activate(current,reviewed=True)
        third=self.workbench.apply_common_product('tb','SAME',self.common({'材质':'碳纤维'}),True)
        self.assertEqual(third['revision']['payload'][0]['attributes']['材质'],'本店特殊款')

    def test_shared_permissions_and_dynamic_fields(self):
        common=self.common()
        self.assertEqual(self.workbench.info('xhs')['common_catalog']['products'],[])
        with self.assertRaises(ValueError): self.workbench.apply_common_product('xhs','SAME',common,True)
        with self.assertRaises(ValueError): self.workbench.apply_common_product('tb','SAME',common,False)
        with self.assertRaises(ValueError): self.workbench.save_common_product('tb','BAD','{"价格":"99元"}','表',True)
        with self.assertRaises(ValueError): self.workbench.save_common_product('tb','BAD','{"材质":"木"}','表',False)

    def test_same_customer_and_message_ids_are_isolated(self):
        keys=[]
        for store,price in [('tb',149),('pdd',129),('xhs',169)]:
            self.workbench.import_conversations(store,self.payload(store))
            spec=self.workbench.spec(store)
            keys.append(self.workbench.inbox.get(spec,'同名顾客')['scope_key'])
            draft=self.workbench.generate_imported_draft(store,'同名顾客')
            self.assertIn(str(price),draft['draft_reply'])
            self.assertFalse(draft['sent']);self.assertFalse(draft['automatic_resolution'])
            self.assertEqual(draft['delivery_channel'],'imported_draft')
            info=self.workbench.info(store)
            self.assertEqual(info['commerce']['live_receptions'],0)
        self.assertEqual(len(set(keys)),3)

    def test_import_deduplicates_invalidates_drafts_and_rejects_wrong_store(self):
        payload=self.payload()
        self.assertEqual(self.workbench.import_conversations('tb',payload)['inserted'],3)
        self.workbench.generate_imported_draft('tb','同名顾客')
        self.assertEqual(self.workbench.import_conversations('tb',payload)['inserted'],0)
        self.assertIsNotNone(self.workbench.inbox.get(self.workbench.spec('tb'),'同名顾客')['draft'])
        with self.assertRaises(ValueError): self.workbench.import_conversations('pdd',payload)
        self.workbench.import_conversations('tb',self.payload(messages=[{'message_id':'m4','role':'buyer','content':'有货吗'}]))
        self.assertIsNone(self.workbench.inbox.get(self.workbench.spec('tb'),'同名顾客')['draft'])

    def test_conflicting_reimports_are_atomic(self):
        self.workbench.import_conversations('tb',self.payload())
        bad=self.payload(messages=[{'message_id':'m4','role':'buyer','content':'新增'},
                                   {'message_id':'m1','role':'buyer','content':'修改旧消息'}])
        with self.assertRaises(ValueError): self.workbench.import_conversations('tb',bad)
        thread=self.workbench.inbox.get(self.workbench.spec('tb'),'同名顾客')
        self.assertEqual(len(thread['messages']),3)

    def test_aftersales_pauses_drafts_and_manual_pause_cancels_results(self):
        self.workbench.import_conversations('tb',self.payload(messages=[{'message_id':'m1','role':'buyer','content':'收到就坏了，我要退货'}]))
        draft=self.workbench.generate_imported_draft('tb','同名顾客')
        self.assertEqual(draft['action'],'handoff')
        spec=self.workbench.spec('tb')
        thread=self.workbench.inbox.get(spec,'同名顾客')
        self.assertTrue(thread['paused'])
        with self.assertRaises(ValueError): self.workbench.generate_imported_draft('tb','同名顾客')
        self.workbench.inbox.pause(spec,'同名顾客',False)
        head=thread['messages'][-1]['sequence']
        self.workbench.inbox.pause(spec,'同名顾客',True)
        with self.assertRaises(ValueError): self.workbench.inbox.save_draft(spec,'同名顾客',head,draft)

    def test_customer_update_cancels_inflight_draft(self):
        self.workbench.import_conversations('tb',self.payload())
        spec=self.workbench.spec('tb')
        head=self.workbench.inbox.get(spec,'同名顾客')['messages'][-1]['sequence']
        self.workbench.import_conversations('tb',self.payload(messages=[{'message_id':'m4','role':'buyer','content':'等等'}]))
        with self.assertRaises(ValueError): self.workbench.inbox.save_draft(spec,'同名顾客',head,{'draft_reply':'旧回复'})

    def test_knowledge_update_invalidates_displayed_draft(self):
        self.workbench.import_conversations('tb',self.payload())
        self.workbench.generate_imported_draft('tb','同名顾客')
        self.assertIsNotNone(self.workbench.imported_conversation('tb','同名顾客')['draft'])
        kb=self.workbench.knowledge('tb')
        products=copy.deepcopy(kb.active('products')['payload']);products[0]['price']=179
        revision=kb.stage('products',products,'商品中心更新');kb.activate(revision,reviewed=True)
        thread=self.workbench.imported_conversation('tb','同名顾客')
        self.assertIsNone(thread['draft']);self.assertTrue(thread['draft_stale'])

    def test_existing_store_cannot_silently_change_platform_or_merchant(self):
        path=self.root/'stores/tb/merchant.yaml'
        text=path.read_text(encoding='utf-8')
        path.write_text(text.replace('merchant_id: brand','merchant_id: other'),encoding='utf-8')
        with self.assertRaises(ValueError): self.workbench.knowledge('tb')

    def test_scope_keys_include_all_identifiers(self):
        base=ConversationScope('brand','taobao','tb','相同昵称')
        self.assertNotEqual(base.key,ConversationScope('brand','pinduoduo','tb','相同昵称').key)
        self.assertNotEqual(base.key,ConversationScope('other','taobao','tb','相同昵称').key)
        self.assertNotEqual(base.key,ConversationScope('brand','taobao','other','相同昵称').key)


if __name__=='__main__': unittest.main(verbosity=2)
