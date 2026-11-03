"""Explicit live replay against retained 1.5 replies, without production stores."""
import argparse
import copy
import json
import sys
import tempfile
from datetime import datetime,timedelta,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from compare_reception_versions import CASES,grade,compact
from app.service.workbench import Workbench
from app.service.qwen import model_manager
from app import __version__
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-live',action='store_true')
    args=parser.parse_args()
    if not args.run_live:parser.error('需要显式 --run-live')
    previous=json.loads((ROOT/'docs/verification/comparison-1.4-vs-1.5-20261018.json').read_text(encoding='utf-8'))
    old={row['id']:row for row in previous['results']}
    output=ROOT/'docs/verification/upgrade-1.6.0-live.json'
    checkpoint=output.with_suffix('.jsonl');checkpoint.write_text('',encoding='utf-8')
    results=[]
    with tempfile.TemporaryDirectory(prefix='feige-upgrade-') as temp:
        root=Path(temp);wb=Workbench(root/'reception',root/'shops',models=model_manager())
        kb=wb.knowledge('demo_billiards')
        faq=[{'question':'可以刻字吗','answer':'模拟政策：本店不提供刻字服务'},
             {'question':'一盒巧粉有几块','answer':'模拟资料：每盒2块','product_ids':['CHALK'],'requires_context':True},
             {'question':'现在买杆送杆盒吗','answer':'赠送杆盒','expires_at':'2020-01-01'}]
        kb.activate(kb.stage('faq',faq,'与1.5同一组构造资料',demo=True),allow_demo=True)
        for original in CASES:
            spec=copy.deepcopy(original)
            # Preserve the previous review's two explicit corrections in the rubric.
            if spec['id']=='T044':spec['forbidden'].append('巧克粉')
            if spec['id']=='T056':spec['missing']=[]
            start=time.monotonic();response=wb.chat(spec['store'],spec['question'],spec['session'],demo_order=spec['order'])
            score=grade(spec,response)
            row={'id':spec['id'],'question':spec['question'],'group':spec['group'],
                 'previous':old[spec['id']]['new'],
                 'current':{**compact(response,time.monotonic()-start),**score,'understanding':response['understanding']}}
            if not response['model_connected']:row['current']['passed']=False
            results.append(row)
            with checkpoint.open('a',encoding='utf-8') as file:file.write(json.dumps(row,ensure_ascii=False)+'\n')
            if len(results)%10==0:print(f"完成 {len(results)}/58，当前通过 {sum(r['current']['passed'] for r in results)}",flush=True)
        # Demonstrate store-controlled response changes without new invented facts.
        wb.save_sales_policy('demo_billiards',{'enabled':True,'goal':'help_choose','tone':'warm','next_step':'offer_help'})
        adapted=wb.chat('demo_billiards','黑杆多少钱','policy-demo')
        assert '199' in adapted['draft_reply'] and '需要的话' in adapted['draft_reply']
        assert wb.info('demo_billiards')['commerce']['live_receptions']==0
    report={'version':__version__,'tested_at':datetime.now(timezone(timedelta(hours=9))).isoformat(),
            'synthetic_only':True,'total':len(results),'passed':sum(r['current']['passed'] for r in results),
            'policy_demo_reply':adapted['draft_reply'],'results':results}
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False,indent=2))
    return 0 if report['passed']==report['total'] else 1


if __name__=='__main__':raise SystemExit(main())
