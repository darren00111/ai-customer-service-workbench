"""Observed history is context, never a new live head or a delivery receipt."""
import base64
import json
import math
import queue
from urllib.parse import urlsplit

from ..platforms import MessageEvent
from ..service.multichannel import ChannelInbox
from ..service.knowledge import now

PATH = '/plateau/chat/list'
LIMIT = 2 * 1024 * 1024
METHODS = ('Network.responseReceived', 'Network.loadingFinished', 'Network.loadingFailed')


def parse_history(value, account):
    from .pinduoduo import normalize_frame
    if not isinstance(value, dict) or value.get('success') is not True:
        raise ValueError('历史消息接口未返回成功结果')
    result = value.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('messages'), list) or len(result['messages']) > 1000:
        raise ValueError('历史消息结构未识别或单页过大')
    rows = {}
    for raw in result['messages']:
        message, reason = normalize_frame(json.dumps({'response':'push','message':raw}, ensure_ascii=False), account)
        if reason == 'read_receipt':
            continue
        if message is None:
            raise ValueError('历史消息的双方身份或消息标识无法核实')
        prior = rows.get(message.message_id)
        if prior is not None and prior != message:
            raise ValueError('历史消息标识与正文冲突')
        rows[message.message_id] = message
    if len({m.conversation_id for m in rows.values()}) > 1:
        raise ValueError('单次历史响应混入多个顾客，未采用')
    if len({m.timestamp for m in rows.values()})!=len(rows):
        raise ValueError('历史消息时间相同，尚未核实同刻消息顺序')
    return sorted(rows.values(), key=lambda m:(m.timestamp, m.message_id))


class HistoryContext:
    def __init__(self, workspace):
        self.workspace = workspace
        with workspace.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS observed_history (
              scope_key TEXT, message_id TEXT, account_key TEXT, role TEXT, content TEXT,
              timestamp INTEGER, action TEXT, product_id TEXT, kind TEXT,
              PRIMARY KEY(scope_key,message_id))''')

    def ingest(self, spec, account_key, messages):
        if spec.platform != 'pinduoduo' or len(messages) > 1000:
            raise ValueError('历史消息来源或数量无效')
        prepared = []
        for message in messages:
            scope = ChannelInbox.scope(spec, message.conversation_id)
            MessageEvent(scope, message.message_id, message.role, message.content)
            if type(message.timestamp) is not int or message.timestamp <= 0:
                raise ValueError('历史消息没有可靠时间')
            if (message.kind not in ('text','emotion','product_card','control','unsupported')
                or not isinstance(message.product_id,str) or len(message.product_id)>180
                or (message.kind == 'product_card') != bool(message.product_id)):
                raise ValueError('历史商品引用无效')
            prepared.append((scope, message))
        changed = set()
        with self.workspace.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from .ledger import LiveLedger
            LiveLedger._check_account(db, spec, account_key)
            for scope, message in prepared:
                # Replays of a known live ID must agree with its original source.
                live = db.execute('''SELECT m.role,m.content,l.timestamp,l.product_id,l.kind,l.account_key
                  FROM channel_messages m JOIN live_message_meta l ON m.scope_key=l.scope_key AND m.message_id=l.message_id
                  WHERE m.scope_key=? AND m.message_id=?''', (scope.key, message.message_id)).fetchone()
                old = live or db.execute('SELECT * FROM observed_history WHERE scope_key=? AND message_id=?',
                                        (scope.key, message.message_id)).fetchone()
                expected = {k:getattr(message,k) for k in ('role','content','timestamp','product_id','kind')}
                if old:
                    if old['account_key'] != account_key or any(old[k] != v for k,v in expected.items()):
                        raise ValueError('历史消息与已观察原文冲突，请在原平台核对')
                    continue
                count = db.execute('SELECT COUNT(*) FROM observed_history WHERE scope_key=?', (scope.key,)).fetchone()[0]
                if count >= 2000:
                    raise ValueError('本会话历史已达2000条，请先整理资料后再采集')
                db.execute('INSERT INTO observed_history VALUES(?,?,?,?,?,?,?,?,?)',
                           (scope.key,message.message_id,account_key,message.role,message.content,
                            message.timestamp,message.action,message.product_id,message.kind))
                changed.add(scope.key)
                head = db.execute('''SELECT l.timestamp FROM channel_messages m JOIN live_message_meta l
                  ON m.scope_key=l.scope_key AND m.message_id=l.message_id
                  WHERE m.scope_key=? ORDER BY m.sequence DESC LIMIT 1''', (scope.key,)).fetchone()
                # A response exposes traffic not seen on the live stream. Never
                # answer its old head or manufacture a "recovered" live event.
                if head and message.timestamp >= head['timestamp']:
                    db.execute("UPDATE channel_threads SET paused=1,pause_reason='history_gap' WHERE scope_key=?", (scope.key,))
                    db.execute('''UPDATE live_message_meta SET action='context' WHERE scope_key=? AND message_id=(
                      SELECT message_id FROM channel_messages WHERE scope_key=? ORDER BY sequence DESC LIMIT 1)''',
                               (scope.key,scope.key))
            for key in changed:
                db.execute("UPDATE channel_threads SET context_revision=context_revision+1,draft='',draft_head=NULL,updated_at=? WHERE scope_key=?", (now(),key))
        return len(changed)

    def bundle(self, spec, conversation_id, live_messages, ttl=3600):
        scope = ChannelInbox.scope(spec, conversation_id)
        with self.workspace.connect() as db:
            live = [dict(r) for r in db.execute('''SELECT m.*,l.timestamp,l.product_id,l.kind FROM channel_messages m
              JOIN live_message_meta l ON m.scope_key=l.scope_key AND m.message_id=l.message_id
              WHERE m.scope_key=? ORDER BY m.sequence''', (scope.key,))]
            if not live or live[-1]['message_id'] != live_messages[-1]['message_id']:
                raise ValueError('会话已更新，不能使用旧的历史上下文')
            ids = {r['message_id'] for r in live}
            head = live[-1]
            rows = [dict(r) for r in db.execute('SELECT * FROM observed_history WHERE scope_key=? AND timestamp<? ORDER BY timestamp,message_id',
                                               (scope.key,head['timestamp'])) if r['message_id'] not in ids]
        combined = sorted(rows+live, key=lambda r:(r['timestamp'],r.get('sequence',0)))
        try:
            ttl = float(ttl)
            ttl = max(60,ttl) if math.isfinite(ttl) else 3600
        except (TypeError,ValueError):
            ttl = 3600
        start = 0
        for index in range(1,len(combined)):
            if combined[index]['timestamp']-combined[index-1]['timestamp'] > ttl*1000:
                start = index
        combined = combined[start:]
        selected = combined[-24:]
        prior = [r for r in combined[:-24] if r['kind']=='product_card'][-8:]
        refs = {r['message_id']:r for r in selected if r['kind']=='product_card'}
        return selected, prior, refs

    def list(self, spec, conversation_id):
        scope = ChannelInbox.scope(spec, conversation_id)
        with self.workspace.connect() as db:
            return [dict(r) for r in db.execute('''SELECT message_id,role,content,timestamp,kind,product_id
              FROM observed_history WHERE scope_key=? ORDER BY timestamp,message_id LIMIT 2000''', (scope.key,))]


class HistoryCapture:
    def __init__(self, cdp, store, spec, account, on_history=None):
        self.cdp, self.store, self.spec, self.account = cdp, store, spec, account
        self.stream = cdp.subscribe(METHODS)
        self.pending = set()
        self.pages = 0
        self.on_history = on_history
        self.learning_error = ''
        self.revisions = set()

    def poll(self):
        if self.stream.overflowed:
            raise ValueError('历史消息采集缓冲已满，请重新连接并核对会话')
        for _ in range(1000):
            try:
                event = self.stream.events.get_nowait()
            except queue.Empty:
                break
            method, p = event.get('method'), event.get('params') or {}
            rid = p.get('requestId')
            if method == 'Network.responseReceived':
                response = p.get('response') or {}
                url = urlsplit(response.get('url') or '')
                if (url.scheme=='https' and url.hostname=='mms.pinduoduo.com' and url.path==PATH
                    and p.get('type') in ('XHR','Fetch') and response.get('status')==200
                    and response.get('mimeType','').startswith('application/json')):
                    if len(self.pending)>=50:
                        raise ValueError('待采集历史响应过多，请重新连接')
                    self.pending.add(rid)
            elif method in ('Network.loadingFinished','Network.loadingFailed') and rid in self.pending:
                self.pending.remove(rid)
                if method == 'Network.loadingFailed':
                    continue
                if p.get('encodedDataLength',0)>LIMIT:
                    raise ValueError('历史响应过大，请分批查看会话')
                body = self.cdp.call('Network.getResponseBody', {'requestId':rid}, timeout=3)
                raw = body.get('body')
                if not isinstance(raw,str) or len(raw)>LIMIT:
                    raise ValueError('历史响应缺失或过大')
                if body.get('base64Encoded'):
                    raw = base64.b64decode(raw,validate=True).decode('utf-8')
                if len(raw.encode('utf-8'))>LIMIT:
                    raise ValueError('历史响应解码后过大')
                messages = parse_history(json.loads(raw), self.account)
                self.store.ingest(self.spec, self.account.key, messages)
                self.pages += 1
                if self.on_history:
                    try:
                        self.revisions.update(self.on_history(messages))
                        self.learning_error=''
                    except Exception:
                        self.learning_error='历史上下文已保存，但待审核问答生成未完成，请重新打开会话后重试'

    def status(self):
        return {'pages':self.pages,'candidate_versions':len(self.revisions),'learning_error':self.learning_error,
                'complete_history':False}
