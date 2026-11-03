"""Offline, disposable tests for the learning/adaptation contract and five bugs."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import StoreRegistry
from app.service.commerce import CommerceLearning,validate_sales,apply_sales,choose_variant,policy_fingerprint,commercial_context
from app.service.contracts import Understanding
from app.service.dialogue import ReceptionService
from app.service.data import SnapshotDataSource
from app.service.grounding import canonical_region
from app.service.knowledge import KnowledgeStore
from app.service.workbench import Workbench
from app.service.sync import SyncManager


class Model:
    name='scripted_semantic_fixture'
    def __init__(self,value):self.value=value
    def understand(self,request):return self.value


class CommerceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.kb=KnowledgeStore(self.root/'kb','shop')
        self.now=datetime(2026,10,18,tzinfo=timezone.utc)
        self.at=self.now-timedelta(days=10)
        self.clock=lambda:self.now
        self.learning=CommerceLearning(self.kb,self.clock)
        self.policy=validate_sales({'enabled':True,'goal':'help_choose','learning_mode':'auto','min_samples':20})
        self.business={'sales':self.policy}
        self.versions={'business':1,'products':2}
        self.products=[{'product_id':'A','title':'黑色球杆','product_type':'球杆','price':199,'stock':8,'attributes':{'材质':'枫木'}},
                       {'product_id':'B','title':'白色球杆','product_type':'球杆','price':239,'stock':0,'attributes':{'材质':'枫木'}},
                       {'product_id':'P','title':'巧粉','product_type':'巧粉','price':29,'stock':30}]

    def event(self,key='e1',order='o1',session='s1',kind='paid',offset=1):
        return {'event_id':key,'store_id':'shop','order_id':order,'session_id':session,
                'event_type':kind,'occurred_at':(self.at+timedelta(days=offset)).isoformat()}

    def exposure(self,key=1,session='s1',variant='offer_help',channel='live_confirmed',demo=False):
        result={'sales':{'enabled':True,'variant':variant,'policy_id':policy_fingerprint(self.business),'experiment':True},
                'action':'reply','automatic_resolution':True,'delivery_channel':channel,'demo':demo,
                'model_connected':True,'knowledge_versions':self.versions}
        CommerceLearning(self.kb,lambda:self.at).record_reception(key,session,result)

    def cover(self):
        self.learning.ingest_export({'store_id':'shop','events':[],
                    'coverage':{'from':(self.at-timedelta(days=1)).isoformat(),'through':self.now.isoformat(),'complete':True}},
                    origin='authorized_connector',trusted=True)

    def metrics(self):return self.learning.metrics(self.business,self.versions)

    def service(self,value,products=None,business=None):
        return ReceptionService(SnapshotDataSource('shop',business or {},products or self.products),self.kb,Model(value))

    def test_region_canonicalization(self):
        for value in ('广东','广东省','广东省深圳市','深圳','深圳市'):
            self.assertEqual(canonical_region(value),'广东')
        self.assertEqual(canonical_region('未知市'),'未知市')

    def test_delivery_uses_canonicalized_model_region(self):
        source={'shipping':{'delivery_by_region':{'广东':'揽收后1–2天'}}}
        result=self.service({'intents':['delivery'],'destination':'广东省'},business=source).respond('深圳呀')
        self.assertIn('1–2天',result['draft_reply']);self.assertEqual(result['state']['destination'],'广东')

    def test_attribute_fact_stored_under_specification(self):
        products=[{'product_id':'L','title':'台灯','attributes':{'规格':'8W，USB供电'}}]
        result=self.service({'intents':['details'],'product_id':'L','attribute':'供电方式'},products).respond('怎么供电的')
        self.assertIn('USB',result['draft_reply']);self.assertTrue(result['automatic_resolution'])
        self.assertIn('products.L.attributes.规格',[x['path'] for x in result['evidence']])

    def test_expired_nested_attribute_never_answers(self):
        products=[{'product_id':'L','title':'台灯','attributes':{'规格':{'value':'USB供电','expires_at':'2020-01-01'}}}]
        result=self.service({'intents':['details'],'product_id':'L','attribute':'供电方式'},products).respond('怎么供电的')
        self.assertFalse(result['automatic_resolution']);self.assertNotIn('USB',result['draft_reply'])

    def test_comparison_catches_omitted_performance_metric(self):
        result=self.service({'intents':['compare'],'product_ids':['A','B']}).respond('黑白杆哪个更不震手')
        self.assertIn('attributes.震手',result['missing']);self.assertFalse(result['automatic_resolution'])
        self.assertIn('震手',result['draft_reply'])

    def test_multiple_attributes_answer_known_and_report_unknown(self):
        result=self.service({'intents':['details'],'product_id':'A','requested_attributes':['材质','震手']}).respond('材质和震手情况')
        self.assertIn('枫木',result['draft_reply']);self.assertIn('attributes.震手',result['missing'])

    def test_recommendation_excludes_accessories(self):
        result=self.service({'intents':['recommend'],'product_type':'球杆','budget':{'max':200}}).respond('预算两百推荐球杆')
        self.assertIn('199',result['draft_reply']);self.assertNotIn('巧粉',result['draft_reply'])

    def test_preferred_product_cannot_bypass_stock_or_budget(self):
        business={'sales':{'enabled':True,'preferred_product_ids':['B','P']}}
        result=self.service({'intents':['recommend'],'product_type':'球杆','budget':{'max':200}},business=business).respond('推荐一根球杆')
        self.assertIn('199',result['draft_reply']);self.assertNotIn('239',result['draft_reply']);self.assertNotIn('巧粉',result['draft_reply'])

    def test_discount_completeness_guard(self):
        result=self.service({'intents':['price'],'product_id':'A'},business={'promotions':{'note':'满299减20'}}).respond('黑杆能便宜一点吗')
        self.assertIn('满299减20',result['draft_reply']);self.assertIn('promotion',result['intents'])

    def test_policy_rejects_unbounded_promises_and_bad_types(self):
        for value in ({'next_step':'保证明天到'}, {'discount':99}, {'min_samples':1},{'learning_mode':[]},{'enabled':'yes'}):
            with self.assertRaises(ValueError):validate_sales(value)

    def test_store_strategy_changes_next_reply_and_is_reversible(self):
        wb=Workbench(self.root/'wb',self.root/'stores')
        before=wb.chat('demo_billiards','黑杆多少钱','before')
        old=wb.knowledge('demo_billiards').active('business')['id']
        wb.save_sales_policy('demo_billiards',{'enabled':True,'goal':'help_choose','tone':'warm','next_step':'offer_help'})
        after=wb.chat('demo_billiards','黑杆多少钱','after')
        self.assertNotIn('需要的话',before['draft_reply']);self.assertIn('需要的话',after['draft_reply'])
        self.assertIn('199',after['draft_reply'])
        self.assertEqual(wb.knowledge('demo_billiards').transcript('after')[-1]['content'],after['draft_reply'])
        wb.activate('demo_billiards',old,True)
        self.assertNotIn('需要的话',wb.chat('demo_billiards','黑杆多少钱','rollback')['draft_reply'])

    def test_sales_prompt_only_once_and_never_for_handoff_or_missing(self):
        service=self.service({'intents':['price'],'product_id':'A'},business={'sales':{'enabled':True,'goal':'help_choose','next_step':'offer_help'}})
        first=service.respond('黑杆多少钱',session_id='s');second=service.respond('再说一遍',first['state'],session_id='s')
        self.assertIn('需要的话',first['draft_reply']);self.assertNotIn('需要的话',second['draft_reply'])
        result=service.respond('我要退货',session_id='s')
        self.assertEqual(result['action'],'handoff');self.assertNotIn('需要的话',result['draft_reply'])

    def test_assignment_is_stable_and_store_specific(self):
        left=[choose_variant('shop',str(i),self.policy,self.versions) for i in range(100)]
        self.assertEqual(left,[choose_variant('shop',str(i),self.policy,self.versions) for i in range(100)])
        self.assertEqual(set(left),{'none','offer_help'})

    def test_simulated_sessions_do_not_train(self):
        self.exposure(channel='simulation');self.cover()
        self.learning.ingest([self.event()],origin='authorized_connector',trusted=True)
        result=self.metrics();self.assertEqual(result['groups']['offer_help']['sessions'],0)

    def test_demo_data_never_trains_even_if_marked_live(self):
        self.exposure(demo=True);self.cover()
        self.assertEqual(self.metrics()['live_receptions'],0)

    def test_customer_text_cannot_create_order_events(self):
        event=self.event();event['trusted']=True
        with self.assertRaises(ValueError):self.learning.ingest([event])
        with self.assertRaises(ValueError):self.learning.ingest([self.event()],trusted=True)

    def test_event_dedup_and_order_id_not_stored_plain(self):
        event=self.event(order='SECRET_ORDER_REFERENCE')
        self.assertEqual(self.learning.ingest([event])['added'],1)
        self.assertEqual(self.learning.ingest([event])['duplicates'],1)
        with self.kb.connect() as db:
            row=dict(db.execute('SELECT * FROM sales_outcomes').fetchone())
        self.assertNotIn('SECRET_ORDER_REFERENCE',json.dumps(row))

    def test_conflicting_batch_is_atomic(self):
        self.learning.ingest([self.event()])
        altered=self.event();altered['session_id']='other'
        with self.assertRaises(ValueError):self.learning.ingest([self.event('fresh','fresh'),altered])
        self.assertEqual(self.metrics()['reported_events'],1)

    def test_cross_store_events_rejected(self):
        event=self.event();event['store_id']='other'
        with self.assertRaises(ValueError):self.learning.ingest([event])

    def test_order_cannot_be_attributed_to_two_sessions(self):
        self.learning.ingest([self.event()])
        with self.assertRaises(ValueError):self.learning.ingest([self.event('e2',session='other',offset=2)])

    def test_conflicting_same_time_states_rejected(self):
        self.learning.ingest([self.event()])
        with self.assertRaises(ValueError):self.learning.ingest([self.event('refund',kind='refunded')])

    def test_unverified_order_never_changes_adaptation(self):
        self.exposure();self.cover();self.learning.ingest([self.event()])
        self.assertEqual(self.metrics()['groups']['offer_help']['paid_sessions'],0)

    def test_incomplete_coverage_not_counted_as_lost_sale(self):
        self.exposure();self.learning.ingest([self.event()],origin='authorized_connector',trusted=True)
        metrics=self.metrics();self.assertEqual(metrics['groups']['offer_help']['sessions'],0)
        self.assertEqual(metrics['awaiting_complete_outcome_data'],1)

    def test_trusted_payment_and_refund_recompute_outcome(self):
        self.exposure();self.cover();self.learning.ingest([self.event()],origin='authorized_connector',trusted=True)
        self.assertEqual(self.metrics()['groups']['offer_help']['paid_sessions'],1)
        self.learning.ingest([self.event('refund',kind='refunded',offset=2)],origin='authorized_connector',trusted=True)
        self.assertEqual(self.metrics()['groups']['offer_help']['paid_sessions'],0)

    def test_out_of_order_refund_remains_refunded(self):
        self.exposure();self.cover()
        self.learning.ingest([self.event('refund',kind='refunded',offset=2),self.event()],origin='authorized_connector',trusted=True)
        self.assertEqual(self.metrics()['groups']['offer_help']['paid_sessions'],0)

    def test_pre_reception_payment_never_attributed(self):
        self.exposure();self.cover()
        self.learning.ingest([self.event(offset=-1)],origin='authorized_connector',trusted=True)
        self.assertEqual(self.metrics()['groups']['offer_help']['paid_sessions'],0)

    def test_session_count_is_not_message_or_order_count(self):
        self.exposure();self.exposure(key=2);self.cover()
        self.learning.ingest([self.event(),self.event('e2','o2')],origin='authorized_connector',trusted=True)
        metrics=self.metrics()['groups']['offer_help']
        self.assertEqual(metrics['sessions'],1);self.assertEqual(metrics['paid_sessions'],1)

    def test_new_policy_does_not_reuse_old_conversion_counts(self):
        self.exposure();self.cover();self.business['sales']['tone']='warm'
        self.assertEqual(self.metrics()['groups']['offer_help']['sessions'],0)

    def test_insufficient_samples_never_auto_apply(self):
        self.exposure();self.cover();self.learning.ingest([self.event()],origin='authorized_connector',trusted=True)
        self.assertIsNone(self.metrics()['auto_applied_variant'])

    def test_sufficient_complete_cohorts_choose_approved_variant_only(self):
        events=[]
        for i in range(40):
            self.exposure(key=i+1,session='s'+str(i),variant='none' if i<20 else 'offer_help')
            if i>=20:events.append(self.event('e'+str(i),'o'+str(i),'s'+str(i)))
        self.cover();self.learning.ingest(events,origin='authorized_connector',trusted=True)
        self.assertEqual(self.metrics()['auto_applied_variant'],'offer_help')

    def test_observation_only_does_not_apply_winner(self):
        self.business['sales']['learning_mode']='observe'
        self.assertIsNone(self.metrics()['auto_applied_variant'])

    def test_continuous_feedback_generates_candidate_without_publishing(self):
        wb=Workbench(self.root/'feedback',self.root/'stores')
        result=wb.chat('demo_billiards','能刻字吗','s')
        item=wb.correct('demo_billiards',result['interaction_id'],'本店不提供刻字服务')
        report=wb.learning_refresh('demo_billiards');self.assertEqual(len(report['generated_revisions']),1)
        candidate=wb.propose_correction('demo_billiards',item['feedback_id'])
        self.assertFalse(candidate['active'])
        self.assertEqual(wb.learning_refresh('demo_billiards')['generated_revisions'],[])
        self.assertTrue(wb.evaluate('demo_billiards',candidate['id'])['passed'])
        wb.activate('demo_billiards',candidate['id'],True)
        self.assertIn('不提供刻字',wb.chat('demo_billiards','能刻字吗','new')['draft_reply'])

    def test_order_export_sync_is_idempotent_and_not_knowledge(self):
        path=self.root/'orders.json';path.write_text(json.dumps([self.event()]),encoding='utf-8')
        sync=SyncManager(self.kb);sync.register(str(path),'outcomes',trusted_outcomes=True)
        self.assertEqual(sync.poll(force=True)[0]['status'],'outcomes_imported')
        self.assertEqual(sync.poll(force=True)[0]['status'],'unchanged')
        self.assertEqual(self.kb.revisions(),[])
        self.assertEqual(self.metrics()['trusted_events'],1)

    def test_invalid_sales_policy_cannot_replace_active_knowledge(self):
        version=self.kb.stage('business',{'sales':{}},'核准');self.kb.activate(version)
        with self.assertRaises(ValueError):self.kb.stage('business',{'sales':{'min_samples':0}},'错误')
        self.assertEqual(self.kb.active('business')['id'],version)

    def test_stock_and_source_timestamp_updates_keep_learning_context(self):
        before=commercial_context(self.business,self.products)
        changed=copy.deepcopy(self.products);changed[0]['stock']=1
        changed[0]['price']={'value':199,'updated_at':'2026-10-18','source':'商品中心'}
        self.assertEqual(commercial_context(self.business,changed),before)
        changed[0]['price']=209
        self.assertNotEqual(commercial_context(self.business,changed),before)

    def test_unknown_data_and_simulations_cannot_choose_winner(self):
        for i in range(60):self.exposure(i+1,'sim'+str(i),channel='simulation')
        self.cover();self.assertIsNone(self.metrics()['auto_applied_variant'])

    def test_stock_updates_preserve_real_cohorts_but_price_updates_separate(self):
        result={'sales':{'enabled':True,'policy_id':policy_fingerprint(self.business),'variant':'offer_help','experiment':True,
                         'context_id':commercial_context(self.business,self.products)},'action':'reply','automatic_resolution':True,
                'delivery_channel':'live_confirmed','model_connected':True,'knowledge_versions':self.versions}
        CommerceLearning(self.kb,lambda:self.at).record_reception(1,'s1',result);self.cover()
        changed=copy.deepcopy(self.products);changed[0]['stock']=1
        stats=self.learning.metrics(self.business,{'business':1,'products':99},changed)
        self.assertEqual(stats['groups']['offer_help']['sessions'],1)
        changed[0]['price']=200
        self.assertEqual(self.learning.metrics(self.business,self.versions,changed)['groups']['offer_help']['sessions'],0)

    def test_partial_question_answers_known_facts_despite_model_clarification(self):
        result=self.service({'intents':['price','delivery'],'product_id':'A','needs_clarification':True},business={'shipping':{}}).respond('多少钱，多久到')
        self.assertIn('199',result['draft_reply']);self.assertIn('destination',result['missing'])

    def test_price_and_inventory_comparison_are_not_missing_attributes(self):
        result=self.service({'intents':['compare'],'product_ids':['A','B'],'requested_attributes':['价格','库存']}).respond('比比价格和库存')
        self.assertTrue(result['automatic_resolution']);self.assertEqual(result['missing'],[])

    def test_stale_feedback_candidate_can_be_regenerated(self):
        wb=Workbench(self.root/'stale',self.root/'stores');store='demo_billiards'
        response=wb.chat(store,'能刻字吗','one');feedback=wb.correct(store,response['interaction_id'],'本店不提供刻字服务')
        first=wb.propose_correction(store,feedback['feedback_id'])
        kb=wb.knowledge(store);version=kb.stage('faq',[{'question':'其他问题','answer':'其他答案'}],'知识更新',demo=True);kb.activate(version,allow_demo=True)
        second=wb.propose_correction(store,feedback['feedback_id']);self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(second['base_revision'],version)

    def test_storewide_faq_answers_without_inventing_a_product(self):
        revision=self.kb.stage('faq',[{'question':'可以刻字吗','answer':'本店不提供刻字服务'}],'商家核准');self.kb.activate(revision)
        result=self.service({'intents':['faq'],'faq_index':0,'attribute':'刻字','needs_clarification':True}).respond('名字能刻在这根上吗')
        self.assertEqual(result['draft_reply'],'本店不提供刻字服务')

    def test_faq_cannot_cover_a_different_missing_attribute(self):
        revision=self.kb.stage('faq',[{'question':'可以刻字吗','answer':'本店不提供刻字服务'}],'商家核准');self.kb.activate(revision)
        result=self.service({'intents':['details'],'product_id':'A','faq_index':0,'requested_attributes':['震手']}).respond('这根震手吗')
        self.assertIn('attributes.震手',result['missing']);self.assertFalse(result['automatic_resolution'])

    def test_explicit_source_trust_upgrade_preserves_event_identity(self):
        self.learning.ingest([self.event()],origin='merchant_export',trusted=False)
        result=self.learning.ingest([self.event()],origin='merchant_export',trusted=True)
        self.assertEqual(result['upgraded'],1);self.assertEqual(self.metrics()['trusted_events'],1)
        changed=self.event();changed['order_id']='another'
        with self.assertRaises(ValueError):self.learning.ingest([changed],origin='merchant_export',trusted=True)

    def test_switching_from_assisted_to_auto_reuses_same_business_evidence(self):
        original=policy_fingerprint(self.business)
        self.business['sales']['learning_mode']='assist'
        self.assertEqual(policy_fingerprint(self.business),original)

    def test_candidate_product_type_and_attribute_contract_rejects_foreign_ids(self):
        for value in ({'intents':['recommend'],'recommendation_ids':['FOREIGN']},
                      {'intents':['details'],'requested_attributes':'材质'},
                      {'intents':['faq'],'faq_index':True}):
            with self.assertRaises(ValueError):Understanding.from_dict(value,{'A'})


if __name__=='__main__':unittest.main(verbosity=2)
