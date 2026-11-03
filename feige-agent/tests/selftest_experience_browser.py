"""Real browser and real isolated onboarding API; no external services."""
import base64
import argparse
import json
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.desktop import UI_DIR
from app.engine import browser
from app.engine.cdp import CDPClient
from app.service.experience import ExperienceAPI


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--browser',type=Path)
    args=parser.parse_args()
    checks=[]
    def check(label,passed):
        checks.append({'label':label,'passed':bool(passed)})
        print(('PASS ' if passed else 'FAIL ')+label,flush=True)
        if not passed:raise AssertionError(label)
    output=ROOT/'docs/verification';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='experience-browser-') as temp:
        root=Path(temp);api=ExperienceAPI(root)
        allowed={'status','prepare','chat','clear_chat','preview_correction','confirm_correction','rollback','import_products'}
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(UI_DIR),**kwargs)
            def log_message(self,*_):pass
            def translate_path(self,path):
                if path.split('?',1)[0]=='/assets/app-icon.svg':return str(UI_DIR.parent/'assets/app-icon.svg')
                return super().translate_path(path)
            def do_POST(self):
                method=self.path.removeprefix('/api/')
                if method not in allowed:self.send_error(404);return
                args=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                data=json.dumps(getattr(api,method)(*args),ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        executable=str(args.browser.resolve()) if args.browser else browser.find_chrome()
        if not executable:raise RuntimeError('需要浏览器执行界面验收')
        proc=subprocess.Popen([executable,'--headless=new','--disable-gpu','--no-first-run',
            '--remote-debugging-port=%d'%port,'--user-data-dir=%s'%(root/'browser'),
            'http://127.0.0.1:%d/experience.html'%server.server_port],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        cdp=None
        try:
            deadline=time.monotonic()+20
            while not browser.cdp_alive(port):
                if time.monotonic()>deadline:raise TimeoutError('浏览器未启动')
                time.sleep(.1)
            cdp=CDPClient(port);cdp.connect(prefer='/experience.html')
            while not cdp.evaluate("document.readyState==='complete' && typeof action==='function'"):
                if time.monotonic()>deadline:raise TimeoutError('页面未加载')
                time.sleep(.1)
            cdp.evaluate("""window.testErrors=[];window.addEventListener('error',e=>testErrors.push(e.message));
              window.until=async f=>{const end=Date.now()+10000;while(!f()){if(Date.now()>end)throw Error('界面等待超时');await new Promise(r=>setTimeout(r,30));}};
              window.pywebview={api:new Proxy({},{get:(_,method)=>(...args)=>fetch('/api/'+method,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(r=>r.json())})};
              window.dispatchEvent(new Event('pywebviewready'));until(()=>ready&&!pending&&document.querySelectorAll('#sampleStore option').length===3);""")
            check('无账号加载三家示例店，试聊准备前隐藏',cdp.evaluate("document.querySelectorAll('#sampleStore option').length===3&&el('chatSection').hidden"))
            cdp.evaluate("el('prepare').click();until(()=>!pending&&!el('chatSection').hidden)")
            check('一键整理商品属性和问答',cdp.evaluate("el('summary').textContent.includes('3 件商品')&&el('summary').textContent.includes('1 条问答')"))
            def ask(question):
                cdp.evaluate("el('question').value=%s;el('chatForm').requestSubmit();until(()=>!pending&&latest)"%json.dumps(question))
                return cdp.evaluate("el('messages').lastElementChild.textContent")
            check('实际界面查价格与库存', '199' in ask('黑杆多少钱，还有货吗'))
            check('依据展开可读',cdp.evaluate("!el('evidencePanel').hidden&&el('evidence').textContent.includes('products.')"))
            check('新问题补问，不编造刻字服务','不提供刻字' not in ask('能刻字吗'))
            cdp.evaluate("el('correctAnswer').value='本店暂不提供刻字服务';el('correctionForm').requestSubmit();until(()=>!pending&&!el('correctionPreview').hidden)")
            check('自动整理纠错并显示检查和确认',cdp.evaluate("el('correctionCheck').textContent.includes('通过')&&!el('confirmCorrection').hidden"))
            cdp.evaluate("el('confirmCorrection').click();until(()=>!pending&&el('correctionResult').textContent.includes('已用于'))")
            check('确认后可撤销更新',cdp.evaluate("!el('rollback').hidden"))
            cdp.evaluate("el('clearChat').click();until(()=>!pending&&el('messages').children.length===0)")
            ask('黑杆多少钱')
            learned=ask('能刻字吗')
            check('新会话使用已确认知识','不提供刻字' in learned)
            cdp.evaluate("el('rollback').click();until(()=>!pending&&el('correctionResult').textContent.includes('已撤销'))")
            cdp.evaluate("el('clearChat').click();until(()=>!pending&&el('messages').children.length===0)")
            check('回退后实际答复恢复缺资料','不提供刻字' not in ask('能刻字吗'))
            cdp.evaluate("""const files=new DataTransfer();files.items.add(new File(['商品名称,价格,库存,别名\\n体验黑杆,35,4,黑杆\\n'],'示例.csv',{type:'text/csv'}));el('productFile').files=files.files;el('importProducts').click();until(()=>!pending&&el('summary').textContent.includes('1 件商品'));""")
            check('中文CSV导入无需技术标识','35' in ask('体验黑杆多少钱'))
            cdp.evaluate("""const bad=new DataTransfer();bad.items.add(new File(['商品名称,价格,库存\\n错误,NaN,3'],'坏表.csv'));el('productFile').files=bad.files;el('importProducts').click();until(()=>!pending&&el('status').classList.contains('error'));""")
            check('错误说明具体行和修复方式，原资料保留',cdp.evaluate("el('status').textContent.includes('第2行')&&el('summary').textContent.includes('1 件商品')"))
            cdp.evaluate("el('sampleStore').value='demo_home';el('sampleStore').dispatchEvent(new Event('change'));until(()=>!pending&&currentStore==='demo_home')")
            check('切店清空上一店的纠错入口',cdp.evaluate("latest===null&&el('correctionPreview').hidden"))
            cdp.evaluate("el('prepare').click();until(()=>!pending&&!el('chatSection').hidden)")
            check('其他品类正常使用','99' in ask('小灯多少钱'))
            for width in (1240,700,320):
                cdp.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':1000,'deviceScaleFactor':1,'mobile':False})
                check('%d像素无横向溢出'%width,cdp.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
                check('%d像素关键操作有可见区域'%width,cdp.evaluate("['prepare','question','ask'].every(id=>{const r=el(id).getBoundingClientRect();return r.width>0&&r.left>=0&&r.right<=innerWidth+1})"))
                screenshot=cdp.call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
                (output/('experience-%d.png'%width)).write_bytes(base64.b64decode(screenshot['data']))
            cdp.call('Emulation.setDeviceMetricsOverride',{'width':1240,'height':1000,'deviceScaleFactor':1,'mobile':False})
            cdp.evaluate("document.body.style.zoom='2'")
            check('200%缩放无横向溢出',cdp.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            check('无页面运行错误',cdp.evaluate('testErrors.length===0'))
            report={'kind':'local_browser_synthetic','passed':all(x['passed'] for x in checks),'checks':checks,
                    'novice_participants':0,'actual_model_calls':False,'customer_messages_sent':False}
            (output/'experience-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print('PASS %d/%d'%(len(checks),len(checks)))
        finally:
            if cdp:cdp.close()
            proc.terminate()
            try:proc.wait(timeout=10)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=10)
            server.shutdown();server.server_close()
    return 0


if __name__=='__main__':raise SystemExit(main())
