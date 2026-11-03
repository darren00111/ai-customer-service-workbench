# -*- coding: utf-8 -*-
"""Native desktop shell and the small API exposed to the webview UI."""
from __future__ import annotations

import threading
import time
import uuid
import re
import sqlite3
from pathlib import Path

from . import __version__
from .branding import APP_ICON, configure_desktop_identity
from .embedded_browser import EmbeddedBrowserController
from .multistore import MultiStoreSupervisor

UI_DIR = Path(__file__).resolve().parent / "ui"


class DesktopAPI:
    def __init__(self, supervisor: MultiStoreSupervisor | None = None,
                 embedder: EmbeddedBrowserController | None = None, workbench_api=None):
        # pywebview exposes public js_api attributes recursively.  Native
        # WebView/COM objects must never be traversed on its bridge thread.
        self._supervisor = supervisor or MultiStoreSupervisor(browser_app_mode=True)
        self._embedder = embedder or EmbeddedBrowserController()
        self._operation_lock = threading.Lock()
        self._workbench_window = None
        self._workbench_api = workbench_api
        self._workbench_lock = threading.RLock()
        self._login_attempts = {}
        self._knowledge_cache = (0, {})
        self._main_window = None
        self._experience_window = None

    def open_experience(self) -> dict:
        try:
            with self._workbench_lock:
                if self._experience_window:
                    self._experience_window.show()
                else:
                    from .config import DATA_DIR
                    from .service.experience import open_experience
                    self._experience_window = open_experience(DATA_DIR / 'sandbox')
                    self._experience_window.events.closed += self._experience_closed
            return {'ok': True}
        except Exception:
            return {'ok': False, 'error': '无法打开体验窗口，请重新启动程序后重试。'}

    def _experience_closed(self):
        self._experience_window = None

    def choose_qianniu_client(self):
        from .channels.qianniu_launcher import choose_client
        try:
            return choose_client(self._main_window)
        except (ValueError, OSError) as exc:
            return {'ok': False, 'error': str(exc)}

    def _ensure_workbench(self):
        with self._workbench_lock:
            if self._workbench_api is None:
                from .service.workbench_api import WorkbenchAPI
                self._workbench_api = WorkbenchAPI(on_created=getattr(self._supervisor,'reload_registry',None),persistent=True)
            return self._workbench_api

    def open_workbench(self, store_id='', section='') -> dict:
        try:
            from .service.workbench_api import WorkbenchAPI, open_workbench
            self._ensure_workbench()
            self._workbench_api._initial_store = store_id
            self._workbench_api._initial_section = section if section in ('connection','knowledge','cards','overrides') else ''
            if self._workbench_window:
                self._workbench_window.show()
                if store_id:
                    import json
                    self._workbench_window.evaluate_js('if(!pending)loadStores('+json.dumps(store_id)+').then(()=>navigateToSection('+json.dumps(self._workbench_api._initial_section)+'))')
            else:
                self._workbench_window = open_workbench(self._workbench_api)
                self._workbench_window.events.closed += self._workbench_closed
            return {"ok": True}
        except Exception:
            return {"ok": False, "error": "无法打开接入与试聊窗口，请重新启动程序后重试。"}

    def _workbench_closed(self):
        self._workbench_window = None

    def add_store(self, platform, name, merchant_id='local', category='通用零售') -> dict:
        if platform not in ('feige','pinduoduo','taobao'):
            return {'ok':False,'error':'请选择淘宝／天猫、抖店或拼多多'}
        if not isinstance(name,str) or not 1<=len(name.strip())<=100:
            return {'ok':False,'error':'请填写1–100字店铺名称'}
        if not isinstance(merchant_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',merchant_id):
            return {'ok':False,'error':'商家标识需为1–64位字母、数字、下划线或短横线'}
        if not isinstance(category,str) or not 1<=len(category.strip())<=100:
            return {'ok':False,'error':'请填写1–100字经营品类'}
        if not self._operation_lock.acquire(blocking=False):
            return {'ok':False,'error':'已有店铺操作正在执行，请稍候'}
        try:
            store_id=platform+'_'+uuid.uuid4().hex[:12]
            self._ensure_workbench()._workbench.create_store(store_id,name,category,platform,merchant_id)
            self._supervisor.reload_registry()
            self._knowledge_cache=(0,{})
            return {'ok':True,'store_id':store_id,'platform':platform,
                    'hint':'店铺已添加，请单独打开登录入口；尚未启用自动接待'}
        except (ValueError,OSError) as exc:
            return {'ok':False,'error':str(exc)}
        finally:self._operation_lock.release()

    def login_store(self, store_id) -> dict:
        if not self._operation_lock.acquire(blocking=False):
            return {'ok':False,'error':'已有店铺操作正在执行，请稍候'}
        try:
            spec=next((s for s in self._supervisor.specs if s.store_id==store_id),None)
            if not spec:raise ValueError('未找到该店铺')
            if spec.platform in ('pinduoduo','taobao'):
                w=self._ensure_workbench()._workbench
                monitor=w._channel_workers.get(store_id)
                if monitor and (monitor.status().get('running') or monitor.status().get('stopping')):
                    raise ValueError('该店正在接待或停止中，请先停止接待，再重新登录')
                w.pilot_command(store_id,'open_login')
            elif spec.platform=='feige':
                status=next((s for s in self._supervisor.status()['shops'] if s['store_id']==store_id),{})
                if status.get('watching') or status.get('desired'):
                    raise ValueError('该店正在接待，请先停止接待，再重新登录')
                from .engine.browser import ensure_browser
                cfg=spec.config_overrides()
                ensure_browser(spec.cdp_port,cfg['profile_dir'],spec.workspace_url,chrome=spec.chrome_path or None)
            else:raise ValueError('该平台尚未提供登录入口')
            self._login_attempts[store_id]={'state':'verification_required','label':'已打开，待核对',
                'detail':'请自行登录并核对店铺身份；登录窗口打开不代表消息接待已就绪'}
            return {'ok':True,'store_id':store_id,'hint':('请在千牛客户端自行登录；多账号兼容性及收发仍待验证' if spec.platform=='taobao' else '请在本店独立窗口自行登录，再核对身份和接待资料')}
        except (ValueError,OSError,RuntimeError) as exc:
            from .channels.qianniu_launcher import QianniuClientNotFound
            return {'ok':False,'error':str(exc),'needs_client_path':isinstance(exc,QianniuClientNotFound)}
        finally:self._operation_lock.release()

    def _workspace_overview(self,state):
        control=self._workbench_api
        if control is None and getattr(self._supervisor,'registry',None) is not None:
            control=self._ensure_workbench()
        stamp,cached=self._knowledge_cache
        if control is not None and time.monotonic()-stamp>10:
            cached={}
            for shop in state['shops']:
                try:cached[shop['store_id']]=control._workbench.cards(shop['store_id'],False)['summary']
                except (ValueError,OSError,sqlite3.Error):cached[shop['store_id']]={'error':'资料待检查，请打开本店知识库核对'}
            self._knowledge_cache=(time.monotonic(),cached)
        for shop in state['shops']:
            channel=shop.get('channel_runtime',{})
            connection=channel.get('connection',{})
            login=dict(self._login_attempts.get(shop['store_id'],{'state':'unknown','label':'待登录／核对'}))
            if connection.get('bound'):
                login={'state':'identity_confirmed','label':'本次身份已核对','detail':'店铺身份已绑定；平台实际收发仍需单独验收'}
            elif connection.get('account'):
                login={'state':'identity_pending','label':'身份待确认'}
            elif shop.get('state')=='not_logged_in':
                login={'state':'login_required','label':'需要登录'}
            elif shop.get('platform')=='feige' and shop.get('state')=='online':
                login={'state':'page_available','label':'客服页可用，身份需核对'}
            shop['login']=login
            shop['acceptance']={'state':'pending','label':'真实店铺未验收'}
            shop['knowledge_queue']=cached.get(shop['store_id'],{'error':'资料待检查'})
        state['knowledge_pending']=sum(s['knowledge_queue'].get('open',0) for s in state['shops'])
        state['knowledge_incomplete']=any(s['knowledge_queue'].get('error') for s in state['shops'])

    def get_state(self) -> dict:
        state = self._supervisor.status()
        state["version"] = __version__
        state["embedded_browser"] = self._embedder.status()
        channel_specs=[s for s in getattr(self._supervisor,'specs',()) if getattr(s,'platform','feige') in ('pinduoduo','taobao')]
        if channel_specs:
            self._ensure_workbench()
        if self._workbench_api is not None:
            channels = self._workbench_api._workbench.channel_runtime()
            for shop in state['shops']:
                if shop['store_id'] in channels:
                    channel = channels[shop['store_id']]
                    shop['channel_runtime'] = channel
                    shop['watching'] = channel['monitor'].get('running',False)
                    shop['total_replies'] = channel.get('automatic_deliveries',0)
                    shop['can_start_store_reception'] = bool(channel['store_automation']['enabled'])
                    monitor=channel['monitor']
                    shop['counters']={'rounds':monitor.get('rounds',0),'sent':monitor.get('confirmed',0),'handoffs':monitor.get('handoffs',0)}
                    if shop['watching']:
                        shop['state'] = 'store_monitoring' if monitor.get('mode')=='store' else 'acceptance_monitoring'
                    elif channel['monitor'].get('stopping'):
                        shop['state'] = 'stopping'
                    elif monitor.get('error'):
                        shop['state'] = 'error'
                    elif channel['connection'].get('bound'):
                        shop['state'] = 'connected_idle'
                    elif shop['can_start_store_reception']:
                        shop['state'] = 'connection_required'
                    shop['page'] = {'hint':monitor.get('error') or channel.get('handoff_error') or ('本店全店自动接待已开启，售后生成本机人工待办' if monitor.get('mode')=='store' and shop['watching'] else '可在接入与试聊中连接店铺、检查会话或开始接待')}
        if channel_specs:
            online_states=('online','store_monitoring','acceptance_monitoring')
            state['online']=sum(s.get('state') in online_states for s in state['shops'])
            state['needs_attention']=sum(s.get('state') not in (*online_states,'stopped','connected_idle') for s in state['shops'])
        state['notifications']=self._supervisor.notifications.list_pending(limit=100)
        state['pending_notifications']=len(state['notifications'])
        self._workspace_overview(state)
        return state

    def _start_channel_store(self,store_id):
        control=self._ensure_workbench()
        w=control._workbench
        try:
            state=w.start_channel_monitor(store_id,'store',[],True)
            sync=getattr(control,'_sync_worker',None)
            if sync is not None:sync.start()
            return {'ok':True,'store_id':store_id,'state':'store_monitoring','monitor':state}
        except ValueError as exc:
            return {'ok':False,'store_id':store_id,'state':'connection_required','error':str(exc),'detail':str(exc)}

    def start_all(self) -> dict:
        if not self._operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有启动任务正在执行，请稍候。"}
        try:
            result = self._supervisor.boot_all(max_parallel=3)
            for spec in self._supervisor.specs:
                if getattr(spec,'platform','feige') not in ('pinduoduo','taobao') or not getattr(spec,'enabled',True):continue
                w=self._ensure_workbench()._workbench
                if not w.automation.status(spec)['enabled']:
                    result.setdefault('results',[]).append({'ok':False,'store_id':spec.store_id,'state':'connection_required','attempted':False,
                        'detail':'请先完成本店测试并单独开启全店接待'})
                    continue
                channel=self._start_channel_store(spec.store_id)
                result['attempted']+=1
                result['online']+=int(channel['ok'])
                result.setdefault('results',[]).append(channel)
            return {"ok": True, **result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "无法启动店铺：%s" % exc}
        finally:
            self._operation_lock.release()

    def start_store(self, store_id: str) -> dict:
        if not self._operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "已有启动任务正在执行，请稍候。"}
        try:
            spec=next((s for s in self._supervisor.specs if s.store_id==store_id),None)
            if spec and getattr(spec,'platform','feige') in ('pinduoduo','taobao'):
                return self._start_channel_store(store_id)
            result = self._supervisor.boot_store(store_id)
            self._supervisor.start_monitor()
            if result.get("ok"):
                spec = next((row for row in self._supervisor.specs if row.store_id == store_id), None)
                if spec:
                    result["embed"] = self._embedder.attach(spec.cdp_port)
            return {"ok": bool(result.get("ok")), **result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "无法启动该店铺：%s" % exc}
        finally:
            self._operation_lock.release()

    def stop_store(self, store_id: str) -> dict:
        try:
            spec = next((row for row in self._supervisor.specs if row.store_id == store_id),None)
            if spec and getattr(spec,'platform','feige') in ('taobao','pinduoduo') and self._workbench_api:
                state=self._workbench_api._workbench.stop_store_reception(store_id)
                return {'ok':True,'stopping':state.get('stopping',False)}
            stopped = self._supervisor.stop_store(store_id)
            return {"ok": stopped, "error": "" if stopped else "该店铺尚未启动。"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "无法停止该店铺：%s" % exc}

    def mark_notification_done(self, event_id: str) -> dict:
        event=self._supervisor.notifications.get_pending(event_id)
        resumed=False
        if event and event.get('external_manual'):
            try:
                resumed=self._ensure_workbench()._workbench.complete_channel_handoff(event)
            except ValueError as exc:
                return {'ok':False,'error':str(exc)}
        done = self._supervisor.resolve_handoff(event_id)
        return {"ok": done, "conversation_resumed":resumed,
                'hint':('已记录人工处理；会话仍有其他暂停原因，请在接入与会话中核对' if event and event.get('external_manual') and not resumed else ''),
                "error": "" if done else "这条消息已经处理或不存在。"}

    def send_handoff_reply(self, event_id: str, text: str) -> dict:
        try:
            return self._supervisor.send_handoff_reply(event_id, text)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "无法发送草稿：%s" % exc}

    def embed_store(self, store_id: str) -> dict:
        spec = next((row for row in self._supervisor.specs if row.store_id == store_id), None)
        if not spec:
            return {"ok": False, "error": "没有找到该店铺。"}
        if getattr(spec, "platform", "feige") != "feige":
            return {"ok": False, "error": "该平台的工作台连接尚未接入，可先在接入与试聊中准备资料。"}
        agent = getattr(self._supervisor, "agents", {}).get(store_id)
        if agent:
            agent.connect()
        return self._embedder.attach(spec.cdp_port, wait=8.0)

    def release_embedded_page(self) -> dict:
        # Viewing another platform restores the browser as a separate window;
        # its reception worker and login session keep running.
        self._embedder.detach()
        return {'ok':True}

    def set_embed_bounds(self, left: float, top: float, width: float, height: float) -> dict:
        return self._embedder.set_bounds(left, top, width, height)

    def shutdown(self) -> None:
        if self._experience_window:
            window, self._experience_window = self._experience_window, None
            window.destroy()
        if self._workbench_api:
            self._workbench_api.shutdown()
        if self._workbench_window:
            window, self._workbench_window = self._workbench_window, None
            window.destroy()
        self._embedder.detach()
        self._supervisor.shutdown()


def run_desktop(debug: bool = False, initial_store_id: str | None = None) -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("桌面组件未安装，请运行：pip install -r requirements.txt") from exc

    configure_desktop_identity()
    api = DesktopAPI()
    window = webview.create_window(
        "抖店智能客服",
        (UI_DIR / "index.html").resolve().as_uri(),
        js_api=api,
        width=1440,
        height=900,
        min_size=(1100, 700),
        background_color="#f7f7f5",
        text_select=True,
    )
    api._embedder.bind_window(window)
    api._main_window = window
    window.events.moved += api._embedder.reposition
    window.events.resized += api._embedder.reposition
    window.events.restored += api._embedder.restore
    window.events.closed += api.shutdown
    startup = (lambda: api.start_store(initial_store_id)) if initial_store_id else None
    webview.start(func=startup, debug=debug, icon=str(APP_ICON))
