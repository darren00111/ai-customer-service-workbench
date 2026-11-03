"""At most four paid Qwen calls; all products/dialogues are constructed."""
import argparse
import copy
import json
from pathlib import Path
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.service.data import SnapshotDataSource
from app.service.dialogue import ReceptionService
from app.service.qwen import model_manager
from selftest_product_cards import PRODUCTS,reference


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-live',action='store_true');args=parser.parse_args()
    if not args.run_live:parser.error('需要 --run-live，最多4次真实千问调用，仅使用构造资料')
    models=model_manager();status=models.status()
    if not status.get('configured') or not status.get('enabled'):
        raise SystemExit('本机模型未配置或未启用，未执行调用')
    service=ReceptionService(SnapshotDataSource('constructed_card_check',{},copy.deepcopy(PRODUCTS)),interpreter=models.interpreter())
    base={'product_id':'A','product_history':['A','B']}
    cases=[('implicit','这个多少米',reference(),base,'B','279'),
           ('named','不问白杆了，改看黑杆，几钱',reference(),base,'A','149'),
           ('previous','还是前一根，多少钱',reference(),base,'A','149'),
           ('unmapped','这张卡片的咋卖，报个价',reference('999','unknown'),base,None,None)]
    results=[]
    for name,text,ref,state,pid,price in cases:
        history=[{'role':'buyer','content':'之前看了黑杆，又发了白杆的商品卡片'},
                 {'role':'agent','content':'请问想了解哪方面？'},{'role':'buyer','content':text}]
        result=service.respond(text,copy.deepcopy(state),messages=history,platform_product=ref,live=True)
        passed=bool(result['model_connected'] and result['product_id']==pid and
                    ((price in result['draft_reply']) if price else ('149' not in result['draft_reply'] and '279' not in result['draft_reply'])))
        results.append({'case':name,'passed':passed,'product_id':result['product_id'],'action':result['action'],
                        'draft_reply':result['draft_reply'],'understanding':result['understanding'],
                        'model_connected':result['model_connected'],'model_status':result.get('model_status',''),
                        'metrics':result.get('model_metrics',{}),'sent':False})
        print(('PASS ' if passed else 'FAIL ')+name,flush=True)
        if not result['model_connected']:break
    report={'verified_at':datetime.now(timezone.utc).isoformat(),'ok':len(results)==4 and all(r['passed'] for r in results),
            'planned_calls':4,'cases':results,'real_store_acceptance':False,'customer_messages_sent':False,
            'boundary':'真实模型调用，全部为构造商品与对话；不证明平台接入、真实准确率或成单提升'}
    (ROOT/'docs/verification/product-cards-qwen-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
