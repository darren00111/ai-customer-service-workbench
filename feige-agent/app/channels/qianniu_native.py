"""Qianniu's accessible Qt reception client, without patching its installation.

Control identities were cross-checked against the MIT dify-on-qianniu locator
store (8e413a5). Actual client compatibility is established at connection time.
No coordinate clicks, clipboard manipulation or credentials are involved.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import os
import re
import threading
import time

from .pinduoduo import Incoming

ROOT_ID = 'UIWindow.centralwidget.SubChatView.'
CONTENT = ROOT_ID + 'ChatDisplayWidget.ChatContentView.'
BUYER_ID = CONTENT + 'titleWidget.titlename'
EDITOR_ID = CONTENT + 'splitter.sendMsgWidget.chatInputArea.plainTextEdit'
SEND_ID = CONTENT + 'splitter.sendMsgWidget.enterAreaKeyWidget.sendMsg'
HISTORY_ID = CONTENT + 'splitter.msgDisplayWidget.MessageListContainerView.stackedWidget.ScrollingMessageListView.messagelistWidget.qt_scrollarea_viewport'
LIST_ID = ROOT_ID + 'ChatListWidget.ChatListView.centralwidget.list_widget.ReceptionListView.stackedWidget.normalList.NormalReceptionListView.receptionListWidget'
HEADER = re.compile(r'^(?P<who>.+?)\s+(?:(?P<day>\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+)?(?P<clock>\d{1,2}:\d{2}:\d{2})\s*$')
_UI_LOCK = threading.RLock()


class NativeCompatibilityError(ValueError):
    pass


def parse_transcript(text, seller, buyer, clock=None):
    """Accept explicit author/time records only; ambiguous layouts are rejected."""
    if not isinstance(text, str) or not 1 <= len(text) <= 64000 or not seller or not buyer or seller == buyer:
        raise NativeCompatibilityError('千牛会话正文或双方身份不完整')
    current = clock or datetime.now().astimezone()
    records, item = [], None
    for line in text.replace('\r\n', '\n').split('\n'):
        if line.strip() in ('已读', '未读', '对方正在输入...', '消息发送成功'):
            continue
        if re.fullmatch(r'(?:对方|你|您|客服).*撤回.*消息', line.strip()):
            raise NativeCompatibilityError('千牛出现消息撤回，请重新核对会话')
        header = HEADER.fullmatch(line.strip())
        if header and header['who'] in (seller, buyer):
            if item is not None:
                records.append(item)
            day = header['day'] or current.date().isoformat()
            try:
                moment = datetime.strptime(day.replace('/', '-') + ' ' + header['clock'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=current.tzinfo)
            except ValueError as exc:
                raise NativeCompatibilityError('千牛消息时间格式未识别') from exc
            if not header['day'] and moment > current + timedelta(minutes=5):
                moment -= timedelta(days=1)
            item = {'role': 'agent' if header['who'] == seller else 'buyer',
                    'timestamp': int(moment.timestamp() * 1000), 'lines': []}
        elif item is not None:
            item['lines'].append(line)
        elif line.strip() and line.strip() not in ('查看历史消息', '加载更多', '消息记录'):
            raise NativeCompatibilityError('千牛聊天记录没有明确的作者和时间，不能猜测消息角色')
    if item is not None:
        records.append(item)
    if not records or len(records) > 300:
        raise NativeCompatibilityError('千牛未提供可识别的聊天记录')
    output, counts = [], Counter()
    for row in records:
        content = '\n'.join(row['lines']).strip()
        if not content or len(content) > 8000:
            raise NativeCompatibilityError('千牛消息正文缺失或过长')
        # This flat export is suitable for a reviewed preview only. Text alone
        # cannot distinguish an actual author header from a header quoted by a buyer.
        digest = hashlib.sha256(f"{row['role']}\0{row['timestamp']}\0{content}".encode()).hexdigest()
        counts[digest] += 1
        if counts[digest] > 1:
            raise NativeCompatibilityError('同一秒出现相同作者和内容，需原生消息标识消除歧义')
        output.append(Incoming(buyer, 'qn-' + digest, row['role'], content, row['timestamp'],
                               'reply' if row['role'] == 'buyer' else 'context'))
    return output


@dataclass(frozen=True)
class NativeSnapshot:
    window: int
    seller: str
    buyer: str
    text: str
    editor: str
    messages: tuple

    @property
    def fingerprint(self):
        return hashlib.sha256(f'{self.window}\0{self.seller}\0{self.buyer}\0{self.text}'.encode()).hexdigest()


class QianniuNative:
    # The real client must expose separately verified message rows before sends
    # are enabled. A TextPattern transcript cannot establish author provenance.
    verified_message_metadata = False
    def __init__(self):
        self._api = None
        self._buyer_targets = {}
        self._scan_offset = 0
        self.coverage = 'current_chat'

    def _load(self):
        if os.name != 'nt':
            raise NativeCompatibilityError('千牛客户端接入需要 Windows')
        if self._api is None:
            import clr
            clr.AddReference('UIAutomationClient'); clr.AddReference('UIAutomationTypes')
            from System.Windows import Automation as uia
            from System.Diagnostics import Process
            from System import IntPtr
            self._api = (uia, Process, IntPtr)
        return self._api

    def windows(self):
        uia, Process, _ = self._load()
        pids = {p.Id for p in Process.GetProcessesByName('AliWorkbench')}
        output = []
        for element in uia.AutomationElement.RootElement.FindAll(uia.TreeScope.Children, uia.Condition.TrueCondition):
            c = element.Current
            if c.ProcessId not in pids or c.ClassName not in ('ChatView', 'MutilChatView') or c.IsPassword:
                continue
            title = str(c.Name)
            match = re.fullmatch(r'(.+?)[-—]\s*接待中心', title)
            if match and not any(w in title for w in ('登录', '验证', '密码')):
                output.append({'window': int(c.NativeWindowHandle), 'seller': match[1].strip(),
                               'title': title, 'class_name': str(c.ClassName)})
        return output

    def launch(self):
        from .qianniu_launcher import launch_client
        return launch_client()

    def select_buyer(self, window, seller, buyer):
        with _UI_LOCK:
            uia, _, _ = self._load()
            root = self._window(window, seller)
            if self._value(self._control(root, EDITOR_ID)):
                raise NativeCompatibilityError('当前输入区有人工作业，已让出会话控制')
            rows = self._control(root, LIST_ID).FindAll(uia.TreeScope.Children, uia.Condition.TrueCondition)
            known = self._buyer_targets.get((window,seller,buyer))
            matches = [row for row in rows if (tuple(row.GetRuntimeId()) == known if known else str(row.Current.Name).strip() == buyer)]
            if len(matches) != 1:
                raise NativeCompatibilityError('无法唯一定位当前买家，请在千牛打开该会话后重试')
            matches[0].GetCurrentPattern(uia.SelectionItemPattern.Pattern).Select()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if self._value(self._control(root, BUYER_ID)).strip() == buyer:
                    return
                time.sleep(.1)
            raise NativeCompatibilityError('千牛会话切换尚未确认')

    def scan(self, window, seller, limit=8):
        """Walk the actual reception list; send targets are learned from selected rows."""
        with _UI_LOCK:
            uia, _, _ = self._load()
            current = self.snapshot(window,seller)
            snapshots = [current]
            if current.editor:
                return snapshots
            root = self._window(window,seller)
            try:
                rows = list(self._control(root,LIST_ID).FindAll(uia.TreeScope.Children,uia.Condition.TrueCondition))
            except NativeCompatibilityError:
                self.coverage = 'current_chat'
                return snapshots
            self.coverage = 'reception_list'
            if not rows:
                return snapshots
            start = self._scan_offset % len(rows)
            choices = (rows[start:] + rows[:start])[:limit]
            self._scan_offset = (start + len(choices)) % len(rows)
            seen = {current.buyer}
            for row in choices:
                latest = self.snapshot(window,seller)
                if latest.editor or latest.fingerprint != current.fingerprint:
                    snapshots.append(latest)
                    break  # Human input or an incoming update has priority over switching.
                try:
                    pattern = row.GetCurrentPattern(uia.SelectionItemPattern.Pattern)
                    identity = tuple(row.GetRuntimeId())
                    if pattern.Current.IsSelected:
                        self._buyer_targets[(window,seller,current.buyer)] = identity
                        continue
                    pattern.Select()
                    deadline = time.monotonic() + .8
                    switched = None
                    while time.monotonic() < deadline:
                        candidate = self.snapshot(window,seller)
                        if pattern.Current.IsSelected and candidate.buyer != current.buyer:
                            switched = candidate
                            break
                        time.sleep(.08)
                    if switched is None:
                        break
                    if switched.buyer in seen:
                        raise NativeCompatibilityError('接待列表存在无法区分的同名会话，需要进一步核对身份字段')
                    self._buyer_targets[(window,seller,switched.buyer)] = identity
                    seen.add(switched.buyer)
                    snapshots.append(switched)
                    current = switched
                except NativeCompatibilityError:
                    raise
                except Exception as exc:
                    self.coverage = 'current_chat'
                    raise NativeCompatibilityError('千牛接待列表无法可靠切换，需要适配当前客户端') from exc
            return snapshots

    def _window(self, handle, seller):
        uia, _, IntPtr = self._load()
        known = [w for w in self.windows() if w['window'] == handle and w['seller'] == seller]
        if len(known) != 1:
            raise NativeCompatibilityError('千牛窗口或登录账号已变化，请重新连接')
        return uia.AutomationElement.FromHandle(IntPtr(handle))

    def _control(self, root, identity):
        uia, _, _ = self._load()
        condition = uia.PropertyCondition(uia.AutomationElement.AutomationIdProperty, identity)
        values = root.FindAll(uia.TreeScope.Descendants, condition)
        if values.Count != 1:
            raise NativeCompatibilityError('当前千牛版本的控件结构不同，需要核对客户端适配')
        return values[0]

    def _value(self, element):
        uia, _, _ = self._load()
        if element.Current.IsPassword:
            raise NativeCompatibilityError('不读取密码控件')
        try:
            pattern = element.GetCurrentPattern(uia.ValuePattern.Pattern)
            return str(pattern.Current.Value)
        except Exception:
            return str(element.Current.Name or '')

    def snapshot(self, window, seller):
        with _UI_LOCK:
            uia, _, _ = self._load()
            root = self._window(window, seller)
            buyer = self._value(self._control(root, BUYER_ID)).strip()
            if not buyer or buyer in ('[', ']', '暂无会话') or len(buyer) > 100:
                raise NativeCompatibilityError('千牛未提供明确的当前买家身份')
            history = self._control(root, HISTORY_ID)
            try:
                text = str(history.GetCurrentPattern(uia.TextPattern.Pattern).DocumentRange.GetText(64001))
            except Exception as exc:
                raise NativeCompatibilityError('千牛聊天区未暴露可核对的文本结构，需要针对客户端适配') from exc
            editor = self._value(self._control(root, EDITOR_ID))
            messages = tuple(parse_transcript(text, seller, buyer))
            # Reading the identity twice detects a UI switch during extraction.
            if buyer != self._value(self._control(root, BUYER_ID)).strip():
                raise NativeCompatibilityError('读取时顾客已切换，请重新检查')
            return NativeSnapshot(window, seller, buyer, text, editor, messages)

    def preflight(self, expected):
        if not self.verified_message_metadata:
            raise NativeCompatibilityError('当前千牛只读到了合并文本，尚未核实逐条消息的作者与标识，不能发送；请先完成客户端结构联调')
        uia, _, _ = self._load()
        current = self.snapshot(expected.window, expected.seller)
        if current.fingerprint != expected.fingerprint or current.editor:
            raise NativeCompatibilityError('顾客、消息或人工输入已变化，本次发送取消')
        root = self._window(expected.window, expected.seller)
        editor = self._control(root, EDITOR_ID).GetCurrentPattern(uia.ValuePattern.Pattern)
        button = self._control(root, SEND_ID).GetCurrentPattern(uia.InvokePattern.Pattern)
        if editor.Current.IsReadOnly:
            raise NativeCompatibilityError('千牛输入区当前不可编辑')
        return editor, button

    def submit(self, expected, text):
        with _UI_LOCK:
            editor, button = self.preflight(expected)
            editor.SetValue(text)
            current = self.snapshot(expected.window, expected.seller)
            if current.fingerprint != expected.fingerprint or current.editor != text:
                raise NativeCompatibilityError('写入后会话或正文不一致，没有点击发送')
            button.Invoke()
            deadline = time.monotonic() + 3
            old_ids = {m.message_id for m in expected.messages}
            while time.monotonic() < deadline:
                current = self.snapshot(expected.window, expected.seller)
                if current.buyer != expected.buyer:
                    raise NativeCompatibilityError('发送后顾客切换，结果需在原平台核对')
                matches = [m for m in current.messages if m.message_id not in old_ids and m.role == 'agent' and m.content == text]
                if len(matches) == 1:
                    return current, matches[0].message_id
                if len(matches) > 1:
                    raise NativeCompatibilityError('出现重复客服回显，不能确认本轮结果')
                time.sleep(.15)
            raise NativeCompatibilityError('没有观察到新的客服消息回显，结果未知')
