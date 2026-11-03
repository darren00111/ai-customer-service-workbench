"""Resolve observed product references only within the current shop catalog."""
import re


def resolve_product(products,platform,remote_id):
    matches=[p['product_id'] for p in products if p['product_id']==remote_id or
             (isinstance(p.get('platform_ids'),dict) and p['platform_ids'].get(platform)==remote_id)]
    return matches[0] if len(matches)==1 else None


def product_history(products,platform,references):
    output=[]
    for row in references:
        value=resolve_product(products,platform,row['product_id']) or '@unmapped:'+platform+':'+row['product_id']
        if not output or output[-1]!=value:output.append(value)
    return output[-8:]


def prepare_reference(state,products,reference):
    if reference is None:return None
    if (not isinstance(reference,dict) or set(reference)-{'platform','remote_product_id','message_id','latest_is_card'} or
        reference.get('platform') not in ('pinduoduo','taobao','feige','xiaohongshu') or
        not isinstance(reference.get('remote_product_id'),str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}',reference['remote_product_id']) or
        not isinstance(reference.get('message_id'),str) or not 1<=len(reference['message_id'])<=200 or
        type(reference.get('latest_is_card',False)) is not bool):
        raise ValueError('平台商品引用缺少可信的来源标识')
    pid=resolve_product(products,reference['platform'],reference['remote_product_id'])
    source={k:reference[k] for k in ('platform','remote_product_id','message_id')}
    source['local_product_id']=pid
    prior=state.get('platform_product_reference')
    changed=source!=prior
    if changed:
        for key in ('product_id','variant','comparison_products','product_type','platform_reference_overridden'):
            state.pop(key,None)
        if pid:state['product_id']=pid
        visited=state.setdefault('product_history',[])
        value=pid or '@unmapped:'+reference['platform']+':'+reference['remote_product_id']
        if not visited or visited[-1]!=value:visited.append(value)
        del visited[:-8]
    state['platform_product_reference']=source
    return {**source,'latest_is_card':reference.get('latest_is_card',False)}


def apply_reference(understanding,state,reference,text):
    if reference is None:return
    # A plain card has no text from which a model could name a different product.
    bare_card=reference['latest_is_card'] and '\n' not in text
    if understanding.clear_product and not bare_card:
        state['platform_reference_overridden']=reference['message_id']
        return
    if not bare_card and understanding.product_reference in ('named','previous','comparison'):
        state['platform_reference_overridden']=reference['message_id']
        return
    if state.get('platform_reference_overridden')==reference['message_id'] and not bare_card:
        return
    pid=reference['local_product_id']
    understanding.product_id=pid
    understanding.clear_product=pid is None
    if bare_card:
        understanding.variant={}
        if understanding.intents==['unknown'] and state.get('pending_intents'):
            understanding.intents=list(state['pending_intents'])
    if pid is None and set(understanding.intents)&{'price','inventory','details','compare'}:
        understanding.needs_clarification=True
        understanding.product_ids=[]
        understanding.faq_index=None
