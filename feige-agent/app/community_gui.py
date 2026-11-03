"""Exercise the packaged native bridge using only a temporary synthetic shop."""
import tempfile
import time
from pathlib import Path


def run_native_check():
    import webview
    from .config import StoreRegistry
    from .desktop import DesktopAPI, UI_DIR
    from .multistore import MultiStoreSupervisor
    from .notifications import HandoffOutbox
    from .service.workbench import Workbench
    from .service.workbench_api import WorkbenchAPI
    with tempfile.TemporaryDirectory(prefix='community-native-') as temporary:
        root=Path(temporary)
        registry=StoreRegistry(root/'registry.json',root/'stores')
        supervisor=MultiStoreSupervisor(registry=registry)
        supervisor.notifications=HandoffOutbox(root/'notifications.jsonl')
        workbench=Workbench(root/'reception',root/'stores',registry)
        bridge=WorkbenchAPI(workbench)
        api=DesktopAPI(supervisor=supervisor,workbench_api=bridge)
        window=webview.create_window('社区版界面验收', (UI_DIR/'index.html').resolve().as_uri(),js_api=api,
                                    width=1440,height=900,hidden=True)
        api._main_window=window
        api._embedder.bind_window(window)
        report={'ok':False,'checks':[]}
        def check():
            try:
                deadline=time.monotonic()+25
                while time.monotonic()<deadline:
                    try:
                        if window.evaluate_js("typeof apiReady!=='undefined' && apiReady && state.edition==='community'"):
                            break
                    except Exception:
                        pass
                    time.sleep(.1)
                else:
                    raise AssertionError('原生界面桥接未就绪')
                report['checks'].append('打包原生界面和 Python 桥接可用')
                assert window.evaluate_js("document.getElementById('newStorePlatform').options.length===1")
                report['checks'].append('原生界面只显示抖店')
                window.evaluate_js("document.getElementById('addStoreButton').click();document.getElementById('newStoreName').value='原生合成店铺';document.getElementById('addStoreForm').requestSubmit()")
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    if window.evaluate_js('state.configured===1 && !busy'):
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('原生添加店铺未完成')
                report['checks'].append('原生添加表单实际创建一家抖店')
                assert window.evaluate_js("document.getElementById('addStoreButton').disabled")
                report['checks'].append('原生界面锁定第二店入口')
                assert not api.add_store('feige','第二家')['ok']
                report['checks'].append('原生桥接拒绝第二店')
                assert not supervisor.agents
                report['checks'].append('创建不触发真实登录或发送')
                report.update(ok=True,count=len(report['checks']))
            except Exception as exc:
                report['error']=type(exc).__name__+': '+str(exc)
            finally:
                window.destroy()
        try:
            webview.start(func=check,gui='edgechromium')
        finally:
            supervisor.shutdown();bridge.shutdown()
        return report
