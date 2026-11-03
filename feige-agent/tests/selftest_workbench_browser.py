"""Real browser + real local workbench backend, using disposable merchant data."""
from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock,patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import StoreRegistry
from app.desktop import UI_DIR
from app.engine import browser
from app.engine.cdp import CDPClient
from app.service.workbench import Workbench
from app.service.workbench_api import WorkbenchAPI
from app.service.qwen import ModelManager


def main():
    checks = []
    def check(label, value):
        checks.append((label, bool(value)))
        print(("PASS " if value else "FAIL ") + label)

    with tempfile.TemporaryDirectory(prefix="feige-workbench-browser-") as temp:
        root = Path(temp)
        models = ModelManager(root / "model.json", protect=lambda b:b[::-1], unprotect=lambda b:b[::-1])
        api = WorkbenchAPI(Workbench(root / "reception", root / "stores",
                                     StoreRegistry(root / "registry.json", root / "stores"), models=models))
        allowed = {"list_stores", "get_info", "chat", "stage_knowledge", "preview_revision", "activate_revision", "create_store",
                   "get_transcript", "record_correction", "propose_correction", "retire_feedback", "evaluate_knowledge",
                   "resolve_task", "register_sync", "sync_now", "enable_sync"}
        allowed.add("export_regressions")
        allowed.update({"get_model_settings", "save_model_settings", "test_model_connection"})
        allowed.update({'save_sales_policy','refresh_learning','import_outcomes'})
        allowed.update({'list_platforms','save_common_product','apply_common_product','import_conversations',
                        'get_imported_conversation','pause_imported_conversation','generate_imported_draft'})
        allowed.update({'pilot_command','send_live_draft'})
        allowed.update({'start_channel_monitor','stop_channel_monitor'})
        allowed.add('get_initial_store')
        allowed.add('get_initial_section')
        allowed.update({'get_correction_context','save_reply_edit','save_card_answer'})
        allowed.add('capture_product_revision')
        allowed.add('close_uncertain_delivery')
        allowed.add('set_store_automation')
        allowed.update({'list_cards','get_card','card_action','record_override','list_overrides','publish_history','rollback_knowledge'})

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(UI_DIR), **kwargs)

            def log_message(self, *_):
                pass

            def translate_path(self,path):
                asset=path.split('?',1)[0]
                if asset in ('/assets/app-icon.svg','/assets/app-icon.png','/assets/app-icon.ico'):
                    return str(UI_DIR.parent/asset.lstrip('/'))
                return super().translate_path(path)

            def do_POST(self):
                method = self.path.removeprefix("/api/")
                if method not in allowed or not callable(getattr(api,method,None)):
                    self.send_error(404)
                    return
                args = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                output = json.dumps(getattr(api, method)(*args), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(output)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        executable = browser.find_chrome()
        if not executable:
            raise RuntimeError("需要 Chrome 或 Edge 执行界面验收")
        proc = subprocess.Popen([executable, "--headless=new", "--disable-gpu", "--no-first-run",
            "--remote-debugging-port=%d" % port, "--user-data-dir=%s" % (root / "browser"),
            "http://127.0.0.1:%d/workbench.html" % server.server_port],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cdp = None
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not browser.cdp_alive(port):
                time.sleep(.1)
            cdp = CDPClient(port)
            cdp.connect(prefer="/workbench.html")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    if cdp.evaluate("document.readyState==='complete' && typeof renderMerchant === 'function' && !!document.getElementById('chatForm')"):
                        break
                except Exception:
                    pass
                time.sleep(.05)
            cdp.evaluate("""window.testErrors=[]; window.addEventListener('error', e=>testErrors.push(e.message));
            window.until = async function(test) { const end=Date.now()+8000;
              while(!test()) { if(Date.now()>end) throw new Error('UI timeout'); await new Promise(r=>setTimeout(r,30)); } };
            window.pywebview={api:new Proxy({}, {get:(_,method)=>(...args)=>fetch('/api/'+method,
              {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(r=>r.json())})};
            window.dispatchEvent(new Event('pywebviewready'));
            until(()=>document.querySelectorAll('#storeSelect option').length===3);""")
            check("真实后端加载三类模拟店铺", cdp.evaluate("document.getElementById('storeSelect').options.length===3"))
            check('默认页面不展示回归、JSON和意图表单',cdp.evaluate("!$('advancedTools').open&&!document.body.innerText.includes('JSON')&&!document.body.innerText.includes('纠正意图')"))
            check('没有问题时不展示空补资料表单',cdp.evaluate("$('quickCards').hidden"))

            def ask(text):
                return cdp.evaluate("""(async()=>{
                  const before=document.querySelectorAll('.message').length;
                  document.getElementById('question').value=%s;
                  document.getElementById('chatForm').requestSubmit();
                  await until(()=>!pending && document.querySelectorAll('.message').length===before+2);
                  return document.querySelector('#messages .message:last-child').textContent;
                })()""" % json.dumps(text))

            ask('能定做球杆袋吗')
            cdp.evaluate("$('quickValue').value='本店暂不提供定做服务';$('quickCorrectionForm').requestSubmit();until(()=>!pending&&$('quickResult').textContent.includes('生效'))")
            check('一次保存后下一次问答真正生效','不提供定做' in ask('能定做球杆袋吗'))
            ask('巧粉还有货吗')
            check('自动选择商品和库存修改方式',cdp.evaluate("$('quickKind').value==='stock'&&JSON.parse($('quickProduct').value).product_id==='CHALK'"))
            cdp.evaluate("$('quickValue').value='0';$('quickCorrectionForm').requestSubmit();until(()=>!pending&&$('quickResult').textContent.includes('已更新'))")
            check('库存填0后实际回复无货','无货' in ask('巧粉还有货吗'))
            cdp.evaluate("$('quickValue').value='30';$('quickCorrectionForm').requestSubmit();until(()=>!pending&&$('quickResult').textContent.includes('已更新'))")
            check('简单操作后高级设置仍保持关闭',cdp.evaluate("!$('advancedTools').open"))
            cdp.call('Emulation.setDeviceMetricsOverride',{'width':1160,'height':900,'deviceScaleFactor':1,'mobile':False})
            shot=cdp.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
            (ROOT/'docs/verification/merchant-simple-desktop.png').write_bytes(base64.b64decode(shot['data']))
            check("试聊展示实际商品事实", "199" in ask("黑棍几钱，还有货吗"))
            check("追问收货地", "收货地" in ask("啥时候到"))
            check("多轮补充目的地后给出依据", "2–4天" in ask("湖南"))
            for width in (1160, 700, 320):
                cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900,
                         "deviceScaleFactor": 1, "mobile": False})
                check("%dpx 无横向溢出" % width,
                      cdp.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1160, "height": 1000,
                     "deviceScaleFactor": 1, "mobile": False})

            cdp.evaluate("""(async()=>{ document.getElementById('newStoreId').value='browser_shop';
              document.getElementById('newStoreName').value='界面测试店铺';
              document.getElementById('createForm').closest('details').open=true;
              document.getElementById('createForm').requestSubmit();
              await until(()=>!pending && currentStore==='browser_shop'); })()""")
            check("从界面创建真实格式店铺且自动发送关闭", cdp.evaluate("document.getElementById('readiness').textContent.includes('0 件商品')"))
            payload = json.dumps([{"product_id": "A", "title": "台球杆", "aliases": ["黑杆"], "price": 123, "stock": 4}], ensure_ascii=False)
            cdp.evaluate("""(async()=>{ document.getElementById('importKind').value='products';
              document.getElementById('source').value='模拟商品中心导出';
              document.getElementById('importContent').value=%s;
              document.getElementById('importForm').requestSubmit();
              await until(()=>!pending && previewedRevision!==null); })()""" % json.dumps(payload))
            check("导入仅生成待启用版本", cdp.evaluate("document.getElementById('activateButton').disabled && document.getElementById('readiness').textContent.includes('0 件商品')"))
            cdp.evaluate("""(async()=>{ document.getElementById('reviewed').checked=true;
              document.getElementById('reviewed').dispatchEvent(new Event('change'));
              document.getElementById('activateButton').click();
              await until(()=>!pending && document.getElementById('readiness').textContent.includes('1 件商品')); })()""")
            check("启用后下一轮实际使用新资料", "123" in ask("黑杆多少钱"))
            check("知识版本全文可预览", cdp.evaluate("document.querySelectorAll('#revisionSelect option').length===1"))
            cdp.evaluate("""(async()=>{ document.getElementById('transcriptButton').click();
              await until(()=>!pending && document.getElementById('transcript').textContent.includes('reference')); })()""")
            check("会话摘要与完整原文可以追溯", cdp.evaluate("document.getElementById('memorySummary').textContent.includes('references')"))

            export_file = root / "merchant-export.json"
            export_file.write_text('[{"product_id":"A","stock":0}]', encoding="utf-8")
            cdp.evaluate("""(async()=>{ document.getElementById('syncPath').value=%s;
              document.getElementById('syncAuto').checked=true;
              document.getElementById('syncForm').requestSubmit();
              await until(()=>!pending && document.querySelectorAll('#syncList .record-item').length===1);
              document.getElementById('syncNow').click();
              await until(()=>!pending && document.getElementById('syncReport').textContent.includes('published')); })()""" % json.dumps(str(export_file)))
            check("可信文件增量同步保留价格并更新库存", "无货" in ask("黑杆还有货吗"))
            check("同步后其他商品字段仍可用", "123" in ask("黑杆多少钱"))

            ask("能刻字吗")
            check("资料缺口生成问题卡片", cdp.evaluate("Array.from(document.getElementById('cardSelect').options).some(o=>o.value && o.text.includes('待处理'))"))
            cdp.evaluate("""(async()=>{ document.getElementById('correctionText').value='本店暂不提供刻字服务';
              document.getElementById('correctionForm').requestSubmit();
              await until(()=>!pending && document.getElementById('feedbackSelect').value!=='');
              document.getElementById('proposeCorrection').click();
              await until(()=>!pending && previewNeedsEvaluation); })()""")
            cdp.evaluate("document.getElementById('reviewed').checked=true; document.getElementById('reviewed').dispatchEvent(new Event('change'))")
            check("纠错知识未验证前不能启用", cdp.evaluate("document.getElementById('activateButton').disabled"))
            cdp.evaluate("""(async()=>{ document.getElementById('evaluatePreview').click();
              await until(()=>!pending && previewPassed);
              document.getElementById('activateButton').click();
              await until(()=>!pending && previewedRevision===null); })()""")
            check("纠错经回归和确认启用后改变实际答复", "暂不提供刻字" in ask("能刻字吗"))
            check("纠错回归启用后问题卡片进入观察（不直接算补全）", cdp.evaluate("Array.from(document.getElementById('cardSelect').options).some(o=>o.text.includes('能刻字') && o.text.includes('观察中'))"))
            cdp.evaluate("""(async()=>{ document.getElementById('exportCases').click();
              await until(()=>!pending && document.getElementById('regressionExport').textContent.includes('merchant_regression_cases')); })()""")
            check("可导出后续模型评测案例", cdp.evaluate("document.getElementById('regressionExport').textContent.includes('expected_action')"))
            cdp.evaluate("""(async()=>{document.getElementById('salesEnabled').checked=true;
              document.getElementById('salesGoal').value='help_choose';document.getElementById('salesNext').value='offer_help';
              document.getElementById('salesTone').value='warm';document.getElementById('salesForm').requestSubmit();
              await until(()=>!pending && document.getElementById('feedback').textContent.includes('策略已保存'));})()""")
            check('商家策略从界面保存并改变实际回复','需要的话' in ask('黑杆多少钱'))
            check('模拟记录不冒充真实成交会话',cdp.evaluate("document.getElementById('salesSummary').textContent.includes('真实确认接待 0 次')"))
            cdp.evaluate("""(async()=>{document.getElementById('refreshLearning').click();await until(()=>!pending);})()""")
            check('学习建议按钮实际调用后端',cdp.evaluate("document.getElementById('feedback').textContent.includes('学习建议已整理')"))
            check('当前模型回归可以由商家显式选择',cdp.evaluate("document.getElementById('useModelEvaluation').type==='checkbox'"))
            cdp.evaluate("document.getElementById('syncKind').value='outcomes';document.getElementById('syncKind').dispatchEvent(new Event('change'))")
            check('成交文件信任与商品自动发布分别控制',cdp.evaluate("!document.getElementById('syncOutcomesTrusted').disabled && document.getElementById('syncAuto').disabled"))
            check("交互过程中没有未处理的页面异常", cdp.evaluate("testErrors.length===0"))

            cdp.evaluate("""(async()=>{document.getElementById('modelSettings').open=true;
              document.getElementById('modelName').value='qwen3.8-flash';
              document.getElementById('modelEndpoint').value='https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1';
              document.getElementById('modelKey').value='sk-ui-test';
              document.getElementById('modelEnabled').checked=true;
              document.getElementById('modelForm').requestSubmit();
              await until(()=>!pending && currentModel.configured);})()""")
            check("模型设置保存后清空密钥输入并显示待验证", cdp.evaluate("document.getElementById('modelKey').value==='' && document.getElementById('modelBadge').textContent.includes('待验证')"))
            fixture_response = {"choices":[{"finish_reason":"stop","message":{"content":'{"intents":["greeting"]}'}}],"usage":{}}
            with patch("app.service.qwen._post", return_value=fixture_response):
                cdp.evaluate("""(async()=>{document.getElementById('testModel').click();
                  await until(()=>!pending && currentModel.connected);})()""")
            check("连接验证按钮实际调用后端并更新模型状态", cdp.evaluate("document.getElementById('modelBadge').textContent.includes('已验证')"))
            cdp.evaluate("""(async()=>{document.getElementById('modelEnabled').checked=false;
              document.getElementById('modelForm').requestSubmit();
              await until(()=>!pending && !currentModel.enabled);})()""")
            check("取消启用后恢复离线标识且密钥不出现在状态", cdp.evaluate("document.getElementById('modelBadge').textContent.includes('离线') && !JSON.stringify(currentModel).includes('sk-ui-test')"))
            cdp.evaluate("""(async()=>{
              document.getElementById('createForm').closest('details').open=true;
              document.getElementById('newStoreId').value='taobao_browser';
              document.getElementById('newStoreName').value='淘宝验证店（构造资料）';
              document.getElementById('newPlatform').value='taobao';
              document.getElementById('createForm').requestSubmit();
              await until(()=>!pending && currentStore==='taobao_browser');
            })()""")
            check('淘宝连接明确标记待验收并提供客户端检查',cdp.evaluate("document.getElementById('connectionStatus').textContent.includes('淘宝') && document.getElementById('connectionStatus').textContent.includes('待验收') && !document.getElementById('taobaoPilot').hidden && document.getElementById('pddPilot').hidden"))
            cdp.evaluate("""(async()=>{
              document.getElementById('importKind').value='products';
              document.getElementById('source').value='构造的淘宝商品资料';
              document.getElementById('importContent').value=%s;
              document.getElementById('importForm').requestSubmit();
              await until(()=>!pending && previewedRevision!==null);
              document.getElementById('reviewed').checked=true;
              document.getElementById('reviewed').dispatchEvent(new Event('change'));
              document.getElementById('activateButton').click();
              await until(()=>!pending && document.getElementById('readiness').textContent.includes('1 件商品'));
              document.getElementById('commonPanel').open=true;
              document.getElementById('commonKey').value='BLACK_CUE';
              document.getElementById('commonContent').value='{"材质":"枫木"}';
              document.getElementById('commonSource').value='构造的厂家规格表';
              document.getElementById('commonReviewed').checked=true;
              document.getElementById('commonForm').requestSubmit();
              await until(()=>!pending && document.getElementById('commonPreview').textContent.includes('枫木'));
            })()""" % json.dumps(payload))
            check('共用商品属性由界面保存且完整展示来源',cdp.evaluate("document.getElementById('commonPreview').textContent.includes('厂家规格表')"))
            cdp.evaluate("""(async()=>{
              document.getElementById('linkReviewed').checked=true;
              document.getElementById('linkForm').requestSubmit();
              await until(()=>!pending && previewedRevision!==null);
            })()""")
            check('同款关联生成待启用版本并保留本店价格',cdp.evaluate("document.getElementById('preview').textContent.includes('枫木') && document.getElementById('preview').textContent.includes('123') && document.getElementById('activateButton').disabled"))
            cdp.evaluate("""(async()=>{
              document.getElementById('reviewed').checked=true;
              document.getElementById('reviewed').dispatchEvent(new Event('change'));
              document.getElementById('activateButton').click();
              await until(()=>!pending && previewedRevision===null);
            })()""")
            check('启用关联版本后真实接待核心使用共用属性','枫木' in ask('黑杆是什么材质'))
            channel_payload=json.dumps({'store_id':'taobao_browser','conversations':[{'conversation_id':'browser_buyer','messages':[
                {'message_id':'m1','role':'buyer','content':'我看看黑杆'},
                {'message_id':'m2','role':'agent','content':'想了解哪方面？'},
                {'message_id':'m3','role':'buyer','content':'多少钱'}]}]},ensure_ascii=False)
            cdp.evaluate("""(async()=>{
              document.getElementById('channelPanel').open=true;
              document.getElementById('channelContent').value=%s;
              document.getElementById('channelImportForm').requestSubmit();
              await until(()=>!pending && document.getElementById('channelSelect').value==='browser_buyer');
              document.getElementById('draftChannel').click();
              await until(()=>!pending && document.getElementById('channelView').textContent.includes('123'));
            })()""" % json.dumps(channel_payload))
            check('导入上下文后生成本店价格草稿且明确未发送',cdp.evaluate("document.getElementById('channelView').textContent.includes('已发送') && document.getElementById('channelView').textContent.includes('false')"))
            cdp.evaluate("""(async()=>{document.getElementById('pauseChannel').click();
              await until(()=>!pending && document.getElementById('feedback').textContent.includes('已暂停本地草稿'));
              document.getElementById('draftChannel').click();await until(()=>!pending);
            })()""")
            check('人工暂停后无法生成新的导入草稿',cdp.evaluate("document.getElementById('feedback').textContent.includes('已暂停') && document.getElementById('feedback').classList.contains('error')"))
            check('新功能未产生页面异常',cdp.evaluate('testErrors.length===0'))
            cdp.evaluate("document.getElementById('pilotPanel').open=true")
            check('淘宝诊断与拼多多发送操作隔离',cdp.evaluate("!document.getElementById('pilotPanel').hidden && document.getElementById('liveSendPanel').hidden"))
            for width in (1160,700,320):
                cdp.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':1000,'deviceScaleFactor':1,'mobile':False})
                check('多平台资料与会话面板 %dpx 无横向溢出' % width,cdp.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            cdp.call('Emulation.setDeviceMetricsOverride',{'width':1160,'height':1000,'deviceScaleFactor':1,'mobile':False})
            cdp.evaluate("document.getElementById('modelSettings').open=false; document.getElementById('createForm').closest('details').open=false; feedback('多平台构造资料验证完成，实际消息尚未连接。'); document.getElementById('multiTitle').scrollIntoView()")
            image=cdp.call('Page.captureScreenshot',{'format':'png'})
            (ROOT/'docs/verification/multichannel-workbench.png').write_bytes(base64.b64decode(image['data']))
            # Exercise the PDD panel through the real bridge; remote operations stay mocked.
            cdp.evaluate("""(async()=>{document.getElementById('newStoreId').value='pdd_browser';
              document.getElementById('newStoreName').value='拼多多连接验证店（构造资料）';
              document.getElementById('newPlatform').value='pinduoduo';
              document.getElementById('createForm').requestSubmit();
              await until(()=>!pending && currentStore==='pdd_browser');})()""")
            check('拼多多试接入入口可见且审核发送默认隐藏',cdp.evaluate("!document.getElementById('pddPilot').hidden && document.getElementById('taobaoPilot').hidden && document.getElementById('liveSendPanel').hidden"))
            from app.channels.pinduoduo import Account
            pdd_pilot=api._workbench._pilot('pdd_browser')
            with patch.object(pdd_pilot,'inspect',return_value={'state':'awaiting_confirmation','account':{'mall_id':'100','user_id':'200','name':'构造测试店'},'bound':False}):
                cdp.evaluate("""(async()=>{document.getElementById('pilotInspect').click();await until(()=>!pending && document.getElementById('pilotIdentity').textContent.includes('100'));})()""")
            check('核对身份显示真实后端返回值',cdp.evaluate("document.getElementById('pilotIdentity').textContent.includes('构造测试店') && document.getElementById('pilotIdentity').textContent.includes('200') && !document.getElementById('pilotReviewed').checked"))
            cdp.evaluate("""(async()=>{document.getElementById('pilotBind').click();await until(()=>!pending);})()""")
            check('未核对不能绑定账号',cdp.evaluate("document.getElementById('feedback').classList.contains('error')"))
            check('持续接待默认仅草稿且未预选自动发送审核',cdp.evaluate("document.getElementById('channelMonitorMode').value==='draft' && !document.getElementById('monitorReviewed').checked"))
            cdp.evaluate("""(async()=>{document.getElementById('captureProducts').click();await until(()=>!pending);})()""")
            check('未绑定店铺无法读取商品候选',cdp.evaluate("document.getElementById('feedback').textContent.includes('先绑定') && document.getElementById('feedback').classList.contains('error')"))
            from types import SimpleNamespace
            from app.channels.catalog_capture import parse_products, LIST_PATH
            rows=parse_products(LIST_PATH,{'success':True,'result':{'onSaleGoods':[{'goodsId':123,'goodsName':'构造的商品采集样例',
                'minOnSaleGroupPrice':14900,'maxOnSaleGroupPrice':19900}]}})
            pdd_pilot.bound=True;pdd_pilot.account=Account('100','200','构造的测试店')
            pdd_pilot.catalog=SimpleNamespace(products={'123':rows[0]},error='',status=lambda:{'products':1,'error':''})
            try:
                with patch.object(pdd_pilot,'poll',return_value={}), patch.object(pdd_pilot,'_identity',return_value=pdd_pilot.account):
                    cdp.evaluate("""(async()=>{document.getElementById('captureProducts').click();
                      await until(()=>!pending && document.getElementById('preview').textContent.includes('构造的商品采集样例'));})()""")
                    check('商品采集通过真实后端生成待核对版本',cdp.evaluate("previewedRevision!==null && document.getElementById('activateButton').disabled && document.getElementById('preview').textContent.includes('149.00') && document.getElementById('readiness').textContent.includes('0 件商品')"))
                    cdp.evaluate("""(async()=>{document.getElementById('monitorReviewed').checked=true;
                      document.getElementById('channelMonitorForm').requestSubmit();
                      await until(()=>!pending && document.getElementById('channelMonitorStatus').textContent.includes('运行中'));})()""")
                    check('持续接待按钮启动真实后台草稿任务',api._workbench._channel_workers['pdd_browser'].status()['running'])
                    cdp.evaluate("""(async()=>{document.getElementById('stopChannelMonitor').click();
                      await until(()=>!pending && document.getElementById('channelMonitorStatus').textContent.includes('已停止'));})()""")
                    check('停止按钮结束真实后台接待任务',not api._workbench._channel_workers['pdd_browser'].status()['running'])
                    cdp.evaluate("""(async()=>{document.getElementById('channelMonitorMode').value='automatic';
                      document.getElementById('monitorReviewed').checked=true;document.getElementById('channelMonitorForm').requestSubmit();await until(()=>!pending);})()""")
                    check('没有指定真实测试顾客不能开启自动收发',cdp.evaluate("document.getElementById('feedback').classList.contains('error')") and not api._workbench._channel_workers['pdd_browser'].status()['running'])
                    from app.channels.pinduoduo import Incoming
                    live=api._workbench.live;spec=api._workbench.spec('pdd_browser')
                    live.bind(spec,pdd_pilot.account.key)
                    live.ingest(spec,pdd_pilot.account.key,Incoming('300','m1','buyer','构造的测试咨询',1800000000000,'reply'))
                    head=live.head(spec,'300')['sequence']
                    api._workbench.inbox.save_draft(spec,'300',head,{'action':'reply','draft_reply':'构造的测试回复'})
                    token=live.reserve(spec,pdd_pilot.account.key,'300',head,'构造的测试回复');live.finish(token,'delivery_unknown')
                    cdp.evaluate("""(async()=>{await refreshInfo();document.getElementById('channelSelect').value='300';
                      document.getElementById('viewChannel').click();await until(()=>!pending && !document.getElementById('uncertainDeliveryPanel').hidden);})()""")
                    check('未知发送显示核对入口且没有预选确认',cdp.evaluate("!document.getElementById('uncertainDeliveryPanel').hidden && !document.getElementById('uncertainReviewed').checked"))
                    cdp.evaluate("""(async()=>{document.getElementById('closeUncertainDelivery').click();await until(()=>!pending);})()""")
                    check('未人工核对不能关闭未知发送',live.delivery(token)['state']=='delivery_unknown' and cdp.evaluate("document.getElementById('feedback').classList.contains('error')"))
                    cdp.evaluate("""(async()=>{document.getElementById('uncertainReviewed').checked=true;
                      document.getElementById('closeUncertainDelivery').click();await until(()=>!pending && document.getElementById('uncertainDeliveryPanel').hidden);})()""")
                    check('核对后只关闭本轮并保留非成功状态',live.delivery(token)['state']=='manually_closed' and not live.delivery(token)['sent'])
                    api._workbench.history.ingest(spec,pdd_pilot.account.key,[Incoming('300','history-ui','agent',
                      '<script>window.historyXss=1</script>',1799999999000,'context')])
                    cdp.evaluate("""(async()=>{document.getElementById('viewChannel').click();await until(()=>!pending && document.getElementById('channelView').textContent.includes('history-ui'));})()""")
                    check('历史原文单独标明仅作上下文',cdp.evaluate("document.getElementById('channelView').textContent.includes('已观察历史仅作上下文')"))
                    check('历史正文按文字显示不执行脚本',cdp.evaluate("window.historyXss===undefined && document.getElementById('channelView').textContent.includes('<script>')"))
            finally:
                api._workbench.stop_channel_monitor('pdd_browser')
                pdd_pilot.bound=False;pdd_pilot.account=None;pdd_pilot.catalog=None
            check('商品采集和持续接待没有新增页面异常',cdp.evaluate('testErrors.length===0'))
            cdp.evaluate("document.getElementById('channelMonitorMode').value='draft';document.getElementById('monitorReviewed').checked=false;feedback('构造数据流程检查完成；真实店铺待验收。');")
            for width in (1160,700,320):
                cdp.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':1000,'deviceScaleFactor':1,'mobile':False})
                check('连接联调面板 %dpx 无横向溢出' % width,cdp.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            cdp.call('Emulation.setDeviceMetricsOverride',{'width':1160,'height':1000,'deviceScaleFactor':1,'mobile':False})
            cdp.evaluate("document.getElementById('pilotPanel').open=true;document.getElementById('multiTitle').scrollIntoView();")
            image=cdp.call('Page.captureScreenshot',{'format':'png'})
            (ROOT/'docs/verification/channel-pilots-workbench.png').write_bytes(base64.b64decode(image['data']))
            for width in (700, 320):
                cdp.call("Emulation.setDeviceMetricsOverride", {"width":width,"height":900,"deviceScaleFactor":1,"mobile":False})
                check("模型设置 %dpx 无横向溢出" % width, cdp.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            cdp.call("Emulation.setDeviceMetricsOverride", {"width":1160,"height":1000,"deviceScaleFactor":1,"mobile":False})
            cdp.evaluate("document.getElementById('modelSettings').open=false")

            # Return to demo for a shareable screenshot without any customer data.
            cdp.evaluate("""(async()=>{ document.getElementById('storeSelect').value='demo_billiards';
              document.getElementById('storeSelect').dispatchEvent(new Event('change'));
              await until(()=>!pending && currentStore==='demo_billiards'); })()""")
            cdp.evaluate("document.getElementById('createForm').closest('details').open=false")
            ask("黑棍几钱，还有货吗")
            image = cdp.call("Page.captureScreenshot", {"format": "png"})
            target = ROOT / "docs" / "verification" / "workbench-desktop.png"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(base64.b64decode(image["data"]))
            print("截图：" + str(target))
            # Test the store-wide flow in disposable fixtures through the actual
            # bridge and UI. This phase never calls a merchant or paid model.
            from selftest_store_automation import StoreAutomationTests
            f=StoreAutomationTests();f.setUp()
            original_api=api
            original_workbench=api._workbench
            try:
                api._workbench=f.w
                cdp.evaluate("(async()=>{await loadStores('p1');document.getElementById('pilotPanel').open=true;document.getElementById('storeAutomationPanel').open=true;})()")
                check('全店接待默认关闭且未预选确认',not f.w.automation.status(f.spec)['enabled'] and cdp.evaluate("!document.getElementById('storeAutomationReviewed').checked && document.getElementById('enableStoreAutomation').disabled"))
                cdp.evaluate("document.getElementById('storeAutomationReviewed').checked=true;document.getElementById('storeAutomationReviewed').dispatchEvent(new Event('change'))")
                check('勾选不能跳过本店自动收发测试',cdp.evaluate("document.getElementById('enableStoreAutomation').disabled"))
                f.complete_test()
                cdp.evaluate("(async()=>{await refreshInfo();document.getElementById('storeAutomationForm').requestSubmit();await until(()=>!pending && document.getElementById('channelMonitorStatus').textContent.includes('全店自动接待'));})()")
                check('开启按钮实际启动本店全店后台任务',f.w._channel_workers['p1'].status()['running'] and f.w._channel_workers['p1'].mode=='store')
                f.question('301')
                deadline=time.monotonic()+6
                while f.w._channel_workers['p1'].confirmed<1 and time.monotonic()<deadline:time.sleep(.05)
                check('全店任务实际处理测试名单外的新顾客',f.w._channel_workers['p1'].confirmed==1 and f.w.live.history(f.spec,'301')[0]['state']=='confirmed')
                cdp.evaluate("(async()=>{document.getElementById('disableStoreAutomation').click();await until(()=>!pending && document.getElementById('storeAutomationStatus').textContent.includes('已关闭'));})()")
                check('关闭按钮撤回本店设置并停止自动任务',not f.w.automation.status(f.spec)['enabled'] and not f.w._channel_workers['p1'].status()['running'])
                cdp.evaluate("feedback('构造数据的全店接待验证；真实店铺尚未验收。');document.getElementById('storeAutomationPanel').scrollIntoView();")
                image=cdp.call('Page.captureScreenshot',{'format':'png'})
                (ROOT/'docs/verification/store-automation-workbench.png').write_bytes(base64.b64decode(image['data']))
                check('全店接待面板没有新增页面异常',cdp.evaluate('testErrors.length===0'))

                f.w.set_store_automation('p1',True,True)
                desktop=f.desktop();desktop.open_workbench=Mock(return_value={'ok':True})
                api=desktop
                allowed.update({'get_state','start_store','stop_store','start_all','open_workbench','embed_store','release_embedded_page','set_embed_bounds','mark_notification_done'})
                cdp.call('Page.navigate',{'url':'http://127.0.0.1:%d/index.html'%server.server_port})
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    try:
                        if cdp.evaluate("typeof toggleStore==='function' && !!document.getElementById('storeActionButton')"):break
                    except Exception:pass
                    time.sleep(.03)
                cdp.evaluate("""window.testErrors=[];window.addEventListener('error',e=>testErrors.push(e.message));
                  window.until=async test=>{const end=Date.now()+8000;while(!test()){if(Date.now()>end)throw Error('main UI timeout');await new Promise(r=>setTimeout(r,30));}};
                  window.pywebview={api:new Proxy({}, {get:(_,method)=>(...args)=>fetch('/api/'+method,
                    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(r=>r.json())})};
                  window.dispatchEvent(new Event('pywebviewready'));until(()=>state.configured===2 && state.shops[0].store_id==='p1');""")
                check('主窗口读取实际多平台后台并提供本店启动入口',cdp.evaluate("document.getElementById('storeActionButton').textContent==='启动本店接待' && document.getElementById('browserHeading').textContent==='客服连接'"))
                cdp.evaluate("until(()=>document.querySelector('.brand-mark img').naturalWidth>0)")
                check('主窗口品牌图从本地资源正常载入',cdp.evaluate("document.querySelector('.brand-mark img').complete && document.querySelector('.brand-mark img').naturalWidth>0"))
                cdp.evaluate("(async()=>{document.getElementById('storeActionButton').click();await until(()=>!busy && document.getElementById('storeStatus').textContent==='自动接待中');})()")
                check('主窗口按钮启动全店任务并更新在线数',f.w._channel_workers['p1'].status()['running'] and cdp.evaluate("document.getElementById('onlineMetric').textContent==='1 / 2'"))
                check('拼多多启动不会误接飞鸽内嵌通道',desktop._embedder.attached==[])
                cdp.evaluate("(async()=>{document.getElementById('storeActionButton').click();await until(()=>!busy && document.getElementById('onlineMetric').textContent==='0 / 2');})()")
                check('主窗口停止按钮结束任务并断开本店连接',not f.w._channel_workers['p1'].status()['running'] and not f.p.bound)
                cdp.evaluate("(async()=>{Array.from(document.querySelectorAll('#shopList button')).find(b=>b.textContent.includes('p2')).click();document.getElementById('storeActionButton').click();await until(()=>!busy);})()")
                check('未开启的另一店仍进入连接页面不自动接待',desktop.open_workbench.call_args.args==('p2',) and 'p2' not in f.w._channel_workers)
                f.reconnect()
                cdp.evaluate("(async()=>{document.getElementById('startAllButton').click();await until(()=>!busy && document.getElementById('onlineMetric').textContent==='1 / 2');})()")
                check('全部启动保持店铺权限范围并说明未开启店铺',f.w._channel_workers['p1'].status()['running'] and 'p2' not in f.w._pilots and cdp.evaluate("document.getElementById('toast').textContent.includes('另 1 个店铺')"))
                cdp.evaluate("Array.from(document.querySelectorAll('#shopList button')).find(b=>b.textContent.includes('p1')).click();showToast('构造数据的多平台主窗口验证；真实店铺尚未验收。');")
                image=cdp.call('Page.captureScreenshot',{'format':'png'})
                (ROOT/'docs/verification/store-automation-desktop.png').write_bytes(base64.b64decode(image['data']))
                worker=f.w._channel_workers['p1'];f.question('300','我要退货')
                deadline=time.monotonic()+6
                while not desktop._supervisor.notifications.list_pending() and time.monotonic()<deadline:time.sleep(.05)
                first_alert=desktop._supervisor.notifications.list_pending()[0]
                cdp.evaluate("(async()=>{document.getElementById('refreshButton').click();await until(()=>!!document.querySelector('#attentionList .draft-send'));})()")
                check('售后显示本机人工待办且不能误用飞鸽代发',cdp.evaluate("document.querySelector('#attentionList .draft-send').disabled && document.querySelector('#attentionList .draft-send').textContent.includes('原平台')"))
                sent_before=f.cdp.send_calls
                cdp.evaluate("(async()=>{document.querySelector('#attentionList button.text-button').click();await until(()=>document.getElementById('attentionCount').textContent==='0');})()")
                check('主窗口标记人工完成后只恢复会话不重发旧问题',not f.w.inbox.get(f.spec,'300')['paused'] and f.w.live.head(f.spec,'300')['action']=='context' and f.cdp.send_calls==sent_before)
                f.question('300');deadline=time.monotonic()+6
                while worker.confirmed<1 and time.monotonic()<deadline:time.sleep(.05)
                check('人工完成后的新售前问题可继续自动回答',worker.confirmed==1 and '149' in f.latest_result()['draft_reply'])
                f.question('300','我要退货');deadline=time.monotonic()+6
                while not desktop._supervisor.notifications.list_pending() and time.monotonic()<deadline:time.sleep(.05)
                second_alert=desktop._supervisor.notifications.list_pending()[0]
                cdp.evaluate("(async()=>{document.getElementById('refreshButton').click();await until(()=>document.getElementById('attentionCount').textContent==='1');document.getElementById('attentionHeading').scrollIntoView();showToast('构造数据的售后闭环验证；真实店铺尚未验收。');})()")
                check('同一顾客再次售后会生成新人工待办',second_alert['event_id']!=first_alert['event_id'] and f.w.inbox.get(f.spec,'300')['paused'])
                image=cdp.call('Page.captureScreenshot',{'format':'png'})
                (ROOT/'docs/verification/store-handoff-desktop.png').write_bytes(base64.b64decode(image['data']))
                check('多平台主窗口没有新增页面异常',cdp.evaluate('testErrors.length===0'))
            finally:
                cdp.call('Page.navigate',{'url':'about:blank'})
                f.w.close_connections();api=original_api;api._workbench=original_workbench;f.tearDown()
            # New desktop onboarding uses a real disposable registry/workbench.
            # Platform login calls are intercepted; no merchant page is opened.
            import selftest_workspace as workspace_fixtures
            workspace=workspace_fixtures.WorkspaceTests();workspace.setUp()
            saved_api=api
            try:
                api=workspace.api
                api.open_workbench=Mock(return_value={'ok':True})
                allowed.update({'add_store','login_store','choose_qianniu_client'})
                cdp.call('Page.navigate',{'url':'http://127.0.0.1:%d/index.html'%server.server_port})
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    try:
                        if cdp.evaluate("typeof visibleShops==='function'"):break
                    except Exception:pass
                    time.sleep(.03)
                cdp.evaluate("""(async()=>{
                  window.testErrors=[];window.addEventListener('error',e=>testErrors.push(e.message));
                  window.until=async test=>{const end=Date.now()+8000;while(!test()){if(Date.now()>end)throw Error('workspace UI timeout');await new Promise(r=>setTimeout(r,30));}};
                  window.pywebview={api:new Proxy({}, {get:(_,method)=>(...args)=>fetch('/api/'+method,
                    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(r=>r.json())})};
                  window.dispatchEvent(new Event('pywebviewready'));
                  await until(()=>apiReady && state.configured===0);
                })()""")
                check('空店铺主窗口提供添加入口且登录按钮禁用',cdp.evaluate("!document.getElementById('addStoreButton').disabled && document.getElementById('loginStoreButton').disabled"))
                ids={}
                for number,platform in enumerate(('taobao','feige','pinduoduo'),1):
                    cdp.evaluate("""(async()=>{document.getElementById('addStoreButton').click();
                      document.getElementById('newStorePlatform').value=%s;
                      document.getElementById('newStorePlatform').dispatchEvent(new Event('change'));
                      document.getElementById('newStoreName').value=%s;
                      document.getElementById('addStoreForm').requestSubmit();
                      await until(()=>!document.getElementById('addStoreDialog').open && state.configured===%d);
                    })()"""%(json.dumps(platform),json.dumps('构造'+platform+'店'),number))
                    ids[platform]=next(s.store_id for s in workspace.supervisor.specs if s.platform==platform)
                check('主窗口实际添加三平台店铺并保持零接待任务',len(workspace.registry.load())==3 and not workspace.supervisor.agents and not workspace.w._pilots and cdp.evaluate("state.online===0"))
                cdp.evaluate("document.getElementById('platformFilter').value='taobao';document.getElementById('platformFilter').dispatchEvent(new Event('change'))")
                check('平台筛选只改变显示而不改变全店统计',cdp.evaluate("document.querySelectorAll('#shopList .shop-item').length===1 && document.getElementById('onlineMetric').textContent==='0 / 3'"))
                check('淘宝入口明确打开千牛且可设置安装位置',cdp.evaluate("document.getElementById('loginStoreButton').textContent==='打开千牛工作台' && !document.getElementById('chooseQianniuButton').hidden"))
                from app.channels.qianniu_launcher import QianniuClientNotFound
                with patch.object(workspace.w,'pilot_command',side_effect=[QianniuClientNotFound('请选择千牛'),{'state':'login_required'}]) as retry, \
                     patch.object(api,'choose_qianniu_client',return_value={'ok':True}) as chooser:
                    cdp.evaluate("(async()=>{document.getElementById('loginStoreButton').click();await until(()=>!busy && state.shops.some(s=>s.login.state==='verification_required'));})()")
                    check('找不到千牛时选择安装位置后重试打开',chooser.call_count==1 and retry.call_count==2 and not workspace.supervisor.agents)
                with patch.object(workspace.w,'pilot_command',side_effect=QianniuClientNotFound('请选择千牛')) as retry, \
                     patch.object(api,'choose_qianniu_client',return_value={'ok':False,'cancelled':True,'error':'已取消选择千牛程序'}) as chooser:
                    cdp.evaluate("document.getElementById('loginStoreButton').click()")
                    deadline=time.monotonic()+6
                    while (not chooser.called or cdp.evaluate('busy')) and time.monotonic()<deadline:time.sleep(.03)
                    check('取消选择不会重新启动千牛',chooser.call_count==1 and retry.call_count==1 and cdp.evaluate("document.getElementById('toast').textContent.includes('已取消')"))
                with patch.object(workspace.w,'pilot_command',return_value={'state':'login_required'}) as command:
                    cdp.evaluate("(async()=>{document.getElementById('loginStoreButton').click();await until(()=>!busy && state.shops.some(s=>s.login.state==='verification_required'));})()")
                    check('淘宝登录按钮走千牛入口且不启动飞鸽',command.call_args.args==(ids['taobao'],'open_login') and not workspace.supervisor.agents)
                cdp.evaluate("document.getElementById('platformFilter').value='feige';document.getElementById('platformFilter').dispatchEvent(new Event('change'))")
                check('未启动店铺也显示登录与验收准备状态',cdp.evaluate("!document.getElementById('detailList').hidden && document.getElementById('detailList').textContent.includes('登录／身份') && document.getElementById('detailList').textContent.includes('真实验收')"))
                check('抖店不会出现千牛安装位置按钮',cdp.evaluate("document.getElementById('chooseQianniuButton').hidden && document.getElementById('loginStoreButton').textContent==='打开登录入口'"))
                with patch('app.engine.browser.ensure_browser',return_value={'status':'launched'}) as launch:
                    cdp.evaluate("(async()=>{document.getElementById('loginStoreButton').click();await until(()=>!busy && state.shops.find(s=>s.platform==='feige').login.state==='verification_required');})()")
                    check('抖店登录按钮只准备本店独立窗口',launch.call_args.args[0]==workspace.w.spec(ids['feige']).cdp_port and not workspace.supervisor.agents)
                workspace.w.chat(ids['feige'],'能刻字吗',session='workspace-gap');api._knowledge_cache=(0,{})
                cdp.evaluate("(async()=>{document.getElementById('platformFilter').value='all';document.getElementById('platformFilter').dispatchEvent(new Event('change'));document.getElementById('refreshButton').click();await until(()=>document.querySelectorAll('#knowledgeQueueList button').length===1);})()")
                check('跨店知识队列显示实际漏答卡片',cdp.evaluate("document.getElementById('knowledgeQueueCount').textContent==='1' && document.getElementById('knowledgeQueueList').textContent.includes('构造feige店')"))
                cdp.evaluate("document.querySelector('#knowledgeQueueList button').click()")
                deadline=time.monotonic()+4
                while api.open_workbench.call_args is None and time.monotonic()<deadline:time.sleep(.02)
                check('补全按钮准确进入卡片所属店铺',api.open_workbench.call_args.args==(ids['feige'],'cards'))
                cdp.evaluate("document.getElementById('platformFilter').value='pinduoduo';document.getElementById('platformFilter').dispatchEvent(new Event('change'));document.getElementById('storeKnowledgeButton').click()")
                deadline=time.monotonic()+4
                while api.open_workbench.call_args.args!=(ids['pinduoduo'],'knowledge') and time.monotonic()<deadline:time.sleep(.02)
                check('知识库按钮使用当前平台店铺',api.open_workbench.call_args.args==(ids['pinduoduo'],'knowledge'))
                check('非飞鸽接待页面使用完整工作台宽度',cdp.evaluate("document.querySelector('.workspace').classList.contains('overview-mode') && document.getElementById('browserHost').hidden"))
                cdp.evaluate("document.getElementById('platformFilter').value='all';document.getElementById('platformFilter').dispatchEvent(new Event('change'));document.getElementById('knowledgeQueueHeading').scrollIntoView()")
                screenshot=cdp.call('Page.captureScreenshot',{'format':'png'})
                (ROOT/'docs/verification/multiplatform-workspace.png').write_bytes(base64.b64decode(screenshot['data']))
                check('多平台新增流程无页面异常',cdp.evaluate('testErrors.length===0'))
                api=workspace.control;api._initial_store=ids['feige'];api._initial_section='cards'
                cdp.call('Page.navigate',{'url':'http://127.0.0.1:%d/workbench.html'%server.server_port})
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    try:
                        if cdp.evaluate("typeof navigateToSection==='function'"):break
                    except Exception:pass
                    time.sleep(.03)
                cdp.evaluate("""(async()=>{
                  window.until=async test=>{const end=Date.now()+8000;while(!test()){if(Date.now()>end)throw Error('guided form timeout');await new Promise(r=>setTimeout(r,30));}};
                  window.pywebview={api:new Proxy({}, {get:(_,method)=>(...args)=>fetch('/api/'+method,
                    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(r=>r.json())})};
                  window.dispatchEvent(new Event('pywebviewready'));
                  await until(()=>currentStore===%s && document.getElementById('cardSelect').value!=='' && !pending);
                  document.getElementById('cardSelect').dispatchEvent(new Event('change'));
                  await until(()=>!pending && document.getElementById('cardFormQuestion').value.includes('刻字'));
                })()"""%json.dumps(ids['feige']))
                check('从主窗口可定位本店卡片并预填普通问题',cdp.evaluate("document.getElementById('cardFormType').value==='faq' && document.getElementById('cardFormQuestion').value==='能刻字吗'"))
                cdp.evaluate("""(async()=>{document.getElementById('cardFormValue').value='本店暂不提供刻字服务';
                  document.getElementById('cardFormSource').value='构造的店主确认';
                  document.querySelector('[data-card-action="fill_form"]').click();
                  await until(()=>!pending && document.getElementById('cardDetail').textContent.includes('待复测'));
                })()""")
                card=workspace.w.cards(ids['feige'])['cards'][0]
                check('普通文字表单生成候选且不改变正式答案',card['status']=='retest' and workspace.w.knowledge(ids['feige']).active('faq') is None)
                cdp.evaluate("""(async()=>{document.querySelector('[data-card-action="retest"]').click();
                  await until(()=>!pending && previewedRevision!==null && document.getElementById('cardDetail').textContent.includes('待发布'));
                })()""")
                check('卡片复测后进入预览并要求重新确认',cdp.evaluate("!document.getElementById('reviewed').checked && document.getElementById('activateButton').disabled"))
                cdp.evaluate("""(async()=>{document.getElementById('reviewed').checked=true;document.getElementById('reviewed').dispatchEvent(new Event('change'));
                  document.getElementById('activateButton').click();await until(()=>!pending && previewedRevision===null);
                })()""")
                check('表单补全经过复测和确认发布后用于后续回答','暂不提供刻字' in workspace.w.chat(ids['feige'],'能刻字吗',session='new-customer')['draft_reply'])
            finally:
                cdp.call('Page.navigate',{'url':'about:blank'});api=saved_api;workspace.doCleanups()
        finally:
            if cdp:
                try:
                    cdp.call('Browser.close', timeout=3)
                except Exception:
                    pass
                cdp.close()
            api._close()
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    print("通过 %d/%d 项" % (sum(v for _, v in checks), len(checks)))
    return 0 if all(v for _, v in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
