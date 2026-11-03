"""Run the actual desktop JavaScript against the actual local desktop bridge."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import StoreRegistry
from app.desktop import DesktopAPI, UI_DIR
from app.multistore import MultiStoreSupervisor
from app.service.workbench import Workbench
from app.service.workbench_api import WorkbenchAPI
from app.engine import browser
from app.engine.cdp import CDPClient


def main():
    checks=[]
    def check(label,value):
        if not value:raise AssertionError(label)
        checks.append(label);print('PASS '+label,flush=True)
    with tempfile.TemporaryDirectory(prefix='community-original-ui-') as temporary:
        root=Path(temporary);registry=StoreRegistry(root/'registry.json',root/'stores')
        workbench=Workbench(root/'reception',root/'stores',registry)
        supervisor=MultiStoreSupervisor(registry=registry)
        bridge=WorkbenchAPI(workbench)
        desktop=DesktopAPI(supervisor,SimpleNamespace(status=lambda:{'embedded':False},restore=lambda:{'ok':False},
                          set_bounds=lambda *args:{'ok':True}),bridge)
        current_api=desktop
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(UI_DIR),**kwargs)
            def log_message(self,*args):pass
            def translate_path(self,path):
                if path.startswith('/assets/') and Path(path).name in ('app-icon.svg','app-icon.png','app-icon.ico'):
                    return str(UI_DIR.parent/'assets'/Path(path).name)
                return super().translate_path(path)
            def do_POST(self):
                method=self.path.removeprefix('/api/')
                if method.startswith('_') or not callable(getattr(current_api,method,None)):
                    self.send_error(404);return
                args=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                raw=json.dumps(getattr(current_api,method)(*args),ensure_ascii=False).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        url=f'http://127.0.0.1:{server.server_port}/index.html'
        process=subprocess.Popen([browser.find_chrome(),'--headless=new','--no-first-run','--disable-gpu',
            '--remote-debugging-port='+str(port),'--user-data-dir='+str(root/'edge'),'--window-size=1440,1000',url],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        client=None
        try:
            deadline=time.monotonic()+15
            while time.monotonic()<deadline and not browser.cdp_alive(port):time.sleep(.1)
            client=CDPClient(port);client.connect(prefer='/index.html')
            def wait(expression):
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    try:
                        if client.evaluate(expression):return
                    except Exception:pass
                    time.sleep(.08)
                raise AssertionError('UI condition timed out: '+expression)
            def inject():
                client.evaluate("""window.pywebview={api:new Proxy({}, {get:(_,method)=>(...args)=>fetch('/api/'+method,
                  {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(args)}).then(r=>r.json())})};
                  window.dispatchEvent(new Event('pywebviewready'));""")
            wait("typeof refreshState==='function'");inject();wait("apiReady && state.edition==='community'")
            check('原主界面标明社区版',client.evaluate("document.title.includes('社区版')"))
            check('原主界面保留飞鸽原页面入口',client.evaluate("!!document.getElementById('browserHost')"))
            check('空店不显示十店模拟数据',client.evaluate("state.configured===0 && document.querySelectorAll('.shop-item').length===0"))
            check('添加平台只有抖店',client.evaluate("document.getElementById('newStorePlatform').options.length===1 && document.getElementById('newStorePlatform').value==='feige'"))
            client.evaluate("document.getElementById('addStoreButton').click();document.getElementById('newStoreName').value='社区合成店';document.getElementById('addStoreForm').requestSubmit()")
            wait("state.configured===1 && !busy")
            check('原主界面实际添加一家抖店',len(registry.load())==1)
            check('添加第二家按钮锁定',client.evaluate("document.getElementById('addStoreButton').disabled && document.getElementById('addStoreButton').textContent.includes('1 / 1')"))
            check('登录与知识库入口保留',client.evaluate("!document.getElementById('loginStoreButton').disabled && !document.getElementById('storeKnowledgeButton').disabled"))
            client.evaluate("document.getElementById('autoReplyButton').click()")
            check('可打开原引擎回复模式设置',client.evaluate("document.getElementById('autoReplyDialog').open && document.getElementById('autoReplyMode').value==='suggest'"))
            client.evaluate("document.getElementById('autoReplyForm').requestSubmit()")
            wait("!document.getElementById('autoReplyDialog').open")
            check('建议模式设置保存成功',registry.load()[0].runtime_overrides['auto_send'] is False)
            screen_dir=ROOT.parent/'docs/screenshots';screen_dir.mkdir(parents=True,exist_ok=True)
            screenshot=client.call('Page.captureScreenshot',{'format':'png'})
            (screen_dir/'community-desktop.png').write_bytes(base64.b64decode(screenshot['data']))
            current_api=bridge
            client.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/workbench.html'})
            wait("typeof loadStores==='function'");inject();wait("currentStore!=='' && !pending")
            check('知识工作台只列出真实单店',client.evaluate("document.getElementById('storeSelect').options.length===1"))
            check('工作台不再允许新增第二店',client.evaluate("document.getElementById('storeOnboarding').hidden"))
            check('工作台平台也只有抖店',client.evaluate("document.getElementById('newPlatform').options.length===1"))
            check('工作台不展示其他平台联调',client.evaluate("getComputedStyle(document.getElementById('pilotPanel')).display==='none'"))
            check('原试聊表单可以使用',client.evaluate("!document.getElementById('chatSend').disabled"))
            client.evaluate("document.getElementById('question').value='这款多少钱';document.getElementById('chatForm').requestSubmit()")
            wait("!pending && document.getElementById('messages').textContent.includes('这款多少钱')")
            check('原试聊管道真实返回保守答复',len(workbench.knowledge(registry.load()[0].store_id).transcript(bridge._initial_store or client.evaluate('session')))>0)
            print(json.dumps({'ok':True,'count':len(checks),'checks':checks},ensure_ascii=False),flush=True)
        finally:
            if client:
                try:client.call('Browser.close',timeout=3)
                except Exception:pass
                client.close()
            process.terminate()
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            server.shutdown();server.server_close();thread.join()
            supervisor.shutdown();bridge.shutdown()


if __name__=='__main__':main()
