"""Observe the merchant page's own product responses; never fabricate inventory.

Endpoint/field meanings were checked against Customer-Agent 4343a1f's public
product_manager notes. This independent adapter only makes reviewed candidates.
It does not export cookies, replay anti-bot headers, or treat list prices as SKU prices.
"""
import base64
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlsplit

from .pinduoduo import remote_id

LIST_PATH = '/latitude/goods/recommendGoods'
DETAIL_PATH = '/glide/v2/mms/query/commit/on_shop/detail'
METHODS = ('Network.responseReceived','Network.loadingFinished','Network.loadingFailed')
LIMIT = 2*1024*1024


def parse_products(path, payload, observed_at=None):
    stamp = observed_at or datetime.now(timezone.utc)
    if path not in (LIST_PATH, DETAIL_PATH) or not isinstance(payload,dict) or payload.get('success') is not True:
        raise ValueError('商品资料请求未成功或接口不匹配')
    result = payload.get('result')
    if not isinstance(result,dict):
        raise ValueError('商品资料结构未识别')
    records = result.get('onSaleGoods') if path == LIST_PATH else [result]
    if not isinstance(records,list) or len(records)>100:
        raise ValueError('商品资料数量或结构未识别')
    output = []
    for raw in records:
        if not isinstance(raw,dict):
            raise ValueError('商品资料行无效')
        pid = remote_id(raw.get('goodsId') if path == LIST_PATH else raw.get('goods_id',raw.get('goodsId')))
        title = raw.get('goodsName') if path == LIST_PATH else raw.get('goods_name',raw.get('goodsName'))
        if not isinstance(title,str) or not 1<=len(title.strip())<=300:
            raise ValueError('商品名称缺失或过长')
        observation = {'source':'拼多多商家页面'+path,'observed_at':stamp.isoformat(),
                       'scope':'页面返回的部分商品，不代表全店或实时规格库存'}
        attributes = {}
        if path == LIST_PATH:
            low, high = raw.get('minOnSaleGroupPrice'),raw.get('maxOnSaleGroupPrice')
            if low is not None or high is not None:
                if type(low) is not int or type(high) is not int or not 0<=low<=high<=1000000000:
                    raise ValueError('团购价格单位或区间无效，未导入')
                observation.update(group_price_min_cents=low,group_price_max_cents=high)
                price = f'{low/100:.2f}' if low==high else f'{low/100:.2f}–{high/100:.2f}'
                attributes['平台团购价范围']={'value':price+'元；不是所选规格、个人优惠或最终支付价',
                    'source':observation['source'],'updated_at':stamp.isoformat(),
                    'expires_at':(stamp+timedelta(minutes=10)).isoformat()}
        else:
            skus = raw.get('skus',[])
            if not isinstance(skus,list) or len(skus)>1000:
                raise ValueError('规格列表结构未识别')
            options = []
            for sku in skus:
                if not isinstance(sku,dict) or not isinstance(sku.get('spec',[]),list):
                    raise ValueError('商品规格结构未识别')
                parts=[]
                for spec in sku.get('spec',[]):
                    if not isinstance(spec,dict) or not isinstance(spec.get('spec_name'),str) or not 1<=len(spec['spec_name'])<=100:
                        raise ValueError('规格名称缺失')
                    parent=spec.get('parent_name','')
                    if not isinstance(parent,str) or len(parent)>100:
                        raise ValueError('规格类别无效')
                    parts.append((parent+'：' if parent else '')+spec['spec_name'])
                if parts:
                    option='，'.join(parts)
                    if option not in options:options.append(option)
            if options:
                attributes['平台规格选项']={'value':'；'.join(options)[:8000],
                    'source':observation['source'],'updated_at':stamp.isoformat()}
        output.append({'product_id':pid,'title':title.strip(),'attributes':attributes,
                       'platform_observation':observation})
    if len({row['product_id'] for row in output}) != len(output):
        raise ValueError('同批商品标识重复，未导入')
    return output


class ProductCapture:
    def __init__(self,cdp):
        self.cdp=cdp
        self.stream=cdp.subscribe(METHODS)
        self.pending={}
        self.products={}
        self.error=''
        self.observed=0

    def poll(self):
        if self.stream.overflowed:
            self.pending.clear()
            self.error='商品采集缓冲已满，请重新检查连接；客服消息独立处理'
            return
        import queue
        for _ in range(1000):
            try:event=self.stream.events.get_nowait()
            except queue.Empty:break
            method,p=event.get('method'),event.get('params') or {}
            rid=p.get('requestId')
            if method=='Network.responseReceived':
                response=p.get('response') or {}
                url=urlsplit(response.get('url') or '')
                if (url.scheme!='https' or url.hostname!='mms.pinduoduo.com' or
                    url.path not in (LIST_PATH,DETAIL_PATH) or p.get('type') not in ('XHR','Fetch') or
                    response.get('status')!=200 or not response.get('mimeType','').startswith('application/json')):
                    continue
                if len(self.pending)>=50:
                    self.error='商品采集请求过多，请重新检查连接';continue
                self.pending[rid]=url.path
            elif rid in self.pending:
                path=self.pending.pop(rid)
                if method=='Network.loadingFailed':continue
                try:
                    if p.get('encodedDataLength',0)>LIMIT:
                        raise ValueError('商品响应过大，请分批查看商品')
                    body=self.cdp.call('Network.getResponseBody',{'requestId':rid},timeout=3)
                    raw=body.get('body')
                    if not isinstance(raw,str) or len(raw)>LIMIT:
                        raise ValueError('商品响应缺失或过大')
                    if body.get('base64Encoded'):
                        raw=base64.b64decode(raw,validate=True).decode('utf-8')
                    rows=parse_products(path,json.loads(raw))
                    if len(set(self.products)|{r['product_id'] for r in rows})>1000:
                        raise ValueError('本次商品采集已达1000件，请先核对当前批次')
                    for row in rows:
                        old=self.products.get(row['product_id'],{})
                        self.products[row['product_id']]={**old,**row,'attributes':{**old.get('attributes',{}),**row['attributes']}}
                    self.observed+=len(rows);self.error=''
                except Exception as exc:
                    self.error=str(exc) if isinstance(exc,ValueError) else '商品响应读取失败，请刷新商品列表后重试'

    def status(self):
        return {'products':len(self.products),'observed_rows':self.observed,'error':self.error,
                'hint':'在已绑定的拼多多客服页打开商品列表或商品详情，再读取待核对资料；尚未连接实时规格价格、库存和订单'}
