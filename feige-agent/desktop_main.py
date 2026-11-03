# -*- coding: utf-8 -*-
"""PyInstaller entry point: double-click opens the desktop console."""
import sys

if '--self-test-trace' in sys.argv and '--self-test-report' in sys.argv:
    import faulthandler
    from pathlib import Path
    _trace_path = Path(sys.argv[sys.argv.index('--self-test-report') + 1]).with_suffix('.trace.log')
    _trace_file = _trace_path.open('w', encoding='utf-8')
    faulthandler.enable(file=_trace_file)
    faulthandler.dump_traceback_later(20, file=_trace_file)

from app.desktop import run_desktop


if __name__ == "__main__":
    if "--experience" in sys.argv:
        import webview
        from app.config import DATA_DIR
        from app.service.experience import open_experience
        from app.branding import APP_ICON, configure_desktop_identity
        configure_desktop_identity()
        open_experience(DATA_DIR / 'sandbox')
        webview.start(icon=str(APP_ICON))
        sys.exit(0)
    if "--self-test-qwen" in sys.argv:
        import json
        from pathlib import Path
        from app.service.model_check import run_live_check
        try:
            report = run_live_check()
        except Exception:
            report = {"ok": False, "error": "千问验收未完成，请在模型设置检查连接状态"}
        if "--self-test-report" in sys.argv:
            Path(sys.argv[sys.argv.index("--self-test-report")+1]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(0 if report["ok"] else 1)
    if "--self-test-offline" in sys.argv:
        import json
        import tempfile
        from pathlib import Path
        from app.config import StoreRegistry
        from app.desktop import UI_DIR
        from app.service.workbench import Workbench

        report = {"ok": False}
        try:
            with tempfile.TemporaryDirectory(prefix="feige-release-check-") as temp:
                root = Path(temp)
                workbench = Workbench(root / "reception", root / "stores",
                                      StoreRegistry(root / "registry.json", root / "stores"))
                response = workbench.chat("demo_billiards", "黑杆多少钱")
                assert "199" in response["draft_reply"]
                assert response["demo"] and not response["model_connected"]
                workbench.create_store("release_check", "打包自检店铺")
                recommendation = workbench.chat("demo_billiards", "两百以内推荐一根")
                assert "199" in recommendation["draft_reply"]
                assert recommendation["state"]["summary"]["confirmed"]["budget"]["max"] == 200
                workbench.chat("demo_billiards", "湖南，明天比赛来得及不")
                assert len(workbench.knowledge("demo_billiards").transcript("default")) == 6
                unknown = workbench.chat("release_check", "能刻字吗")
                feedback = workbench.correct("release_check", unknown["interaction_id"], "本店不提供刻字服务")
                candidate = workbench.propose_correction("release_check", feedback["feedback_id"])
                assert workbench.evaluate("release_check", candidate["id"])["passed"]
                workbench.activate("release_check", candidate["id"], True)
                assert "不提供刻字" in workbench.chat("release_check", "能刻字吗")["draft_reply"]
                source_file = root / "merchant-export.json"
                source_file.write_text('[{"product_id":"A","title":"测试商品","price":99,"stock":1}]', encoding="utf-8")
                workbench.sync("release_check").register(str(source_file), auto_publish=True)
                assert workbench.sync("release_check").poll(force=True)[0]["status"] == "published"
                for name in ("index.html", "app.js", "workbench.html", "workbench.js", "workbench.css", "merchant.js"):
                    assert (UI_DIR / name).is_file(), name
                from app.multistore import MultiStoreSupervisor
                workbench.create_store('taobao_check','淘宝资料自检',platform='taobao')
                kb = workbench.knowledge('taobao_check')
                kb.activate(kb.stage('products',[{'product_id':'A','title':'黑色台球杆','aliases':['黑杆'],'price':149,'stock':1}], '构造的商品资料'),reviewed=True)
                common = workbench.save_common_product('taobao_check','CUE','{"材质":"枫木"}','构造的厂家规格表',True)
                linked = workbench.apply_common_product('taobao_check','A',common['revision'],True)
                assert kb.active('products')['payload'][0].get('attributes') is None
                workbench.activate('taobao_check',linked['revision']['id'],True)
                assert '枫木' in workbench.chat('taobao_check','黑杆什么材质')['draft_reply']
                supervisor = MultiStoreSupervisor(specs=[workbench.spec('taobao_check')])
                try:
                    assert supervisor.boot_store('taobao_check')['state']=='connection_required'
                    assert not supervisor.agents
                finally:
                    supervisor.shutdown()
                content = json.dumps({'store_id':'taobao_check','conversations':[{'conversation_id':'test','messages':[
                    {'message_id':'1','role':'buyer','content':'看看黑色台球杆'},
                    {'message_id':'2','role':'buyer','content':'多少钱'}]}]},ensure_ascii=False)
                assert workbench.import_conversations('taobao_check',content)['inserted']==2
                assert workbench.import_conversations('taobao_check',content)['inserted']==0
                draft = workbench.generate_imported_draft('taobao_check','test')
                assert '149' in draft['draft_reply'] and not draft['sent']
                from app.channels.pinduoduo import Account, normalize_frame
                workbench.create_store('pdd_check','拼多多联调自检',platform='pinduoduo')
                spec = workbench.spec('pdd_check')
                account = Account('100', '200', '构造店铺')
                workbench.live.bind(spec, account.key)
                event, reason = normalize_frame(json.dumps({'response':'push','message':{
                    'msg_id':'check1','from':{'role':'user','uid':'300'},'to':{'role':'mall_cs','uid':'200'},
                    'type':0,'content':'这款多少钱','ts':1800000000000}}), account)
                assert reason=='text' and workbench.live.ingest(spec,account.key,event)==1
                assert workbench.live.ingest(spec,account.key,event)==0
                head=workbench.live.head(spec,'300')['sequence']
                workbench.inbox.save_draft(spec,'300',head,{'action':'reply','draft_reply':'构造的测试回复'})
                token=workbench.live.reserve(spec,account.key,'300',head,'构造的测试回复')
                workbench.live.finish(token,'delivery_unknown')
                workbench.live.close_uncertain(spec,'300',token,True)
                assert workbench.live.delivery(token)['state']=='manually_closed' and not workbench.live.delivery(token)['sent']
                products=workbench.knowledge('pdd_check')
                products.activate(products.stage('products',[{'product_id':'123','title':'构造的台球杆','price':149}],
                                                  '离线构造的本店资料'),reviewed=True)
                card_event,reason=normalize_frame(json.dumps({'response':'push','message':{
                    'msg_id':'card1','from':{'role':'user','uid':'300'},'to':{'role':'mall_cs','uid':'200'},
                    'type':0,'sub_type':0,'info':{'goodsID':'123','goodsPrice':'0.01'},
                    'content':'https://mobile.yangkeduo.com/goods.html?goods_id=123','ts':'1800000001000'}}),account)
                assert reason=='product_card' and card_event.timestamp==1800000001000
                workbench.live.ingest(spec,account.key,card_event)
                question,_=normalize_frame(json.dumps({'response':'push','message':{
                    'msg_id':'follow1','from':{'role':'user','uid':'300'},'to':{'role':'mall_cs','uid':'200'},
                    'type':0,'content':'这个多少钱','ts':'1800000002000'}}),account)
                workbench.live.ingest(spec,account.key,question)
                card_draft=workbench.generate_imported_draft('pdd_check','300')
                assert '149' in card_draft['draft_reply'] and not card_draft['sent'] and not card_draft['can_send']
                assert normalize_frame(json.dumps({'response':'push','message':{
                    'from':{'role':'user','uid':'300'},'to':{'role':'mall_cs','uid':'200'},'type':20}}),account)[1]=='read_receipt'
                assert workbench.info('pdd_check')['connection']['capabilities']['experimental_connection']
                assert not workbench.info('pdd_check')['connection']['runtime_available']
                from app.channels.pdd_wire import receive_envelopes
                import base64,struct
                assert receive_envelopes(base64.b64encode(struct.pack('>HHIII',0,0,0,0,0)).decode(),2)==[]
                from app.channels.catalog_capture import parse_products,LIST_PATH
                captured=parse_products(LIST_PATH,{'success':True,'result':{'onSaleGoods':[
                    {'goodsId':123,'goodsName':'构造商品','minOnSaleGroupPrice':14900,'maxOnSaleGroupPrice':19900}]}})[0]
                assert 'price' not in captured and 'stock' not in captured and '平台团购价范围' in captured['attributes']
                from app.channels.qianniu_native import QianniuNative,NativeCompatibilityError
                try:
                    QianniuNative().preflight(None)
                except NativeCompatibilityError:
                    pass
                else:
                    raise AssertionError('未验证消息身份不能发送')
                from app.channels.lease import ConnectionLease
                first,second=ConnectionLease(workbench._workspace,spec),ConnectionLease(workbench._workspace,spec)
                first.acquire()
                try:
                    try:second.acquire()
                    except ValueError:pass
                    else:raise AssertionError('同店连接必须独占')
                finally:first.release();second.release()
                from app.channels.reception_worker import ChannelReceptionWorker
                assert not ChannelReceptionWorker(workbench,'pdd_check').status()['running']
                from app.channels.history_context import parse_history
                past=parse_history({'success':True,'result':{'messages':[
                    {'msg_id':'history-q','from':{'role':'user','uid':'300'},'to':{'role':'mall_cs','uid':'200'},
                     'type':0,'content':'台球杆怎么保养','ts':1799999998000},
                    {'msg_id':'history-a','from':{'role':'mall_cs','uid':'200'},'to':{'role':'user','uid':'300'},
                     'type':0,'content':'使用干布擦拭','ts':1799999999000}]}},account)
                old_head=workbench.live.head(spec,'300')['sequence']
                workbench.history.ingest(spec,account.key,past)
                assert workbench.live.head(spec,'300')['sequence']==old_head
                history_versions=workbench.stage_observed_history('pdd_check',past)
                assert len(history_versions)==1 and workbench.knowledge('pdd_check').active('faq') is None
                assert workbench.stage_observed_history('pdd_check',past)==history_versions
                assert '149' in workbench.generate_imported_draft('pdd_check','300')['draft_reply']
                assert not workbench.automation.status(spec)['enabled']
                try:
                    workbench.start_channel_monitor('pdd_check','store',[],True)
                except ValueError:
                    pass
                else:
                    raise AssertionError('没有本店开启记录不能全店自动接待')
                from app.desktop import DesktopAPI
                from app.notifications import HandoffOutbox
                from types import SimpleNamespace
                desktop_supervisor=MultiStoreSupervisor(specs=[],registry=workbench.registry)
                desktop_supervisor.notifications=HandoffOutbox(root/'desktop-notifications.jsonl')
                desktop=DesktopAPI(desktop_supervisor,SimpleNamespace(status=lambda:{'embedded':False}),
                                   SimpleNamespace(_workbench=workbench))
                existing_pilots=set(workbench._pilots)
                added=desktop.add_store('pinduoduo','构造的多平台工作台店铺')
                assert added['ok'] and set(workbench._pilots)==existing_pilots and not desktop_supervisor.agents
                assert desktop.get_state()['online']==0
                sid=added['store_id'];workbench.chat(sid,'能刻字吗',session='original-gap')
                card=workbench.cards(sid)['cards'][0]
                proposed=workbench.card_action(sid,card['id'],'fill_form',{'form':'faq','value':'本店暂不提供刻字服务'})
                assert workbench.knowledge(sid).active('faq') is None
                assert workbench.card_action(sid,card['id'],'retest',{})['report']['passed']
                workbench.activate(sid,proposed['revision_id'],True)
                assert '不提供刻字' in workbench.chat(sid,'能刻字吗',session='new-gap-check')['draft_reply']
                assert workbench.card(sid,card['id'])['status']=='observing'
                from app.service.experience import Experience
                from app.channels.sandbox import Sandbox
                experience=Experience(root/'account-free')
                assert not experience.status()['prepared']
                assert experience.prepare('demo_billiards')['products']==3
                turn=experience.chat('demo_billiards','能刻字吗')
                change=experience.preview_correction('demo_billiards',turn['interaction_id'],'本店不提供刻字服务')
                experience.confirm_correction('demo_billiards',change['revision'])
                assert '不提供刻字' in experience.chat('demo_billiards','能刻字吗')['reply']
                experience.rollback('demo_billiards')
                experience.clear_chat('demo_billiards')
                assert '不提供刻字' not in experience.chat('demo_billiards','能刻字吗')['reply']
                sandbox=Sandbox(root/'network-free')
                sandbox.add_store('sandbox_packaged')
                sandbox.enqueue('sandbox_packaged',sandbox.packet('message1','黑杆多少钱'))
                assert sandbox.drain()[0]['state']=='received'
                assert '199' in sandbox.draft('sandbox_packaged')['draft_reply']
                receipt=sandbox.send('sandbox_packaged',fault='delayed')
                assert not receipt['sent'] and sandbox.echo(receipt['token'])['sent']
                assert not sandbox.summary()['customer_messages_sent']
                for asset in ('experience.html','experience.css','experience.js'):
                    assert (UI_DIR/asset).is_file()
                from app.service.merchant_edits import MerchantEdits
                edits=MerchantEdits(workbench)
                turn=workbench.chat('release_check','可以定制包装吗',session='simple-edit')
                ctx=edits.context('release_check',turn['interaction_id'])
                saved=edits.save('release_check',turn['interaction_id'],'faq','本店暂不提供定制包装','',None,ctx['versions'])
                assert saved['applied'] and not saved['sent']
                assert '不提供定制包装' in workbench.chat('release_check','可以定制包装吗',session='simple-new')['draft_reply']
                turn=workbench.chat('demo_billiards','巧粉还有货吗',session='simple-stock')
                ctx=edits.context('demo_billiards',turn['interaction_id'])
                assert edits.save('demo_billiards',turn['interaction_id'],'stock','0','CHALK',None,ctx['versions'])['applied']
                assert '无货' in workbench.chat('demo_billiards','巧粉还有货吗',session='simple-stock-new')['draft_reply']
                desktop_supervisor.shutdown();workbench.close_connections()
                report = {"ok": True, "checks": ["模拟试聊", "新店接入", "离线模式标识", "预算与摘要", "原文归档", "纠错回归启用", "文件增量同步", "界面资源", "多平台账户与启动边界", "共用商品待核对启用", "导入会话幂等与草稿", "单色商品价格依据", "拼多多报文与账号绑定", "实时事件持久化去重", "试接入与正式自动接待隔离"]}
                report['checks'] += ['新版二进制协议模块','团购区间与规格价格隔离','千牛未验证消息禁发','同店连接独占','持续接待默认停止','未知发送人工关闭不算成功']
                report['checks'] += ['字符串消息时间','商品卡片关联本店资料','已读回执不触发回复']
                report['checks'] += ['历史上下文不替代新消息','历史问答仅生成待审核版本']
                report['checks'] += ['全店接待默认关闭与测试前置校验']
                report['checks'] += ['主窗口多平台添加默认不接待','常用文字资料候选与复测发布','模拟补全保持观察']
                report['checks'] += ['无账号独立体验资料','体验纠错确认与回退','模拟传输迟到回显','首次体验页面资源']
                report['checks'] += ['简化纠错一键生效且不发送','库存纠错更新商品事实','简化纠错页面资源']
        except Exception as exc:
            report = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        if "--self-test-report" in sys.argv:
            target = Path(sys.argv[sys.argv.index("--self-test-report") + 1])
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(0 if report["ok"] else 1)
    if "--lab" in sys.argv:
        from app.service.workbench_api import run_workbench
        run_workbench()
        raise SystemExit(0)
    if "--self-test" in sys.argv:
        from app.agent import FeigeAgent
        from app.config import StoreRegistry

        specs = StoreRegistry().load()
        if not specs:
            raise SystemExit(2)
        agent = FeigeAgent.from_spec(specs[0])
        agent.shutdown()
        raise SystemExit(0)
    run_desktop()
