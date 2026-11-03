"""Explicit, bounded Qwen check using constructed multi-platform store data."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import StoreRegistry
from app.service.qwen import model_manager
from app.service.workbench import Workbench


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-live',action='store_true')
    parser.add_argument('--case',default='',help='仅执行指定的店铺/场景')
    args=parser.parse_args()
    if not args.run_live:parser.error('需要显式 --run-live，最多4次真实模型调用')
    models=model_manager()
    if not models.status()['configured'] or not models.status()['enabled']:
        raise SystemExit('本机未启用已配置模型，未执行真实调用')
    results=[]
    with tempfile.TemporaryDirectory(prefix='feige-channel-live-') as temp:
        root=Path(temp)
        registry=StoreRegistry(root/'registry.json',root/'stores')
        wb=Workbench(root/'reception',root/'stores',registry,models=models)
        for store,platform,price in [('tb','taobao',149),('pdd','pinduoduo',129)]:
            wb.create_store(store,'构造资料验证店','台球用品',platform,'test_brand')
            kb=wb.knowledge(store)
            version=kb.stage('products',[{'product_id':'CUE','title':'黑色台球杆','aliases':['黑杆'],
                 'price':price,'stock':5,'attributes':{'材质':'枫木'}}],'构造的测试商品资料')
            kb.activate(version,reviewed=True)
        cases=[('tb','price','多少米',149,'reply'),('pdd','price','几钱',129,'reply'),
               ('tb','context','这根是啥做的','枫木','reply'),('tb','aftersale','收到就坏了，我要退货',None,'handoff')]
        if args.case:
            cases=[row for row in cases if row[0]+'/'+row[1]==args.case]
            if not cases:parser.error('未找到指定场景')
        for store,conversation,question,expected,action in cases:
            wb.import_conversations(store,json.dumps({'store_id':store,'conversations':[{
                'conversation_id':conversation,'messages':[
                    {'message_id':'m1','role':'buyer','content':'我想看看黑色台球杆'},
                    {'message_id':'m2','role':'agent','content':'您想了解哪方面？'},
                    {'message_id':'m3','role':'buyer','content':question}]}]},ensure_ascii=False))
            result=wb.generate_imported_draft(store,conversation)
            passed=bool(result.get('model_connected') and result['action']==action and
                        (expected is None or str(expected) in result['draft_reply']) and result['sent'] is False)
            results.append({'store':store,'case':conversation,'passed':passed,'action':result['action'],
                            'draft_reply':result['draft_reply'],'model_connected':result['model_connected'],
                            'model_status':result.get('model_status',''),
                            'understanding':result.get('understanding'), 'missing':result.get('missing'),
                            'metrics':result.get('model_metrics'), 'sent':result['sent']})
            print(('PASS' if passed else 'FAIL')+' '+store+'/'+conversation)
            if not result.get('model_connected'):
                print(result.get('model_status') or '本机模型未连接，停止其余真实调用')
                break
    report={'ok':len(results)==len(cases) and all(row['passed'] for row in results),'planned':len(cases),'cases':results,
            'boundary':'构造商品与会话，仅验证真实模型和本地核心，不代表平台消息已接入或真实店铺准确率'}
    (ROOT/'docs/verification/multichannel-qwen-live.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
