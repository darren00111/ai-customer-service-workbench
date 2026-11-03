"""Read-only Qianniu compatibility probe. Does not modify/patch the client.

Names and structural metadata stay local; a readable tree does not prove that
buyer identity, roles and message IDs are reliably mapped for auto-reception.
"""
import os
import threading
import hashlib

from .pinduoduo import Account
from .qianniu_native import QianniuNative, NativeCompatibilityError


class QianniuPilot:
    """Native receive/draft/send path. Real-client acceptance remains required."""
    def __init__(self, spec, ledger, native=None):
        if spec.platform != 'taobao':
            raise ValueError('此连接仅用于淘宝／天猫')
        self.spec, self.ledger = spec, ledger
        self.native = native or QianniuNative()
        self.lock = threading.RLock()
        self.account = None
        self.window = None
        self.bound = False
        self.state = 'disconnected'
        self.baselines = set()
        self.eligible_heads = {}
        self.last_snapshot = None
        from .lease import ConnectionLease
        self.lease = ConnectionLease(ledger.workspace, spec)

    def status(self):
        from dataclasses import asdict
        return {'state': self.state, 'account': asdict(self.account) if self.account else None,
                'bound': self.bound, 'automatic_sending': False, 'production_verified': False,
                'native_window': self.window, 'coverage':getattr(self.native,'coverage','current_chat'),
                'verified_message_metadata':getattr(self.native,'verified_message_metadata',False)}

    def open_login(self):
        with self.lock:
            self.lease.acquire()
            try:
                return self.native.launch()
            except Exception:
                self.close()
                raise

    def inspect(self):
        with self.lock:
            self.close()
            self.lease.acquire()
            try:
                self.ledger.recover(self.spec)
                windows = self.native.windows()
                if len(windows) != 1:
                    raise NativeCompatibilityError('请打开并登录一个千牛接待中心；多个账号时先只保留要验证的账号窗口')
                target = windows[0]
                snap = self.native.snapshot(target['window'], target['seller'])
                self.window = target['window']
                seller_id = hashlib.sha256(target['seller'].encode()).hexdigest()[:24]
                self.account = Account('taobao', seller_id, target['seller'])
                self.last_snapshot = snap
                self.state = 'awaiting_confirmation'
                return {**self.status(), 'buyer': snap.buyer, 'readable_messages': len(snap.messages)}
            except Exception:
                self.close()
                raise

    def bind(self, account_key, reviewed=False):
        with self.lock:
            if reviewed is not True or not self.account or account_key != self.account.key:
                raise ValueError('请核对千牛登录账号、当前买家与聊天原文后确认绑定')
            self.native.snapshot(self.window, self.account.name)
            self.ledger.bind(self.spec, self.account.key)
            self.bound = True
            self.state = 'observing'
            return self.status()

    def _ingest(self, snap):
        first = snap.buyer not in self.baselines
        inserted = 0
        for message in snap.messages:
            inserted += self.ledger.ingest(self.spec, self.account.key, message, historical=first)
        self.baselines.add(snap.buyer)
        if snap.messages[-1].role == 'buyer':
            self.eligible_heads[snap.buyer] = snap.messages[-1].message_id
        else:
            self.eligible_heads.pop(snap.buyer, None)
        self.last_snapshot = snap
        return inserted

    def poll(self):
        with self.lock:
            if not self.bound or not self.account:
                raise ValueError('请先检查千牛连接并确认绑定')
            try:
                snapshots = (self.native.scan(self.window,self.account.name) if hasattr(self.native,'scan') else
                             [self.native.snapshot(self.window, self.account.name)])
                inserted = sum(self._ingest(snapshot) for snapshot in snapshots)
                return {**self.status(), 'inserted': inserted, 'buyer': snapshots[-1].buyer,
                        'hint': '已读取千牛会话；接待列表切换和实际收发仍需真实客户端验收'}
            except Exception:
                self.close()
                raise

    def send_reviewed(self, conversation_id, head, text, validate=None, automatic=False):
        with self.lock:
            self.poll()
            if self.last_snapshot.buyer != conversation_id:
                self.native.select_buyer(self.window, self.account.name, conversation_id)
                self._ingest(self.native.snapshot(self.window,self.account.name))
            if validate:
                validate()
            expected = self.last_snapshot
            latest = self.ledger.head(self.spec, conversation_id)
            if expected.buyer != conversation_id or latest.get('sequence') != head or self.eligible_heads.get(conversation_id) != latest.get('message_id'):
                raise ValueError('千牛顾客或消息已变化，旧回复已取消')
            self.native.preflight(expected)
            token = self.ledger.reserve(self.spec, self.account.key, conversation_id, head, text, automatic)
            try:
                after, message_id = self.native.submit(expected, text)
                self._ingest(after)
                self.ledger.finish(token, 'awaiting_echo', message_id)
            except Exception:
                self.ledger.finish(token, 'delivery_unknown')
            return self.ledger.delivery(token)

    def close(self):
        with self.lock:
            try:
                if self.bound:
                    self.ledger.invalidate_connection(self.spec)
                self.bound = False
                self.account = None
                self.window = None
                self.last_snapshot = None
                self.baselines.clear()
                self.eligible_heads.clear()
                self.state = 'disconnected'
            finally:
                self.lease.release()


def inspect_qianniu():
    if os.name != 'nt':
        return {'state': 'unsupported_os', 'detail': '千牛客户端检查需要 Windows', 'automatic_sending': False}
    result = []

    def probe():
        try:
            import clr
            clr.AddReference('UIAutomationClient')
            clr.AddReference('UIAutomationTypes')
            from System.Windows.Automation import AutomationElement, Condition, TreeScope, TreeWalker
            from System.Diagnostics import Process
            processes = {p.Id for p in Process.GetProcessesByName('AliWorkbench')}
            if not processes:
                result.append({'state': 'client_not_running', 'windows': [], 'detail': '请自行打开并登录千牛，停留在客服会话页'})
                return
            windows = AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition)
            candidates = []
            for win in windows:
                if win.Current.ProcessId not in processes:
                    continue
                # Do not read login or password dialogs.
                title = str(win.Current.Name)
                if any(word in title for word in ('登录', '验证', '密码')):
                    continue
                rows, stack = [], [(win, 0)]
                walker = TreeWalker.ControlViewWalker
                while stack and len(rows) < 800:
                    element, depth = stack.pop()
                    cur = element.Current
                    if cur.IsPassword:
                        continue
                    rows.append({'name': str(cur.Name)[:300], 'automation_id': str(cur.AutomationId)[:100],
                                 'class_name': str(cur.ClassName)[:100], 'control_type': str(cur.ControlType.ProgrammaticName), 'depth': depth})
                    if depth < 12:
                        children, child = [], walker.GetFirstChild(element)
                        while child is not None and len(children) < 200:
                            children.append((child, depth + 1))
                            child = walker.GetNextSibling(child)
                        stack.extend(reversed(children))
                candidates.append({'title': title[:100], 'class_name': str(win.Current.ClassName), 'controls': rows})
            result.append({'state': 'mapping_required' if candidates else 'login_or_chat_required', 'windows': candidates,
                           'detail': '只读结构已检查；仍需核对店铺、顾客、消息顺序和角色，尚不能收发客服消息'})
        except Exception:
            result.append({'state': 'accessibility_unavailable', 'windows': [],
                           'detail': '当前版本未暴露可用的无障碍结构，需要登录后检查客户端兼容方式'})

    thread = threading.Thread(target=probe, name='qianniu-readonly-probe', daemon=True)
    thread.start()
    thread.join(8)
    return {**(result[0] if result else {'state': 'probe_timeout', 'windows': [], 'detail': '客户端检查超时，请确认千牛没有卡顿'}),
            'automatic_sending': False, 'production_verified': False}
