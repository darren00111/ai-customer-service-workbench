"""Two real model calls, with synthetic handoff history and no shop traffic."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.service.qwen import model_manager
from selftest_store_automation import StoreAutomationTests


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-live',action='store_true')
    if not parser.parse_args().run_live:parser.error('Requires --run-live; at most two paid calls with synthetic records')
    models=model_manager()
    if not models.status().get('configured') or not models.status().get('enabled'):
        raise SystemExit('模型未配置或未启用，没有执行调用')
    cases=[]
    for name,question in [('new_sales_question','黑杆多少钱'),('refund_followup','啥时候到')]:
        f=StoreAutomationTests();f.setUp()
        try:
            worker=f.worker(f.enable())
            f.question('300','我要退款，钱啥时候到账');worker.tick()
            desktop=f.desktop();event=desktop._supervisor.notifications.list_pending()[0]
            assert desktop.mark_notification_done(event['event_id'])['conversation_resumed']
            f.w.models=models
            f.question('300',question);f.p.poll()
            result=f.w.generate_imported_draft('p1','300')
            passed=bool(result.get('model_connected') and not result['sent'] and
                (result['action']=='reply' and '149' in result['draft_reply'] if name=='new_sales_question'
                 else result['action']=='handoff' and result['category']=='handoff_after_sale'))
            cases.append({'case':name,'passed':passed,'action':result['action'],'category':result['category'],
                          'draft_reply':result['draft_reply'],'model_connected':result.get('model_connected'),
                          'metrics':result.get('model_metrics',{}),'sent':False})
            print(('PASS ' if passed else 'FAIL ')+name,flush=True)
            if not result.get('model_connected'):break
        finally:f.tearDown()
    report={'verified_at':datetime.now(timezone.utc).isoformat(),'ok':len(cases)==2 and all(c['passed'] for c in cases),
            'cases':cases,'planned_model_calls':2,'real_shop_acceptance':False,'customer_messages_sent':False,
            'boundary':'真实千问调用，历史、商品和处理标记均为构造数据；不证明真实平台收发或实际店铺效果'}
    (ROOT/'docs/verification/store-handoff-qwen-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
