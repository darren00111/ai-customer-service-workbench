"""Local multi-platform preparation; imported conversations never send messages."""
import copy
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..platforms import ConversationScope, MessageEvent
from .knowledge import now


class WorkspaceData:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    store_id TEXT PRIMARY KEY, merchant_id TEXT, platform TEXT);
                CREATE TABLE IF NOT EXISTS common_products (
                    revision INTEGER PRIMARY KEY, merchant_id TEXT, product_key TEXT,
                    attributes TEXT, source TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS product_links (
                    merchant_id TEXT, platform TEXT, store_id TEXT, product_id TEXT,
                    shared_revision INTEGER, target_revision INTEGER,
                    PRIMARY KEY(merchant_id,platform,store_id,product_id));
                CREATE TABLE IF NOT EXISTS channel_threads (
                    scope_key TEXT PRIMARY KEY, merchant_id TEXT, platform TEXT, store_id TEXT,
                    conversation_id TEXT, paused INTEGER DEFAULT 0, draft TEXT DEFAULT '',
                    draft_head INTEGER, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS channel_messages (
                    sequence INTEGER PRIMARY KEY, scope_key TEXT, message_id TEXT,
                    role TEXT, content TEXT, UNIQUE(scope_key,message_id));
                CREATE TABLE IF NOT EXISTS channel_handoff_outbox (
                    scope_key TEXT,head INTEGER,payload TEXT,published INTEGER DEFAULT 0,
                    PRIMARY KEY(scope_key,head));
                CREATE TABLE IF NOT EXISTS channel_corrections (
                    scope_key TEXT,head INTEGER,override_id INTEGER,changes TEXT,created_at TEXT,
                    PRIMARY KEY(scope_key,override_id));
            """)
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(channel_threads)')}
            if 'context_revision' not in columns:
                db.execute('ALTER TABLE channel_threads ADD COLUMN context_revision INTEGER DEFAULT 0')
            if 'pause_reason' not in columns:
                db.execute("ALTER TABLE channel_threads ADD COLUMN pause_reason TEXT DEFAULT ''")
                db.execute("UPDATE channel_threads SET pause_reason='legacy' WHERE paused=1")
            if 'handoff_head' not in columns:
                db.execute('ALTER TABLE channel_threads ADD COLUMN handoff_head INTEGER')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, spec):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO channel_bindings VALUES(?,?,?)",
                       (spec.store_id, spec.merchant_id, spec.platform))
            row = db.execute("SELECT * FROM channel_bindings WHERE store_id=?", (spec.store_id,)).fetchone()
            if (row['merchant_id'], row['platform']) != (spec.merchant_id, spec.platform):
                raise ValueError("该店铺已有其他商家或平台的数据，请创建新的店铺标识，避免混用历史")


class SharedCatalog:
    def __init__(self, workspace):
        self.workspace = workspace

    def save(self, spec, product_key, attributes, source, reviewed=False):
        self.workspace.bind(spec)
        if reviewed is not True:
            raise ValueError("请核对共用商品属性并确认适用于同款商品")
        if not isinstance(product_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", product_key):
            raise ValueError("共用商品标识需为1–64位字母、数字、下划线或短横线")
        if not isinstance(source, str) or not 1 <= len(source.strip()) <= 200:
            raise ValueError("请填写1–200字资料来源")
        if not isinstance(attributes, dict) or not 1 <= len(attributes) <= 50:
            raise ValueError("请填写1–50项共用商品属性")
        for key, value in attributes.items():
            if (not isinstance(key, str) or not 1 <= len(key) <= 60 or
                    re.search(r"价格|售价|库存|优惠|折扣|促销|赠品|运费|发货|到货|送达|退款|退货|换货|售后|承诺|包邮|配送|快递|支付|下单|活动|保价|保修|price|stock|inventory|shipping|policy|promotion|discount|delivery|refund|payment|freight", key, re.I)):
                raise ValueError("共用资料仅保存商品属性，价格、库存、优惠和店铺政策请在各店维护")
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
                raise ValueError("共用属性值需为1–2000字文字")
        with self.workspace.connect() as db:
            cursor = db.execute("INSERT INTO common_products(merchant_id,product_key,attributes,source,created_at) VALUES(?,?,?,?,?)",
                                (spec.merchant_id, product_key, json.dumps(attributes,ensure_ascii=False), source.strip(), now()))
            return cursor.lastrowid

    def list(self, spec):
        self.workspace.bind(spec)
        with self.workspace.connect() as db:
            rows = db.execute("SELECT * FROM common_products WHERE merchant_id=? ORDER BY revision DESC LIMIT 200", (spec.merchant_id,)).fetchall()
            links = db.execute("SELECT * FROM product_links WHERE merchant_id=? AND platform=? AND store_id=?",
                               (spec.merchant_id,spec.platform,spec.store_id)).fetchall()
        return {"products": [{**dict(row),"attributes":json.loads(row['attributes'])} for row in rows],
                "links": [dict(row) for row in links]}

    def stage_for_store(self, spec, kb, product_id, shared_revision, reviewed=False):
        self.workspace.bind(spec)
        if reviewed is not True:
            raise ValueError("请确认这是同款商品；本次会生成待启用版本")
        with self.workspace.connect() as db:
            common = db.execute("SELECT * FROM common_products WHERE revision=? AND merchant_id=?",
                                (int(shared_revision),spec.merchant_id)).fetchone()
        if not common:
            raise ValueError("本商家未找到此共用商品版本")
        active = kb.active('products')
        products = copy.deepcopy(active['payload']) if active else []
        product = next((row for row in products if row['product_id']==product_id),None)
        if not product:
            raise ValueError("请选择本店已启用的商品")
        shared = json.loads(common['attributes'])
        attributes = product.setdefault('attributes',{})
        inherited = product.get('shared_origin',{}).get('attributes',{})
        for key, value in inherited.items():
            if key not in shared and attributes.get(key)==value:
                attributes.pop(key,None)
        applied, protected = {}, []
        for key, value in shared.items():
            if key not in attributes or (key in inherited and attributes[key]==inherited[key]):
                attributes[key] = value
                applied[key] = value
            else:
                protected.append(key)
        product['shared_origin'] = {'revision':common['revision'],'product_key':common['product_key'],
                                    'source':common['source'],'attributes':applied}
        revision = kb.stage('products',products,'共用商品 %s #%s · %s' %
                            (common['product_key'],common['revision'],common['source']),base_revision=active['id'])
        with self.workspace.connect() as db:
            db.execute("INSERT INTO product_links VALUES(?,?,?,?,?,?) ON CONFLICT(merchant_id,platform,store_id,product_id) DO UPDATE SET shared_revision=excluded.shared_revision,target_revision=excluded.target_revision",
                       (spec.merchant_id,spec.platform,spec.store_id,product_id,common['revision'],revision))
        return {"revision":kb.preview(revision),"applied":list(applied),"preserved_local":protected}


class ChannelInbox:
    def __init__(self, workspace):
        self.workspace = workspace

    @staticmethod
    def scope(spec, conversation_id):
        return ConversationScope(spec.merchant_id,spec.platform,spec.store_id,conversation_id)

    def import_conversations(self, spec, content):
        self.workspace.bind(spec)
        if not isinstance(content,str) or len(content)>5_000_000:
            raise ValueError("会话文件限5MB")
        payload = json.loads(content)
        if not isinstance(payload,dict) or set(payload)!={'store_id','conversations'} or payload['store_id']!=spec.store_id:
            raise ValueError("导入文件必须明确属于当前店铺，且仅包含 store_id、conversations")
        conversations = payload['conversations']
        if not isinstance(conversations,list) or not 1<=len(conversations)<=200:
            raise ValueError("每次导入1–200个会话")
        prepared, count = [], 0
        for conversation in conversations:
            if not isinstance(conversation,dict) or set(conversation)!={'conversation_id','messages'}:
                raise ValueError("会话需要 conversation_id 和 messages")
            scope = self.scope(spec,conversation['conversation_id'])
            messages = conversation['messages']
            if not isinstance(messages,list) or not 1<=len(messages)<=1000:
                raise ValueError("每个会话需包含1–1000条按时间排列的消息")
            for message in messages:
                if not isinstance(message,dict) or set(message)!={'message_id','role','content'}:
                    raise ValueError("每条消息需要 message_id、role、content")
                MessageEvent(scope,**message)
            count += len(messages)
            if count>3000:
                raise ValueError("每次最多导入3000条消息")
            prepared.append((scope,messages))
        inserted = 0
        with self.workspace.connect() as db:
            for scope, messages in prepared:
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_message_meta'").fetchone() and db.execute("SELECT 1 FROM live_message_meta WHERE scope_key=? LIMIT 1", (scope.key,)).fetchone():
                    raise ValueError("实时采集会话不能混入手工导入记录，请新建资料账户验证导出文件")
                db.execute("INSERT OR IGNORE INTO channel_threads(scope_key,merchant_id,platform,store_id,conversation_id,updated_at) VALUES(?,?,?,?,?,?)",
                           (scope.key,scope.merchant_id,scope.platform,scope.store_id,scope.conversation_id,now()))
                # Overlapping exports must preserve order; older history cannot be appended after a reply.
                existing = db.execute("SELECT sequence,message_id,role,content FROM channel_messages WHERE scope_key=? ORDER BY sequence",(scope.key,)).fetchall()
                by_id = {row['message_id']:row for row in existing}
                unseen, last = False, -1
                for message in messages:
                    row = by_id.get(message['message_id'])
                    if row:
                        if unseen or row['sequence']<=last:
                            raise ValueError("导入顺序与已有消息冲突，请使用顺序一致的完整导出或新增消息")
                        if (row['role'],row['content'])!=(message['role'],message['content']):
                            raise ValueError("同一消息标识的内容发生变化，已拒绝覆盖")
                        last = row['sequence']
                    else:
                        unseen = True
                        cursor = db.execute("INSERT OR IGNORE INTO channel_messages(scope_key,message_id,role,content) VALUES(?,?,?,?)",
                                            (scope.key,message['message_id'],message['role'],message['content']))
                        if cursor.rowcount:
                            inserted += 1
                            by_id[message['message_id']] = {'sequence':cursor.lastrowid,**message}
                        db.execute("UPDATE channel_threads SET draft='',draft_head=NULL,updated_at=? WHERE scope_key=?",(now(),scope.key))
        return {"inserted":inserted,"conversations":len(prepared),"delivery_channel":"imported","sent":False}

    def get(self,spec,conversation_id):
        self.workspace.bind(spec)
        scope = self.scope(spec,conversation_id)
        with self.workspace.connect() as db:
            thread = db.execute("SELECT * FROM channel_threads WHERE scope_key=?",(scope.key,)).fetchone()
            if not thread:
                raise ValueError("本店未找到该会话")
            messages = db.execute("SELECT * FROM channel_messages WHERE scope_key=? ORDER BY sequence",(scope.key,)).fetchall()
        value = dict(thread)
        value['messages'] = [dict(row) for row in messages]
        value['draft'] = json.loads(value['draft']) if value['draft'] else None
        return value

    def list(self,spec):
        self.workspace.bind(spec)
        with self.workspace.connect() as db:
            has_live = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_message_meta'").fetchone()
            live_clause = 'EXISTS(SELECT 1 FROM live_message_meta l WHERE l.scope_key=t.scope_key)' if has_live else '0'
            rows = db.execute("""SELECT t.*,m.content AS latest_content,m.role AS latest_role,""" + live_clause + """ AS live
                FROM channel_threads t LEFT JOIN channel_messages m ON m.sequence=(
                  SELECT MAX(sequence) FROM channel_messages WHERE scope_key=t.scope_key)
                WHERE merchant_id=? AND platform=? AND store_id=? ORDER BY updated_at DESC,scope_key LIMIT 100""",
                (spec.merchant_id,spec.platform,spec.store_id)).fetchall()
        return [{**dict(row),'draft':bool(row['draft'])} for row in rows]

    def pause(self,spec,conversation_id,paused):
        if not isinstance(paused,bool):
            raise ValueError("暂停状态无效")
        thread = self.get(spec,conversation_id)
        with self.workspace.connect() as db:
            db.execute("UPDATE channel_threads SET paused=?,pause_reason=?,draft='',draft_head=NULL WHERE scope_key=?",
                       (int(paused),'manual' if paused else '',thread['scope_key']))
        return {"paused":paused,"sent":False}

    def invalidate_draft(self,spec,conversation_id,takeover=False):
        """A human correction cancels the current draft and any generation in flight."""
        thread = self.get(spec,conversation_id)
        with self.workspace.connect() as db:
            db.execute("UPDATE channel_threads SET draft='',draft_head=NULL,context_revision=context_revision+1,"
                       "paused=CASE WHEN ? THEN 1 ELSE paused END,pause_reason=CASE WHEN ? THEN 'manual' ELSE pause_reason END,"
                       "updated_at=? WHERE scope_key=?",(int(bool(takeover)),int(bool(takeover)),now(),thread['scope_key']))
        return {"draft_cancelled":bool(thread['draft']),"paused":bool(takeover) or bool(thread['paused']),"sent":False}

    def record_correction(self,spec,conversation_id,head,override_id,changes):
        scope=self.scope(spec,conversation_id)
        with self.workspace.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest=db.execute('SELECT MAX(sequence) FROM channel_messages WHERE scope_key=?',(scope.key,)).fetchone()[0]
            if latest!=head:
                raise ValueError('顾客消息已更新，请刷新后重新改判')
            thread=db.execute('SELECT paused,pause_reason FROM channel_threads WHERE scope_key=?',(scope.key,)).fetchone()
            if not thread:raise ValueError('本店未找到该会话')
            paused,reason=thread['paused'],thread['pause_reason']
            if changes.get('action') in {'handoff','keep_draft'} or changes.get('answer'):
                if not paused:paused,reason=1,'manual'
            elif changes.get('action')=='resume' and reason=='manual':paused,reason=0,''
            db.execute('INSERT INTO channel_corrections VALUES(?,?,?,?,?)',
                       (scope.key,head,override_id,json.dumps(changes,ensure_ascii=False),now()))
            db.execute("UPDATE channel_threads SET draft='',draft_head=NULL,context_revision=context_revision+1,paused=?,pause_reason=?,updated_at=? WHERE scope_key=?",
                       (paused,reason,now(),scope.key))
        return {'draft_cancelled':True,'paused':bool(paused),'sent':False}

    def corrections(self,spec,conversation_id):
        scope=self.scope(spec,conversation_id)
        with self.workspace.connect() as db:
            rows=db.execute('SELECT head,changes FROM channel_corrections WHERE scope_key=? ORDER BY override_id',(scope.key,)).fetchall()
        result={}
        for row in rows:result.setdefault(row['head'],{}).update(json.loads(row['changes']))
        return result

    def save_draft(self,spec,conversation_id,head,result):
        scope = self.scope(spec,conversation_id)
        with self.workspace.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute("SELECT MAX(sequence) FROM channel_messages WHERE scope_key=?",(scope.key,)).fetchone()[0]
            thread = db.execute("SELECT paused,context_revision FROM channel_threads WHERE scope_key=?",(scope.key,)).fetchone()
            if not thread or thread['paused'] or latest!=head or result.get('context_revision',0)!=thread['context_revision']:
                raise ValueError("会话已更新或已交人工，旧草稿已取消，请重新检查")
            db.execute("UPDATE channel_threads SET draft=?,draft_head=?,paused=?,pause_reason=?,handoff_head=? WHERE scope_key=?",
                       (json.dumps(result,ensure_ascii=False),head,int(result.get('action')=='handoff'),
                        'handoff' if result.get('action')=='handoff' else '',head if result.get('action')=='handoff' else None,scope.key))
            if result.get('action')=='handoff' and spec.platform in ('pinduoduo','taobao'):
                has_live=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_message_meta'").fetchone()
                original=db.execute('''SELECT m.message_id,m.content FROM channel_messages m JOIN live_message_meta l
                  ON m.scope_key=l.scope_key AND m.message_id=l.message_id WHERE m.scope_key=? AND m.sequence=?''',
                  (scope.key,head)).fetchone() if has_live else None
                if original:
                    event={'store_id':spec.store_id,'conversation_id':conversation_id,'buyer':conversation_id,
                        'platform':spec.platform,'scope_key':scope.key,'external_manual':True,
                        'category':result.get('category','handoff_after_sale'),'reason':result.get('reason','售后交人工'),
                        'summary':original['content'][:200],'draft_reply':result.get('draft_reply',''),
                        'delivery_channel':'live_channel','fingerprint':original['message_id'],
                        'message_id':original['message_id'],'head':head}
                    db.execute('INSERT OR IGNORE INTO channel_handoff_outbox(scope_key,head,payload) VALUES(?,?,?)',
                        (scope.key,head,json.dumps(event,ensure_ascii=False)))
        return result
