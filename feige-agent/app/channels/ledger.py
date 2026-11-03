"""Durable live-message provenance and conservative single-attempt delivery ledger."""
import json
import uuid

from ..platforms import MessageEvent
from ..service.multichannel import ChannelInbox
from ..service.knowledge import now


class LiveLedger:
    def __init__(self, workspace):
        self.workspace = workspace
        with workspace.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS live_accounts (
                store_id TEXT PRIMARY KEY, account_key TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS live_message_meta (
                scope_key TEXT, message_id TEXT, account_key TEXT, timestamp INTEGER,
                action TEXT, PRIMARY KEY(scope_key,message_id));
              CREATE TABLE IF NOT EXISTS live_deliveries (
                token TEXT PRIMARY KEY, scope_key TEXT, account_key TEXT, head INTEGER,
                text TEXT, state TEXT, remote_message_id TEXT DEFAULT '', created_at TEXT,
                UNIQUE(scope_key,head));
              CREATE TABLE IF NOT EXISTS live_delivery_reviews (
                token TEXT PRIMARY KEY, scope_key TEXT, note TEXT, created_at TEXT);
              CREATE TABLE IF NOT EXISTS live_handoff_reviews (
                scope_key TEXT,message_id TEXT,head INTEGER,created_at TEXT,
                PRIMARY KEY(scope_key,message_id));
            """)
            db.execute('BEGIN IMMEDIATE')
            columns={r['name'] for r in db.execute('PRAGMA table_info(live_deliveries)')}
            for name,definition in (('interaction_id','INTEGER'),('automatic','INTEGER DEFAULT 0'),('learning_recorded','INTEGER DEFAULT 0')):
                if name not in columns:
                    db.execute('ALTER TABLE live_deliveries ADD COLUMN '+name+' '+definition)
            columns={r['name'] for r in db.execute('PRAGMA table_info(live_message_meta)')}
            for name,definition in (('product_id',"TEXT DEFAULT ''"),('kind',"TEXT DEFAULT 'text'")):
                if name not in columns:
                    db.execute('ALTER TABLE live_message_meta ADD COLUMN '+name+' '+definition)

    def recover(self, spec):
        """Called only after acquiring this store's exclusive connection lease."""
        with self.workspace.connect() as db:
            scopes = "SELECT scope_key FROM channel_threads WHERE merchant_id=? AND platform=? AND store_id=?"
            params = (spec.merchant_id, spec.platform, spec.store_id)
            db.execute("UPDATE live_deliveries SET state='delivery_unknown' WHERE state='attempting' AND scope_key IN ("+scopes+")", params)
            db.execute("""UPDATE channel_threads SET pause_reason=CASE WHEN paused=0 OR pause_reason='connection' THEN 'unconfirmed_delivery' ELSE pause_reason END,
              paused=1,draft='',draft_head=NULL WHERE scope_key IN
              (SELECT scope_key FROM live_deliveries WHERE state='delivery_unknown') AND scope_key IN ("""+scopes+")",params)

    def bind(self, spec, account_key):
        self.workspace.bind(spec)
        with self.workspace.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            duplicate=db.execute('SELECT 1 FROM live_accounts WHERE account_key=? AND store_id<>? LIMIT 1',
                                 (account_key,spec.store_id)).fetchone()
            if duplicate:
                raise ValueError('该平台账号已关联其他资料账户，请使用原账户，不能重复接待同一账号')
            db.execute("INSERT OR IGNORE INTO live_accounts VALUES(?,?)", (spec.store_id, account_key))
            saved = db.execute("SELECT account_key FROM live_accounts WHERE store_id=?", (spec.store_id,)).fetchone()
            if saved[0] != account_key:
                raise ValueError("该资料账户已绑定其他平台店铺或客服账号，请新建资料账户，不能覆盖旧绑定")

    @staticmethod
    def _check_account(db, spec, account_key):
        row = db.execute("SELECT account_key FROM live_accounts WHERE store_id=?", (spec.store_id,)).fetchone()
        if not row or row[0] != account_key:
            raise ValueError("平台登录身份与本地绑定不符")

    def ingest(self, spec, account_key, message, historical=False):
        scope = ChannelInbox.scope(spec, message.conversation_id)
        MessageEvent(scope, message.message_id, message.role, message.content)
        if message.kind not in ('text','product_card','emotion','control','unsupported') or not isinstance(message.product_id,str) or len(message.product_id)>180:
            raise ValueError('消息商品引用结构无效')
        if (message.kind=='product_card') != bool(message.product_id):
            raise ValueError('商品卡片需要独立的商品标识，其他消息不能伪造商品引用')
        with self.workspace.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_account(db, spec, account_key)
            old = db.execute("SELECT * FROM channel_messages WHERE scope_key=? AND message_id=?",
                             (scope.key, message.message_id)).fetchone()
            if old:
                meta = db.execute("SELECT * FROM live_message_meta WHERE scope_key=? AND message_id=?",
                                  (scope.key, message.message_id)).fetchone()
                if (not meta or meta['account_key'] != account_key or old['role'] != message.role or old['content'] != message.content
                    or meta['product_id'] != message.product_id or meta['kind'] != message.kind):
                    raise ValueError("消息标识与历史内容冲突，请在原平台核对")
                return 0
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='observed_history'").fetchone():
                history = db.execute('SELECT * FROM observed_history WHERE scope_key=? AND message_id=?',
                                     (scope.key,message.message_id)).fetchone()
                if history:
                    if (history['account_key']!=account_key or any(history[key]!=getattr(message,key)
                        for key in ('role','content','timestamp','product_id','kind'))):
                        raise ValueError('实时消息与已观察历史冲突，请在原平台核对')
                    expected_echo = message.role=='agent' and db.execute('''SELECT 1 FROM live_deliveries
                      WHERE scope_key=? AND remote_message_id=? AND text=? AND state='awaiting_echo' ''',
                        (scope.key,message.message_id,message.content)).fetchone()
                    if not expected_echo:
                        return 0  # History replay is not a newly arrived question.
            # Imported history and observed live traffic must not be silently mixed.
            imported = db.execute("""SELECT 1 FROM channel_messages m LEFT JOIN live_message_meta l
              ON m.scope_key=l.scope_key AND m.message_id=l.message_id
              WHERE m.scope_key=? AND l.message_id IS NULL LIMIT 1""", (scope.key,)).fetchone()
            if imported:
                raise ValueError("此会话已有手动导入记录，请使用单独的新资料账户进行实时联调")
            latest_time = db.execute("SELECT MAX(timestamp) FROM live_message_meta WHERE scope_key=?", (scope.key,)).fetchone()[0]
            db.execute("INSERT OR IGNORE INTO channel_threads(scope_key,merchant_id,platform,store_id,conversation_id,updated_at) VALUES(?,?,?,?,?,?)",
                       (scope.key, scope.merchant_id, scope.platform, scope.store_id, scope.conversation_id, now()))
            db.execute("INSERT INTO channel_messages(scope_key,message_id,role,content) VALUES(?,?,?,?)",
                       (scope.key, message.message_id, message.role, message.content))
            db.execute("INSERT INTO live_message_meta(scope_key,message_id,account_key,timestamp,action,product_id,kind) VALUES(?,?,?,?,?,?,?)",
                       (scope.key, message.message_id, account_key, message.timestamp, message.action,message.product_id,message.kind))
            own_echo = db.execute("SELECT token FROM live_deliveries WHERE scope_key=? AND remote_message_id=? AND text=? AND state='awaiting_echo'",
                                  (scope.key, message.message_id, message.content)).fetchone() if message.role == 'agent' else None
            reason=('unsupported_content' if message.action=='pause' else 'message_order' if latest_time is not None and message.timestamp<latest_time
                    else 'human_takeover' if message.role=='agent' and not own_echo and not historical else '')
            previous=db.execute('SELECT paused,pause_reason FROM channel_threads WHERE scope_key=?',(scope.key,)).fetchone()
            if reason:
                if reason=='human_takeover' and previous['paused'] and previous['pause_reason'] not in ('','connection'):
                    reason=previous['pause_reason']
                db.execute('UPDATE channel_threads SET paused=1,pause_reason=? WHERE scope_key=?',(reason,scope.key))
            elif (previous['pause_reason']=='connection' and message.role=='buyer' and message.action=='reply' and not historical):
                unresolved=db.execute("SELECT 1 FROM live_deliveries WHERE scope_key=? AND state NOT IN ('confirmed','manually_closed') LIMIT 1",(scope.key,)).fetchone()
                if not unresolved:db.execute("UPDATE channel_threads SET paused=0,pause_reason='' WHERE scope_key=?",(scope.key,))
            db.execute("UPDATE channel_threads SET draft='',draft_head=NULL,updated_at=? WHERE scope_key=?",(now(),scope.key))
            if own_echo:
                db.execute("UPDATE live_deliveries SET state='confirmed' WHERE token=?", (own_echo[0],))
        return 1

    def reserve(self, spec, account_key, conversation_id, head, text, automatic=False):
        scope = ChannelInbox.scope(spec, conversation_id)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 800:
            raise ValueError("请核对1–800字的回复")
        with self.workspace.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_account(db, spec, account_key)
            thread = db.execute("SELECT * FROM channel_threads WHERE scope_key=?", (scope.key,)).fetchone()
            latest = db.execute("SELECT * FROM channel_messages WHERE scope_key=? ORDER BY sequence DESC LIMIT 1", (scope.key,)).fetchone()
            if not thread or thread['paused'] or not latest or latest['sequence'] != head or latest['role'] != 'buyer':
                raise ValueError("顾客消息或人工接管状态已变化，旧回复已取消")
            draft = json.loads(thread['draft']) if thread['draft'] else {}
            if thread['draft_head'] != head or draft.get('action') not in ('reply', 'ask_followup') or draft.get('draft_reply') != text:
                raise ValueError("只允许发送当前已生成并审核的原文草稿；修改后请重新检查")
            live = db.execute("SELECT * FROM live_message_meta WHERE scope_key=? AND message_id=?", (scope.key, latest['message_id'])).fetchone()
            if not live or live['account_key'] != account_key or live['action'] != 'reply':
                raise ValueError("此消息不是本店可回复的实时文字咨询")
            pending = db.execute("SELECT 1 FROM live_deliveries WHERE scope_key=? AND (head=? OR state NOT IN ('confirmed','manually_closed')) LIMIT 1",
                                 (scope.key, head)).fetchone()
            if pending:
                raise ValueError("已有发送记录或未确认结果，请在原平台核对，不能重复发送")
            token = uuid.uuid4().hex
            interaction_id=draft.get('interaction_id')
            if type(interaction_id) is not int or interaction_id<1:
                interaction_id=None
            db.execute("INSERT INTO live_deliveries(token,scope_key,account_key,head,text,state,created_at,interaction_id,automatic) VALUES(?,?,?,?,?,'attempting',?,?,?)",
                       (token, scope.key, account_key, head, text, now(),interaction_id,int(automatic is True)))
        return token

    def is_live(self, spec, conversation_id):
        scope = ChannelInbox.scope(spec, conversation_id)
        with self.workspace.connect() as db:
            return bool(db.execute("SELECT 1 FROM live_message_meta WHERE scope_key=? LIMIT 1", (scope.key,)).fetchone())

    def head(self, spec, conversation_id):
        scope = ChannelInbox.scope(spec, conversation_id)
        with self.workspace.connect() as db:
            row = db.execute("SELECT m.*,t.context_revision,t.paused,t.pause_reason,l.action FROM channel_messages m JOIN channel_threads t ON m.scope_key=t.scope_key LEFT JOIN live_message_meta l ON l.scope_key=m.scope_key AND l.message_id=m.message_id WHERE m.scope_key=? ORDER BY m.sequence DESC LIMIT 1", (scope.key,)).fetchone()
        return dict(row) if row else {}

    def complete_handoff(self,spec,conversation_id,event):
        scope=ChannelInbox.scope(spec,conversation_id)
        mid=event.get('message_id')
        if not mid or type(event.get('head')) is not int:
            return False
        if event.get('scope_key')!=scope.key or event.get('platform')!=spec.platform or event.get('store_id')!=spec.store_id:
            raise ValueError('人工待办与当前店铺会话不符')
        with self.workspace.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            original=db.execute('SELECT sequence,role FROM channel_messages WHERE scope_key=? AND message_id=?',
                                (scope.key,mid)).fetchone()
            if not original or original['sequence']!=event['head'] or original['role']!='buyer':
                raise ValueError('人工待办缺少对应的顾客原文')
            db.execute('INSERT OR IGNORE INTO live_handoff_reviews VALUES(?,?,?,?)',(scope.key,mid,event['head'],now()))
            db.execute("UPDATE live_message_meta SET action='context' WHERE scope_key=? AND message_id=?",(scope.key,mid))
            thread=db.execute('SELECT pause_reason,handoff_head FROM channel_threads WHERE scope_key=?',(scope.key,)).fetchone()
            resumed=bool(thread and thread['pause_reason']=='handoff' and thread['handoff_head']==event['head'])
            if resumed:
                db.execute("UPDATE channel_threads SET paused=0,pause_reason='',handoff_head=NULL,draft='',draft_head=NULL,context_revision=context_revision+1 WHERE scope_key=?",(scope.key,))
        return resumed

    def handled_handoffs(self,spec,conversation_id):
        scope=ChannelInbox.scope(spec,conversation_id)
        with self.workspace.connect() as db:
            return {r['message_id'] for r in db.execute('SELECT message_id FROM live_handoff_reviews WHERE scope_key=?',(scope.key,))}

    def product_context(self,spec,conversation_id,first_sequence,last_sequence):
        scope=ChannelInbox.scope(spec,conversation_id)
        base="""SELECT m.sequence,m.message_id,l.product_id,l.kind FROM live_message_meta l JOIN channel_messages m
          ON m.scope_key=l.scope_key AND m.message_id=l.message_id WHERE m.scope_key=? AND l.kind='product_card'"""
        with self.workspace.connect() as db:
            prior=db.execute(base+' AND m.sequence<? ORDER BY m.sequence DESC LIMIT 8',(scope.key,first_sequence)).fetchall()
            recent=db.execute(base+' AND m.sequence>=? AND m.sequence<=? ORDER BY m.sequence',
                              (scope.key,first_sequence,last_sequence)).fetchall()
        return [dict(r) for r in reversed(prior)],{r['message_id']:dict(r) for r in recent}

    def invalidate_connection(self, spec):
        with self.workspace.connect() as db:
            db.execute("""UPDATE channel_threads SET pause_reason=CASE WHEN paused=0 THEN 'connection' ELSE pause_reason END,
              paused=1,draft='',draft_head=NULL
              WHERE merchant_id=? AND platform=? AND store_id=? AND EXISTS(
              SELECT 1 FROM live_message_meta m WHERE m.scope_key=channel_threads.scope_key)""",
                       (spec.merchant_id, spec.platform, spec.store_id))

    def finish(self, token, state, remote_message_id=''):
        if state not in ('delivery_unknown', 'awaiting_echo'):
            raise ValueError('发送状态无效')
        with self.workspace.connect() as db:
            row = db.execute("SELECT * FROM live_deliveries WHERE token=?", (token,)).fetchone()
            if not row or row['state'] in ('confirmed','manually_closed'):
                return
            state = state if remote_message_id or state == 'delivery_unknown' else 'delivery_unknown'
            echo = db.execute("SELECT * FROM channel_messages WHERE scope_key=? AND message_id=? AND role='agent' AND content=? AND sequence>?",
                              (row['scope_key'], remote_message_id, row['text'], row['head'])).fetchone() if remote_message_id else None
            if echo:
                state = 'confirmed'
                after = db.execute("SELECT COUNT(*) FROM channel_messages WHERE scope_key=? AND sequence>?", (row['scope_key'], row['head'])).fetchone()[0]
                if after == 1:
                    db.execute("UPDATE channel_threads SET paused=0,pause_reason='' WHERE scope_key=? AND pause_reason='human_takeover'", (row['scope_key'],))
            db.execute("UPDATE live_deliveries SET state=?,remote_message_id=? WHERE token=?", (state, remote_message_id, token))
            if state == 'delivery_unknown':
                db.execute("UPDATE channel_threads SET pause_reason=CASE WHEN paused=0 OR pause_reason='connection' THEN 'unconfirmed_delivery' ELSE pause_reason END,paused=1,draft='',draft_head=NULL WHERE scope_key=?", (row['scope_key'],))

    def delivery(self, token):
        with self.workspace.connect() as db:
            row = db.execute("SELECT state,remote_message_id FROM live_deliveries WHERE token=?", (token,)).fetchone()
        return {**dict(row), 'sent': row['state'] == 'confirmed', 'token': token,
                'hint': ('已观察到匹配的平台客服消息' if row['state'] == 'confirmed' else
                         '店主已核对并关闭本轮；不算确认发送，旧回复不会重发' if row['state']=='manually_closed' else
                         '发送结果尚未确认，请在原平台核对，不要重复发送')}

    def history(self,spec,conversation_id):
        scope=ChannelInbox.scope(spec,conversation_id)
        with self.workspace.connect() as db:
            rows=db.execute('SELECT token,head,state,remote_message_id,created_at FROM live_deliveries WHERE scope_key=? ORDER BY rowid DESC LIMIT 10',(scope.key,)).fetchall()
        return [dict(r) for r in rows]

    def close_uncertain(self,spec,conversation_id,token,reviewed=False):
        if reviewed is not True:
            raise ValueError('请先在原平台核对本轮咨询，并确认已经人工处理完毕')
        scope=ChannelInbox.scope(spec,conversation_id)
        with self.workspace.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM live_deliveries WHERE token=? AND scope_key=?',(token,scope.key)).fetchone()
            if not row or row['state'] not in ('delivery_unknown','awaiting_echo','manually_closed'):
                raise ValueError('此发送记录尚在进行、已确认，或不属于当前店铺顾客')
            if row['state']=='manually_closed':return
            db.execute("UPDATE live_deliveries SET state='manually_closed' WHERE token=?",(token,))
            db.execute('INSERT INTO live_delivery_reviews VALUES(?,?,?,?)',
                       (token,scope.key,'店主确认已在原平台核对并处理完毕；未证明原发送成功',now()))
            latest=db.execute('SELECT sequence FROM channel_messages WHERE scope_key=? ORDER BY sequence DESC LIMIT 1',(scope.key,)).fetchone()
            # Newer buyer or human activity may have its own pause reason. Keep it.
            if latest and latest['sequence']==row['head']:
                db.execute("UPDATE channel_threads SET paused=0,pause_reason='',draft='',draft_head=NULL WHERE scope_key=? AND pause_reason='unconfirmed_delivery'",(scope.key,))

    def unreconciled(self,spec):
        with self.workspace.connect() as db:
            rows=db.execute("""SELECT d.token,d.interaction_id,d.automatic FROM live_deliveries d
              JOIN channel_threads t ON d.scope_key=t.scope_key WHERE t.merchant_id=? AND t.platform=? AND t.store_id=?
              AND d.state='confirmed' AND d.interaction_id IS NOT NULL AND d.learning_recorded=0 LIMIT 100""",
              (spec.merchant_id,spec.platform,spec.store_id)).fetchall()
        return [dict(r) for r in rows]

    def acknowledge_learning(self,token):
        with self.workspace.connect() as db:
            db.execute("UPDATE live_deliveries SET learning_recorded=1 WHERE token=? AND state='confirmed'",(token,))
