"""Two bounded model calls with constructed history; no merchant API traffic."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.service.qwen import model_manager
from selftest_history_context import HistoryTests,BASE,raw


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-live',action='store_true')
    if not parser.parse_args().run_live:parser.error('Requires --run-live; at most two paid calls, constructed data only')
    models=model_manager()
    if not models.status().get('configured') or not models.status().get('enabled'):
        raise SystemExit('模型尚未配置或启用，没有执行调用')
    results=[]
    for name,offset in [('recent_history',1000),('expired_history',7200000)]:
        f=HistoryTests();f.setUp()
        try:
            kb=f.w.knowledge('p1')
            products=kb.active('products')['payload']+[{'product_id':'B','title':'白色台球杆','aliases':['白杆'],'price':279,'stock':2}]
            kb.activate(kb.stage('products',products,'构造商品资料'),reviewed=True)
            f.ingest(f.card(BASE-offset),raw('wrong-old-quote','agent','这个之前口头说999元，不确定',BASE-offset+100))
            f.w.models=models
            result=f.w.generate_imported_draft('p1','300')
            passed=bool(result.get('model_connected') and not result['sent'] and
                (result['product_id']=='A' and '149' in result['draft_reply'] and '999' not in result['draft_reply']
                 if name=='recent_history' else result.get('product_id') is None and '149' not in result['draft_reply'] and '279' not in result['draft_reply']))
            results.append({'case':name,'passed':passed,'action':result['action'],'product_id':result.get('product_id'),
                            'draft_reply':result['draft_reply'],'model_connected':result.get('model_connected'),
                            'metrics':result.get('model_metrics',{}),'sent':False})
            print(('PASS ' if passed else 'FAIL ')+name,flush=True)
            if not result.get('model_connected'):break
        finally:f.tearDown()
    report={'verified_at':datetime.now(timezone.utc).isoformat(),'ok':len(results)==2 and all(r['passed'] for r in results),
            'cases':results,'planned_calls':2,'real_shop_acceptance':False,'customer_messages_sent':False,
            'boundary':'真实模型调用，全部为构造商品与历史；不证明真实平台协议或店铺效果'}
    (ROOT/'docs/verification/history-context-qwen-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
