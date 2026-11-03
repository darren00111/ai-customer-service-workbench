"""Merchant-facing edits: one explicit save, internal validation and publishing."""
from __future__ import annotations

import copy
import json
import math
import re

from .knowledge import now, redact
from .learning import LearningLoop


class MerchantEdits:
    def __init__(self, workbench):
        self.w = workbench

    def context(self, store_id, interaction_id):
        kb = self.w.knowledge(store_id)
        with kb.connect() as db:
            row = db.execute('SELECT * FROM interactions WHERE id=?', (int(interaction_id),)).fetchone()
        if not row:
            raise ValueError('请先选择一条需要修改的回复')
        result = json.loads(row['result'])
        active = kb.active('products')
        choices = []
        for product in active['payload'] if active else []:
            variants = product.get('variants') or []
            if variants:
                for index, variant in enumerate(variants):
                    label = ' / '.join(str(variant[k]) for k in ('color', 'size', 'sku_id') if variant.get(k))
                    choices.append({'product_id':product['product_id'], 'variant_index':index,
                                    'title':product['title'] + ' · ' + (label or '规格%d' % (index+1))})
            else:
                choices.append({'product_id':product['product_id'], 'variant_index':None, 'title':product['title']})
        intents = result.get('intents', [])
        kind = 'stock' if 'inventory' in intents else 'price' if 'price' in intents else 'faq'
        return {'interaction_id':row['id'], 'question':row['question'],
                'reply':result.get('draft_reply', ''), 'product_id':result.get('product_id'),
                'choices':choices, 'suggested_kind':kind, 'versions':self.w.service(store_id,offline=True).versions()}

    def save(self, store_id, interaction_id, kind, value, product_id='', variant_index=None, versions=None):
        if kind not in ('faq','stock','price'):
            raise ValueError('请选择改答案、库存或价格')
        with self.w._lock:
            kb = self.w.knowledge(store_id)
            context = self.context(store_id, interaction_id)
            if not isinstance(versions,dict) or versions != context['versions']:
                raise ValueError('店铺资料已更新，请重新选择这条回复后再保存')
            with kb.connect() as db:
                original = db.execute('SELECT * FROM interactions WHERE id=?', (int(interaction_id),)).fetchone()
            result = json.loads(original['result'])
            selected = next((p for p in context['choices'] if p['product_id']==product_id and p['variant_index']==variant_index),None)
            if product_id and not selected:
                raise ValueError('请选择本店商品和具体规格')
            if kind in ('stock','price'):
                if not selected:
                    raise ValueError('请先选择要修改的商品')
                if isinstance(value,bool):raise ValueError('请填写有效数字')
                try:
                    numeric = float(value)
                    if not math.isfinite(numeric) or numeric < 0 or (kind=='stock' and not numeric.is_integer()):raise ValueError()
                except (ValueError,TypeError,OverflowError):
                    raise ValueError('库存请填0或正整数' if kind=='stock' else '价格请填0或正数') from None
                product = next(p for p in kb.active('products')['payload'] if p['product_id']==product_id)
                patch = {'product_id':product_id}
                field = 'stock' if kind=='stock' else 'price'
                numeric = int(numeric) if kind=='stock' else numeric
                fact = {'value':numeric,'source':'店主在回复纠错中确认','updated_at':now()}
                if variant_index is None:
                    patch[field] = fact
                else:
                    patch['variants'] = copy.deepcopy(product['variants'])
                    patch['variants'][variant_index][field] = fact
                revision = kb.stage_incremental('products', {'upserts':[patch]}, '店主确认商品'+('库存' if kind=='stock' else '价格'), store_id in self.w.fixtures)
                view = kb.preview(revision)
                updated = next(p for p in view['payload'] if p['product_id']==product_id)
                actual = updated['variants'][variant_index][field] if variant_index is not None else updated[field]
                if actual != fact:raise ValueError('商品检查没有通过，请重新保存')
                self.w.activate(store_id, revision, True)
                return {'applied':True,'kind':kind,'revision':revision,'product':selected['title'],
                        'message':selected['title']+'的'+('库存' if kind=='stock' else '价格')+'已更新，后续回复使用新资料。',
                        'sent':False}
            if not isinstance(value,str) or not 1<=len(value.strip())<=2000:
                raise ValueError('请填写1–2000字的正确答案')
            value = redact(value.strip())
            if re.search(r'卖完|售罄|缺货|没货|库存|\d+(?:\.\d+)?\s*[元块]|优惠|赔[付偿]|退款|保证.{0,8}(?:到|送)|必[到达]',value):
                raise ValueError('这段包含库存、价格或经营承诺。库存和价格请在上方切换修改；其他承诺请在店铺规则中确认。')
            safe = set(result.get('intents',[])) <= {'unknown','faq','details'}
            if not safe:
                raise ValueError('这条涉及商品或经营资料，请切换“库存不对”“价格不对”，或在更多设置中修改对应资料。')
            if not product_id and re.search(r'这个|这根|这件|那个|前一个|上一款',original['question']):
                raise ValueError('这句话没有说清商品，请先选择适用商品')
            feedback = LearningLoop(kb).record(int(interaction_id),value)
            active_faq=kb.active('faq')
            previous_ids=[row.get('feedback_id') for row in (active_faq['payload'] if active_faq else [])
                          if row['question']==original['question'] and row.get('product_ids',[])==([product_id] if product_id else []) and row.get('feedback_id')]
            entry = {'question':original['question'],'answer':value,'review_required':True,
                     'source_type':'merchant_correction','feedback_id':feedback}
            if product_id:entry['product_ids']=[product_id]
            revision = kb.stage_incremental('faq',[entry],'店主确认的正确答复',store_id in self.w.fixtures)
            # Scope edits affect the validation case, never rewrite historical input.
            state = json.loads(original['input_state'])
            if product_id:state['product_id']=product_id
            else:state.pop('product_id',None)
            with kb.connect() as db:
                db.execute('UPDATE feedback SET revision_id=?,evaluation_state=? WHERE id=?',
                    (revision,json.dumps(state,ensure_ascii=False),feedback))
                old_statuses={}
                for fid in previous_ids:
                    old=db.execute('SELECT status FROM feedback WHERE id=?',(fid,)).fetchone()
                    if old:old_statuses[fid]=old['status']
                for fid in old_statuses:db.execute("UPDATE feedback SET status='retired' WHERE id=?",(fid,))
            def restore_feedback():
                with kb.connect() as db:
                    db.execute("UPDATE feedback SET status='retired' WHERE id=?",(feedback,))
                    for fid,status in old_statuses.items():db.execute('UPDATE feedback SET status=? WHERE id=?',(status,fid))
            try:
                report = self.w.evaluate(store_id,revision,use_model=False)
                if report['passed']:self.w.activate(store_id,revision,True)
            except Exception:
                restore_feedback()
                raise
            if not report['passed']:
                restore_feedback()
                return {'applied':False,'revision':revision,'message':'已保存，检查没有通过，暂未改变回复。请核对适用商品和答案。','sent':False}
            return {'applied':True,'revision':revision,'message':'已保存并生效，后续相同问题会使用这个答案。','sent':False}

    def save_card(self, store_id, card_id, value):
        with self.w._lock:
            card = self.w.card(store_id,int(card_id))
            if card['card_type'] in ('semantic','platform_data','media','product_link'):
                raise ValueError('这条需要检查商品关联或消息读取，填写答案无法修复；请在更多设置中查看详情。')
            attributes=[f.split('.',1)[1] for f in card['fields'] if f.startswith('attributes.')]
            payload={'form':'faq','value':value,'source':'店主确认','question':card['latest_question']}
            if attributes:
                if len(attributes)!=1:raise ValueError('这条涉及多项商品资料，请在更多设置中逐项补充。')
                payload.update(form='attribute',field=attributes[0],product_id=card['product_id'])
            elif set(card['fields']) & {'lead_time','origin'}:
                if len(card['fields'])!=1:raise ValueError('请在店铺规则中分别填写发货时间和发货地。')
                payload.update(form='shipping',field=card['fields'][0])
            elif set(card['fields']) - {'intent','faq_expired'}:
                raise ValueError('这条需要更新商品或经营资料，请在更多设置中查看缺少哪一项。')
            if payload['form']=='faq' and (not isinstance(value,str) or re.search(r'卖完|售罄|缺货|没货|库存|\d+(?:\.\d+)?\s*[元块]|优惠|赔[付偿]|退款|保证.{0,8}(?:到|送)',value)):
                raise ValueError('库存和价格请在上方直接修改，不能存为固定答案；其他经营承诺请在店铺规则中确认。')
            self.w.card_action(store_id,int(card_id),'fill_form',payload)
            checked=self.w.card_action(store_id,int(card_id),'retest',{})
            if not checked['report']['passed']:
                return {'applied':False,'message':'已保存，检查没有通过，暂未改变回复。请核对填写内容。','sent':False}
            self.w.activate(store_id,checked['revision_id'],True)
            return {'applied':True,'message':'已保存并生效，后续回复会使用新资料。','sent':False}
