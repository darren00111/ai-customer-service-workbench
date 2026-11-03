"""Observed card identity -> shop facts -> context switches; synthetic protocol/model."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import selftest_channel_pilots as fixture
from app.channels.pinduoduo import normalize_frame,Account
from app.service.contracts import Understanding
from app.service.data import SnapshotDataSource
from app.service.dialogue import ReceptionService
from app.service.qwen import output_schema

PRODUCTS=[{'product_id':'A','title':'黑色台球杆','aliases':['黑杆'],'price':149,'stock':5,'platform_ids':{'pinduoduo':'123'}},
          {'product_id':'B','title':'白色台球杆','aliases':['白杆'],'price':279,'stock':3,'platform_ids':{'pinduoduo':'456'}}]


def card(pid='456',mid='card1',**changes):
    return fixture.frame(mid=mid,text='https://mobile.yangkeduo.com/goods.html?goods_id='+pid,
        sub_type=0,info={'goodsID':pid,'goodsName':'卡片上的名字不能充当本店商品事实','goodsPrice':'0.01'},**changes)


def reference(pid='456',mid='card1',latest=False):
    return {'platform':'pinduoduo','remote_product_id':pid,'message_id':mid,'latest_is_card':latest}


class Model:
    name='card_context_contract'
    def __init__(self):self.value={'intents':['price'],'product_id':'A'};self.requests=[]
    def status(self):return {'enabled':True,'connected':True,'configured':True,'model':'fixture','error':''}
    def interpreter(self):return self
    def understand(self,request):self.requests.append(request);return self.value


class ProtocolTests(unittest.TestCase):
    def test_numeric_string_seconds_and_milliseconds_are_accepted(self):
        for value,expected in [('1789897477',1789897477000),('1789897477123',1789897477123),('1789897477.123',1789897477123)]:
            msg,_=normalize_frame(fixture.frame(ts=value),fixture.ACCOUNT)
            self.assertEqual(msg.timestamp,expected)

    def test_invalid_timestamps_never_crash_or_fall_back_to_another_field(self):
        for value in [True,'','NaN','Infinity',float('inf'),-1,None,'-1','1e10','9999999999999999999999']:
            self.assertIsNone(normalize_frame(fixture.frame(ts=value,time=1800000000),fixture.ACCOUNT)[0])

    def test_goods_inquiry_keeps_only_reference_not_unverified_card_prices(self):
        msg,reason=normalize_frame(card(),fixture.ACCOUNT)
        self.assertEqual((reason,msg.kind,msg.product_id,msg.action),('product_card','product_card','456','reply'))
        self.assertNotIn('0.01',msg.content);self.assertNotIn('卡片上的名字',msg.content)

    def test_unknown_or_conflicting_card_identifiers_pause_instead_of_guessing(self):
        payload=json.loads(card());payload['message']['content']='https://mobile.yangkeduo.com/goods.html?goods_id=123'
        for raw in [json.dumps(payload),fixture.frame(sub_type=0,info={}),fixture.frame(sub_type=0,info={'goodsID':True}),
                    fixture.frame(sub_type=0,info={'goodsID':'456'},content='https://evil.example/?goods_id=456')]:
            msg,_=normalize_frame(raw,fixture.ACCOUNT)
            self.assertEqual(msg.action,'pause');self.assertFalse(msg.product_id)

    def test_agent_cards_stay_agent_context_and_cannot_invent_customer_intent(self):
        payload=json.loads(card());msg=payload['message'];msg['from'],msg['to']=msg['to'],msg['from'];msg['sub_type']=2
        incoming,_=normalize_frame(json.dumps(payload),fixture.ACCOUNT)
        self.assertEqual((incoming.role,incoming.action,incoming.kind),('agent','context','product_card'))

    def test_buyer_forwarded_merchant_card_is_context_until_the_buyer_asks(self):
        payload=json.loads(card());payload['message']['sub_type']=2
        incoming,_=normalize_frame(json.dumps(payload),fixture.ACCOUNT)
        self.assertEqual((incoming.role,incoming.action,incoming.product_id),('buyer','context','456'))

    def test_read_receipts_are_not_dialogue_and_still_require_correct_account(self):
        incoming,reason=normalize_frame(fixture.frame(type=20,msg_id=None,ts=None),fixture.ACCOUNT)
        self.assertIsNone(incoming);self.assertEqual(reason,'read_receipt')
        incoming,reason=normalize_frame(fixture.frame(type=20,to={'role':'mall_cs','uid':'999'}),fixture.ACCOUNT)
        self.assertIsNone(incoming);self.assertEqual(reason,'other_account')

    def test_emotion_description_is_semantic_text_without_invented_image_content(self):
        incoming,reason=normalize_frame(fixture.frame(type=5,text='[在吗]',info={'description':'在的'}),fixture.ACCOUNT)
        self.assertEqual((incoming.content,incoming.kind,reason),('在的','emotion','emotion'))
        incoming,_=normalize_frame(fixture.frame(type=5,info={}),fixture.ACCOUNT)
        self.assertEqual(incoming.action,'pause')


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.model=Model()
        self.source=SnapshotDataSource('shop',{'policies':{'return_policy':'未使用且符合条件可申请退换'}},copy.deepcopy(PRODUCTS))
        self.service=ReceptionService(self.source,interpreter=self.model)

    def ask(self,text='这个多少钱',state=None,ref=None):
        return self.service.respond(text,state,platform_product=ref or reference(),live=True)

    def test_implicit_question_uses_current_card_even_if_model_selects_old_product(self):
        result=self.ask(state={'product_id':'A','variant':{'size':'XL'}})
        self.assertEqual(result['product_id'],'B');self.assertIn('279',result['draft_reply'])
        self.assertNotIn('149',result['draft_reply']);self.assertNotIn('variant',result['state'])
        self.assertEqual(self.model.requests[-1]['state']['platform_product_reference']['local_product_id'],'B')

    def test_bare_card_cannot_be_reclassified_as_another_named_product(self):
        self.model.value.update(product_reference='named',variant={'size':'XL'})
        result=self.ask('[顾客发送商品卡片]',ref=reference(latest=True))
        self.assertEqual(result['product_id'],'B');self.assertNotIn('variant',result['state'])

    def test_explicit_switch_and_following_implicit_question_keep_the_new_selection(self):
        first=self.ask()
        self.model.value['product_reference']='named'
        second=self.ask('改问黑杆多少钱',state=first['state'])
        self.assertEqual(second['product_id'],'A')
        self.model.value.pop('product_reference')
        third=self.ask('还有货吗',state=second['state'])
        self.assertEqual(third['product_id'],'A')

    def test_previous_product_and_comparison_can_override_the_card(self):
        first=self.ask(ref=reference('123','cardA',True))
        second=self.ask(state=first['state'],ref=reference('456','cardB',True))
        self.model.value['product_reference']='previous'
        previous=self.ask('还是前一个多少钱',state=second['state'],ref=reference('456','cardB'))
        self.assertEqual(previous['product_id'],'A')
        self.model.value={'intents':['compare'],'product_ids':['A','B'],'product_reference':'comparison'}
        compared=self.ask('对比这两款价格',state=second['state'],ref=reference('456','cardB'))
        self.assertIn('149',compared['draft_reply']);self.assertIn('279',compared['draft_reply'])

    def test_unknown_or_ambiguous_card_never_reuses_old_price_or_reasks_for_same_card(self):
        result=self.ask(state={'product_id':'A'},ref=reference('999'))
        self.assertNotIn('149',result['draft_reply']);self.assertIsNone(result['product_id'])
        self.assertIn('已收到商品卡片',result['draft_reply']);self.assertIn('platform_product',result['missing'])
        self.source.products[0]['platform_ids']['pinduoduo']='456'
        result=self.ask()
        self.assertIsNone(result['product_id']);self.assertFalse(result['automatic_resolution'])

    def test_unknown_product_does_not_hide_known_storewide_policy(self):
        self.model.value={'intents':['price','policy'],'product_id':'A'}
        result=self.ask('多少钱，没买前了解一下退换规则',ref=reference('999'))
        self.assertIn('符合条件',result['draft_reply']);self.assertNotIn('149',result['draft_reply'])

    def test_real_after_sales_action_still_goes_to_human_with_card(self):
        result=self.ask('我要退货')
        self.assertEqual(result['action'],'handoff')

    def test_supplier_schema_and_untrusted_input_cannot_create_reference_modes(self):
        schema=output_schema({'products':PRODUCTS})
        self.assertIn('previous',schema['properties']['product_reference']['enum'])
        with self.assertRaises(ValueError):Understanding.from_dict({'intents':['price'],'product_reference':'invented'}, {'A','B'})
        with self.assertRaises(ValueError):self.ask(ref={'platform':'pinduoduo','remote_product_id':'456'})


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PilotTests();self.f.setUp();self.f.connect()
        self.w,self.p,self.cdp,self.spec=self.f.w,self.f.p,self.f.cdp,self.f.spec
        self.model=Model();self.w.models=self.model
        kb=self.w.knowledge('p1');kb.activate(kb.stage('products',copy.deepcopy(PRODUCTS),'构造的本店核准资料'),reviewed=True)
    def tearDown(self):self.f.tearDown()

    def test_card_and_followup_are_continuous_and_keep_scoped_metadata(self):
        self.cdp.push(card());self.cdp.push(fixture.frame(mid='follow',text='这个多少钱',ts=1800000000500));self.p.poll()
        draft=self.w.generate_imported_draft('p1','300')
        self.assertEqual(draft['product_id'],'B');self.assertIn('279',draft['draft_reply'])
        self.assertFalse(self.w.inbox.get(self.spec,'300')['paused'])
        self.assertEqual(self.w.imported_conversation('p1','300')['product_references'][0]['product_id'],'456')
        self.assertNotIn('last_turn',self.model.requests[-1]['state'])

    def test_same_message_id_with_different_product_is_a_conflict(self):
        self.cdp.push(card());self.p.poll();self.cdp.push(card('123'))
        with self.assertRaises(ValueError):self.p.poll()

    def test_reference_older_than_raw_24_message_window_is_retained(self):
        self.cdp.push(card('123','first'));self.cdp.push(card('456','second'))
        for number in range(27):self.cdp.push(fixture.frame(mid='msg'+str(number),text='在吗',ts=1800000000000+number))
        self.cdp.push(fixture.frame(mid='price',text='这个多少钱',ts=1800000000500));self.p.poll()
        draft=self.w.generate_imported_draft('p1','300')
        self.assertEqual(draft['product_id'],'B');self.assertIn('279',draft['draft_reply'])
        self.assertEqual(self.model.requests[-1]['state']['product_history'],['A','B'])

    def test_unknown_card_creates_actionable_shop_task_instead_of_wrong_fact(self):
        self.cdp.push(card('999'));self.p.poll();draft=self.w.generate_imported_draft('p1','300')
        self.assertIsNone(draft['product_id']);self.assertNotIn('149',draft['draft_reply'])
        tasks=self.w.knowledge('p1').tasks()
        self.assertTrue(any('pinduoduo:999' in row['question'] for row in tasks))

    def test_import_cannot_forge_observed_card_fields(self):
        with self.assertRaises(ValueError):
            self.w.import_conversations('p2',json.dumps({'store_id':'p2','conversations':[{'conversation_id':'x','messages':[
                {'message_id':'fake','role':'buyer','content':'这个多少钱','product_id':'456','kind':'product_card'}]}]}))

    def test_same_external_id_and_buyer_in_another_shop_cannot_share_product_facts(self):
        other=self.w.spec('p2');account=Account('999','888','构造的第二店')
        self.w.live.bind(other,account.key)
        payload=json.loads(card());payload['message']['to']['uid']='888'
        message,_=normalize_frame(json.dumps(payload),account);self.w.live.ingest(other,account.key,message)
        kb=self.w.knowledge('p2');kb.activate(kb.stage('products',[{'product_id':'A','title':'另一店商品','price':599,
            'platform_ids':{'pinduoduo':'456'}}],'另一店核准的构造资料'),reviewed=True)
        draft=self.w.generate_imported_draft('p2','300')
        self.assertIn('599',draft['draft_reply']);self.assertNotIn('279',draft['draft_reply'])
        self.assertEqual(self.w.inbox.list(self.spec),[])

    def test_card_reply_uses_same_delivery_confirmation_and_learning_record(self):
        from app.channels.reception_worker import ChannelReceptionWorker
        original=self.p._post
        def post(path,body):
            if path!='/plateau/chat/send_message':return original(path,body)
            self.cdp.send_calls+=1
            self.cdp.push(fixture.frame('out1','agent',body['data']['message']['content'],ts=1800000001000))
            return {'success':True,'result':{'msg_id':'out1'}}
        self.p._post=post
        self.cdp.push(card());worker=ChannelReceptionWorker(self.w,'p1','automatic',('300',));worker.tick()
        self.assertEqual(self.cdp.send_calls,1);self.assertEqual(worker.confirmed,1)
        with self.w.knowledge('p1').connect() as db:
            result=json.loads(db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(result['product_id'],'B');self.assertTrue(result['sent'])
        self.assertEqual(result['delivery_channel'],'live_confirmed')

    def test_read_status_does_not_cancel_a_draft_or_pause_the_customer(self):
        self.cdp.push(card());self.p.poll();draft=self.w.generate_imported_draft('p1','300')
        self.cdp.push(fixture.frame(type=20,msg_id=None,ts=None));self.p.poll()
        thread=self.w.inbox.get(self.spec,'300')
        self.assertEqual(thread['draft'],draft);self.assertFalse(thread['paused'])
        self.assertEqual(self.p.counts['read_receipt'],1)

    def test_binary_channel_card_reaches_the_same_grounded_reply_path(self):
        import base64
        from selftest_pdd_wire import packet
        self.cdp.socket('wss://titan-ws.pinduoduo.com/')
        self.cdp.s.publish({'method':'Network.webSocketFrameReceived','params':{'requestId':'socket1',
            'response':{'opcode':2,'payloadData':base64.b64encode(packet([json.loads(card())['message']],True)).decode()}}})
        self.p.poll();draft=self.w.generate_imported_draft('p1','300')
        self.assertEqual(draft['product_id'],'B');self.assertIn('279',draft['draft_reply'])


if __name__=='__main__':unittest.main(verbosity=1)
