"""Observed product responses and reviewed knowledge integration; synthetic only."""
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import selftest_channel_pilots as fixture
from app.channels.catalog_capture import ProductCapture,parse_products,LIST_PATH,DETAIL_PATH
from app.engine.cdp import EventStream

STAMP=datetime(2026,11,2,3,tzinfo=timezone.utc)
PAYLOAD={'success':True,'result':{'onSaleGoods':[{'goodsId':123,'goodsName':'黑色台球杆','quantity':99,
    'minOnSaleGroupPrice':14900,'maxOnSaleGroupPrice':19900}]}}


class FakeTransport:
    def __init__(self):self.stream=None;self.calls=0;self.body=PAYLOAD
    def subscribe(self,methods):self.stream=EventStream(methods);return self.stream
    def call(self,method,params,timeout):
        self.calls+=1
        if method!='Network.getResponseBody':raise AssertionError(method)
        return {'body':json.dumps(self.body),'base64Encoded':False}
    def push(self,url='https://mms.pinduoduo.com'+LIST_PATH,status=200,size=100):
        self.stream.publish({'method':'Network.responseReceived','params':{'requestId':'r1','type':'XHR',
            'response':{'url':url,'status':status,'mimeType':'application/json'}}})
        self.stream.publish({'method':'Network.loadingFinished','params':{'requestId':'r1','encodedDataLength':size}})


class CaptureTests(unittest.TestCase):
    def test_group_range_is_not_an_exact_sku_price_or_inventory(self):
        row=parse_products(LIST_PATH,PAYLOAD,STAMP)[0]
        self.assertEqual(row['product_id'],'123')
        self.assertNotIn('price',row);self.assertNotIn('stock',row)
        price=row['attributes']['平台团购价范围']
        self.assertIn('149.00–199.00',price['value']);self.assertIn('不是所选规格',price['value'])
        self.assertEqual(price['expires_at'],'2026-11-02T03:10:00+00:00')

    def test_details_collect_names_without_inventing_sku_prices(self):
        payload={'success':True,'result':{'goods_id':123,'goods_name':'台球杆','skus':[
            {'spec':[{'parent_name':'颜色','spec_name':'黑色'}],'price':1,'quantity':100}]}}
        row=parse_products(DETAIL_PATH,payload,STAMP)[0]
        self.assertIn('黑色',row['attributes']['平台规格选项']['value'])
        self.assertNotIn('variants',row);self.assertNotIn('stock',row)

    def test_bad_units_duplicates_and_unknown_schema_reject_entire_batch(self):
        for value in ['14900',149.5,True,-1]:
            bad=json.loads(json.dumps(PAYLOAD));bad['result']['onSaleGoods'][0]['minOnSaleGroupPrice']=value
            with self.assertRaises(ValueError):parse_products(LIST_PATH,bad,STAMP)
        duplicate=json.loads(json.dumps(PAYLOAD));duplicate['result']['onSaleGoods']*=2
        for payload in [duplicate,{'success':True,'result':{}},{'success':False},[]]:
            with self.assertRaises(ValueError):parse_products(LIST_PATH,payload,STAMP)

    def test_only_exact_origin_path_and_successful_json_are_read(self):
        for url,status in [('https://evil.example'+LIST_PATH,200),('https://mms.pinduoduo.com.evil.example'+LIST_PATH,200),
                           ('https://mms.pinduoduo.com/orders',200),('http://mms.pinduoduo.com'+LIST_PATH,200),
                           ('https://mms.pinduoduo.com'+LIST_PATH,403)]:
            cdp=FakeTransport();capture=ProductCapture(cdp);cdp.push(url,status);capture.poll()
            self.assertEqual(cdp.calls,0);self.assertEqual(capture.products,{})

    def test_capture_has_no_network_replay_and_deduplicates_products(self):
        cdp=FakeTransport();capture=ProductCapture(cdp)
        cdp.push();capture.poll();cdp.push();capture.poll()
        self.assertEqual(len(capture.products),1);self.assertEqual(capture.observed,2)

    def test_oversize_overflow_and_changed_schema_are_visible(self):
        cdp=FakeTransport();capture=ProductCapture(cdp);cdp.push(size=3*1024*1024);capture.poll()
        self.assertTrue(capture.error);self.assertEqual(cdp.calls,0)
        cdp.body={'success':True,'result':{}};cdp.push();capture.poll()
        self.assertTrue(capture.error);self.assertEqual(capture.products,{})
        cdp.stream.overflowed=True;capture.poll();self.assertIn('缓冲',capture.error)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PilotTests();self.f.setUp();self.f.connect()
        self.w,self.p=self.f.w,self.f.p
        self.rows=parse_products(LIST_PATH,PAYLOAD,STAMP)
        self.p.catalog.products={'123':self.rows[0]}
    def tearDown(self):self.f.tearDown()

    def test_capture_requires_binding_and_observed_products(self):
        self.p.catalog.products={}
        with self.assertRaises(ValueError):self.w.capture_product_revision('p1')
        self.p.close()
        with self.assertRaises(ValueError):self.w.capture_product_revision('p1')

    def test_capture_is_scoped_staged_and_clicks_are_idempotent(self):
        result=self.w.capture_product_revision('p1')
        self.assertEqual(self.w.capture_product_revision('p1')['revision_id'],result['revision_id'])
        kb=self.w.knowledge('p1');self.assertIsNone(kb.active('products'))
        self.assertEqual(kb.preview(result['revision_id'])['payload'][0]['title'],'黑色台球杆')
        self.assertEqual(self.w.knowledge('p2').revisions(),[])

    def test_incremental_capture_keeps_approved_local_price_and_attributes(self):
        kb=self.w.knowledge('p1')
        revision=kb.stage('products',[{'product_id':'123','title':'旧名称','price':159,'stock':3,
                                     'attributes':{'材质':'枫木'}}],'构造的核准资料')
        kb.activate(revision,reviewed=True)
        result=self.w.capture_product_revision('p1')
        row=kb.preview(result['revision_id'])['payload'][0]
        self.assertEqual(row['price'],159);self.assertEqual(row['stock'],3)
        self.assertEqual(row['attributes']['材质'],'枫木')
        self.assertEqual(kb.active('products')['id'],revision)

    def test_identity_switch_cannot_publish_old_account_products(self):
        self.f.cdp.account=fixture.Account('999','888','其他店铺')
        with self.assertRaises(ValueError):self.w.capture_product_revision('p1')
        self.assertFalse(self.p.bound);self.assertEqual(self.w.knowledge('p1').revisions(),[])


if __name__=='__main__':unittest.main(verbosity=1)
