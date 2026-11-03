"""Store-local, auditable adaptation using sent replies and trusted outcomes.

Only preapproved wording variants can be selected automatically. Business facts
remain in the versioned knowledge store; purchase correlation is not causation.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone

from .knowledge import now, redact

DEFAULT_SALES = {"enabled": False, "goal":"answer_first", "tone":"concise",
                 "next_step":"none", "learning_mode":"observe", "min_samples":30,
                 "observation_days":7, "preferred_product_ids":[], "focus_attributes":[]}


def validate_sales(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULT_SALES):
        raise ValueError("店铺接待策略字段无效")
    result = {**DEFAULT_SALES, **value}
    choices = {"goal":{"answer_first","help_choose"}, "tone":{"concise","warm"},
               "next_step":{"none","offer_help"}, "learning_mode":{"observe","assist","auto"}}
    if type(result['enabled']) is not bool or any(not isinstance(result[k],str) or result[k] not in options for k,options in choices.items()):
        raise ValueError("接待策略选项无效")
    for key,lo,hi in (("min_samples",20,1000),("observation_days",1,30)):
        if type(result[key]) is not int or not lo <= result[key] <= hi:
            raise ValueError("学习样本门槛或观察天数无效")
    for key in ('preferred_product_ids','focus_attributes'):
        if not isinstance(result[key],list) or len(result[key])>20 or any(not isinstance(x,str) or not 1<=len(x)<=80 for x in result[key]):
            raise ValueError("策略中的商品或属性列表无效")
    return result


def policy_fingerprint(business):
    value=json.loads(json.dumps(business,ensure_ascii=False))
    for control in ('learning_mode','min_samples','observation_days'):
        value.get('sales',{}).pop(control,None)
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]


def commercial_context(business,products):
    from .data import checked_fact
    # Routine inventory counts and source timestamps must not restart a whole
    # learning cohort. Prices, assortment, product facts and shop terms do.
    catalog=[]
    for product in products:
        price,_=checked_fact(product.get('price'),'price','context')
        attrs,_=checked_fact(product.get('attributes'),'attributes','context')
        attrs=attrs if isinstance(attrs,dict) else {}
        catalog.append({'id':product['product_id'],'title':product['title'],'price':price,
                        'type':product.get('product_type',''),
                        'variants':[{**{k:v.get(k) for k in ('sku_id','color','size')},'price':checked_fact(v.get('price'),'price','context')[0]} for v in product.get('variants',[])],
                        'attributes':{key:checked_fact(value,key,'context')[0] for key,value in attrs.items()}})
    return hashlib.sha256(json.dumps([policy_fingerprint(business),sorted(catalog,key=lambda x:x['id'])],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]


def choose_variant(store_id,session_id,policy,versions,winner=None):
    if policy['learning_mode'] == 'observe':
        return policy['next_step']
    if policy['learning_mode'] == 'auto' and winner:
        return winner
    # Assignment remains stable during a whole session, across process restarts.
    digest=hashlib.sha256((store_id+'|'+session_id+'|'+json.dumps(versions,sort_keys=True)).encode()).digest()
    return ('none','offer_help')[digest[0]%2]


def apply_sales(result,business,store_id,session_id,versions,winner=None,products=None):
    policy=validate_sales(business.get('sales',{}))
    result['sales']={'enabled':policy['enabled'],'policy_id':policy_fingerprint(business),
                     'variant':'none','experiment':False,'prompt_added':False,
                     'context_id':commercial_context(business,products) if products is not None else ''}
    if not policy['enabled'] or result['action']!='reply' or not result['automatic_resolution'] or not result['evidence']:
        return
    variant=choose_variant(store_id,session_id,policy,{'context':result['sales']['context_id']} if products is not None else versions,winner)
    if policy['goal']!='help_choose':
        variant='none'
    result['sales'].update(variant=variant,experiment=policy['learning_mode']!='observe' and not winner and policy['goal']=='help_choose')
    if policy['tone']=='warm':
        result['draft_reply']='帮您核对好了：\n'+result['draft_reply']
    state=result['state']
    if variant=='offer_help' and not state.get('sales_prompt_shown') and policy['goal']=='help_choose':
        result['draft_reply']+='\n需要的话，我可以继续帮您核对合适的款式、规格和到货时间。'
        state['sales_prompt_shown']=True
        result['sales']['prompt_added']=True


class CommerceLearning:
    def __init__(self,kb,clock=None):
        self.kb=kb
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        with kb.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sales_exposures (
              interaction_id INTEGER PRIMARY KEY,session_id TEXT,channel TEXT,variant TEXT,
              policy_id TEXT,versions TEXT,experiment INTEGER,created_at TEXT);
            CREATE TABLE IF NOT EXISTS sales_outcomes (
              event_id TEXT PRIMARY KEY,order_hash TEXT,session_id TEXT,event_type TEXT,
              occurred_at TEXT,origin TEXT,trusted INTEGER,digest TEXT,received_at TEXT);
            CREATE TABLE IF NOT EXISTS sales_insights (
              insight_key TEXT PRIMARY KEY,kind TEXT,detail TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS sales_coverage (
              start_at TEXT,end_at TEXT,origin TEXT,created_at TEXT,UNIQUE(start_at,end_at,origin));
            ''')
            if 'context_id' not in {row['name'] for row in db.execute('PRAGMA table_info(sales_exposures)')}:
                db.execute("ALTER TABLE sales_exposures ADD COLUMN context_id TEXT DEFAULT ''")

    def record_reception(self,interaction_id,session_id,result):
        sales=result.get('sales',{})
        if not sales.get('enabled') or result.get('action')!='reply' or not result.get('automatic_resolution'):
            return
        # Simulation/default/unconfirmed drafts cannot enter live experiments.
        channel=result.get('delivery_channel','simulation')
        if result.get('demo') or not result.get('model_connected'):
            channel='simulation'
        with self.kb.connect() as db:
            db.execute('INSERT OR IGNORE INTO sales_exposures VALUES(?,?,?,?,?,?,?,?,?)',
                       (interaction_id,session_id,channel,sales.get('variant','none'),sales.get('policy_id',''),
                        json.dumps(result.get('knowledge_versions',{}),sort_keys=True),int(sales.get('experiment',False)),self.clock().isoformat(),sales.get('context_id','')))

    def ingest(self,events,origin='merchant_reported',trusted=False):
        if origin not in {'merchant_reported','merchant_export','authorized_connector'} or type(trusted) is not bool:
            raise ValueError('成交数据来源无效')
        if origin=='merchant_reported' and trusted:
            raise ValueError('手工标记不能视为已核实订单')
        if not isinstance(events,list) or len(events)>10000:
            raise ValueError('每批成交事件最多10000条')
        prepared=[]
        for event in events:
            if not isinstance(event,dict) or set(event)-{'store_id','event_id','session_id','order_id','event_type','occurred_at'}:
                raise ValueError('成交事件字段无效')
            if event.get('store_id')!=self.kb.store_id:
                raise ValueError('成交数据不能跨店导入')
            for key in ('event_id','session_id','order_id'):
                if not isinstance(event.get(key),str) or not 1<=len(event[key])<=100:
                    raise ValueError('成交事件需提供有效的事件、订单及会话标识')
            if event.get('event_type') not in {'paid','cancelled','refunded'}:
                raise ValueError('成交事件仅支持已付款、已取消或已退款')
            try:
                stamp=datetime.fromisoformat(event['occurred_at'].replace('Z','+00:00'))
                if stamp.tzinfo is None or stamp>self.clock()+timedelta(minutes=5):
                    raise ValueError()
            except (KeyError,TypeError,ValueError,AttributeError):
                raise ValueError('成交时间需为带时区的有效时间，不能晚于当前时间') from None
            packed=json.dumps(event,sort_keys=True,ensure_ascii=False)
            prepared.append((event['event_id'],hashlib.sha256((self.kb.store_id+'|'+event['order_id']).encode()).hexdigest(),
                             event['session_id'],event['event_type'],stamp.astimezone(timezone.utc).isoformat(),origin,int(trusted),
                             hashlib.sha256((packed+origin+str(trusted)).encode()).hexdigest(),now()))
        added=0;upgraded=0
        with self.kb.connect() as db:
            for row in prepared:
                previous=db.execute('SELECT * FROM sales_outcomes WHERE event_id=?',(row[0],)).fetchone()
                if previous and previous['digest']!=row[7]:
                    identical=tuple(previous[key] for key in ('event_id','order_hash','session_id','event_type','occurred_at','origin'))==row[:6]
                    if not identical:
                        raise ValueError('相同事件标识的内容或来源发生冲突，整批未导入')
                    if trusted and not previous['trusted']:
                        db.execute('UPDATE sales_outcomes SET trusted=1,digest=? WHERE event_id=?',(row[7],row[0]));upgraded+=1
                if not previous:
                    conflict=db.execute('SELECT event_type FROM sales_outcomes WHERE order_hash=? AND occurred_at=? AND event_type<>?',(row[1],row[4],row[3])).fetchone()
                    if conflict:
                        raise ValueError('同一订单同一时间存在冲突状态，请核对来源')
                    binding=db.execute('SELECT session_id FROM sales_outcomes WHERE order_hash=? LIMIT 1',(row[1],)).fetchone()
                    if binding and binding['session_id']!=row[2]:
                        raise ValueError('同一订单不能绑定多个会话')
                    db.execute('INSERT INTO sales_outcomes VALUES(?,?,?,?,?,?,?,?,?)',row)
                    added+=1
        return {'received':len(events),'added':added,'upgraded':upgraded,'duplicates':len(events)-added-upgraded,'origin':origin,'trusted':trusted}

    def ingest_export(self,payload,origin='merchant_export',trusted=False):
        coverage=None
        if isinstance(payload,dict):
            if set(payload)-{'store_id','events','coverage'} or payload.get('store_id')!=self.kb.store_id:
                raise ValueError('订单导出结构或店铺无效')
            coverage=payload.get('coverage')
            events=payload.get('events')
        else:
            events=payload
        if coverage is not None:
            try:
                if not trusted or origin=='merchant_reported' or set(coverage)!={'from','through','complete'} or coverage['complete'] is not True:
                    raise ValueError()
                start=datetime.fromisoformat(coverage['from'].replace('Z','+00:00'))
                end=datetime.fromisoformat(coverage['through'].replace('Z','+00:00'))
                if not start.tzinfo or not end.tzinfo or start>=end or end>self.clock():
                    raise ValueError()
            except (TypeError,ValueError,AttributeError,KeyError):
                raise ValueError('只有商家确认的完整订单来源才能声明有效覆盖区间') from None
        result=self.ingest(events,origin,trusted)
        if coverage is not None:
            with self.kb.connect() as db:
                db.execute('INSERT OR IGNORE INTO sales_coverage VALUES(?,?,?,?)',
                           (start.astimezone(timezone.utc).isoformat(),end.astimezone(timezone.utc).isoformat(),origin,now()))
        return result

    def metrics(self,business,versions,products=None):
        policy=validate_sales(business.get('sales',{}))
        pid=policy_fingerprint(business)
        version_key=json.dumps(versions,sort_keys=True)
        context_id=commercial_context(business,products) if products is not None else None
        with self.kb.connect() as db:
            exposures=[dict(x) for x in db.execute('SELECT * FROM sales_exposures ORDER BY created_at,interaction_id')]
            outcomes=[dict(x) for x in db.execute('SELECT * FROM sales_outcomes ORDER BY occurred_at,event_id')]
            interactions=db.execute('SELECT result FROM interactions ORDER BY id DESC LIMIT 1000').fetchall()
            coverage=[(datetime.fromisoformat(r['start_at']),datetime.fromisoformat(r['end_at'])) for r in db.execute('SELECT * FROM sales_coverage ORDER BY start_at')]
        # One session contributes one trial under its first eligible assignment.
        cohorts={}
        for row in exposures:
            matching_context=row['context_id']==context_id if context_id is not None else row['versions']==version_key
            if row['channel']!='live_confirmed' or row['policy_id']!=pid or not matching_context or not row['experiment']:
                continue
            cohorts.setdefault(row['session_id'],row)
        cutoff=self.clock()-timedelta(days=policy['observation_days'])
        cohorts={s:r for s,r in cohorts.items() if self.clock()-timedelta(days=90)<=datetime.fromisoformat(r['created_at'])<=cutoff}
        def covered(row):
            start=datetime.fromisoformat(row['created_at']);end=start+timedelta(days=policy['observation_days'])
            for lo,hi in coverage:
                if lo>start:break
                if hi>start:start=hi
                if start>=end:return True
            return False
        uncovered=sum(not covered(row) for row in cohorts.values())
        cohorts={s:r for s,r in cohorts.items() if covered(r)}
        latest={}
        paid_at={}
        for row in outcomes:
            if not row['trusted']:
                continue
            if row['event_type']=='paid':
                paid_at.setdefault(row['order_hash'],row)
            latest[row['order_hash']]=row
        paid_sessions=set()
        for order,row in latest.items():
            session=row['session_id']; paid=paid_at.get(order); exposure=cohorts.get(session)
            if row['event_type']!='paid' or not paid or not exposure:
                continue
            start=datetime.fromisoformat(exposure['created_at']); when=datetime.fromisoformat(paid['occurred_at'])
            if start<=when<=start+timedelta(days=policy['observation_days']):
                paid_sessions.add(session)
        groups={variant:{'sessions':0,'paid_sessions':0} for variant in ('none','offer_help')}
        for session,row in cohorts.items():
            group=groups[row['variant']]
            group['sessions']+=1; group['paid_sessions']+=int(session in paid_sessions)
        for group in groups.values():
            group['observed_rate']=round(group['paid_sessions']/group['sessions'],4) if group['sessions'] else None
        def interval(k,n):
            if not n:return (0,1)
            z=1.96;p=k/n;den=1+z*z/n
            center=(p+z*z/(2*n))/den;half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
            return center-half,center+half
        winner=None
        if min(g['sessions'] for g in groups.values())>=policy['min_samples']:
            for name,other in (('none','offer_help'),('offer_help','none')):
                if interval(groups[name]['paid_sessions'],groups[name]['sessions'])[0]>interval(groups[other]['paid_sessions'],groups[other]['sessions'])[1]:
                    winner=name
        needs=Counter()
        for row in interactions:
            result=json.loads(row['result'])
            if result.get('delivery_channel')=='live_confirmed' and not result.get('demo'):
                needs.update(result.get('intents',[]))
        return {'policy':policy,'policy_id':pid,'groups':groups,'recommended_variant':winner,
                'auto_applied_variant':winner if policy['learning_mode']=='auto' else None,
                'live_receptions':sum(x['channel']=='live_confirmed' for x in exposures),
                'simulation_receptions':sum(x['channel']!='live_confirmed' for x in exposures),
                'reported_events':len(outcomes),'trusted_events':sum(x['trusted'] for x in outcomes),
                'awaiting_complete_outcome_data':uncovered,
                'needs':dict(needs.most_common(8)),'observation_days':policy['observation_days'],
                'note':'仅统计同一经营条件、观察期已结束且订单来源声明完整覆盖的真实确认发送会话。日常库存更新不清空学习，价格、商品范围或政策变化重新分组。缺订单不直接算未成交。手工标记不参与自动调整；相关性不能证明因果或保证成交。'}

    def refresh(self):
        from .learning import LearningLoop
        generated=[]
        loop=LearningLoop(self.kb)
        with self.kb.connect() as db:
            feedbacks=[dict(row) for row in db.execute("SELECT * FROM feedback WHERE status='pending' AND revision_id IS NULL AND ('feedback:'||id) NOT IN (SELECT insight_key FROM sales_insights) ORDER BY id LIMIT 100")]
        for feedback in feedbacks:
            try:
                revision=loop.propose(feedback['id'],self.kb.store_id.startswith('demo_'))
                generated.append(revision['id'])
            except ValueError:
                with self.kb.connect() as db:
                    detail={'question':'接待纠错 #%s'%feedback['id'],'occurrences':1,'missing':[],
                            'suggestion':'请修正对应商品、政策或处理方式；该纠错已保留为回归案例'}
                    db.execute('INSERT OR IGNORE INTO sales_insights VALUES(?,?,?,?)',('feedback:'+str(feedback['id']),'correction_requires_data',json.dumps(detail,ensure_ascii=False),now()))
        with self.kb.connect() as db:
            for task in self.kb.tasks():
                key='gap:'+str(task['id'])
                detail={'question':redact(task['question']),'occurrences':task['occurrences'],
                        'missing':task['missing'],'suggestion':'补全对应商品/政策资料，核对后启用；不从顾客或未知答复中推导事实'}
                db.execute('INSERT INTO sales_insights VALUES(?,?,?,?) ON CONFLICT(insight_key) DO UPDATE SET detail=excluded.detail',
                           (key,'knowledge_gap',json.dumps(detail,ensure_ascii=False),now()))
        return {'generated_revisions':generated}

    def insights(self):
        with self.kb.connect() as db:
            return [{**dict(row),'detail':json.loads(row['detail'])} for row in db.execute('SELECT * FROM sales_insights ORDER BY created_at DESC LIMIT 50')]
