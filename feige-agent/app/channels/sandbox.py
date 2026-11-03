"""Persistent, network-free event transport for acceptance, never a live adapter.

Uses the same reception service, inbox, draft gates and delivery ledger as the
product. The transport and its acknowledgements are explicitly synthetic.
"""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

from ..config import StoreRegistry
from ..service.workbench import Workbench
from .pinduoduo import Incoming


class Sandbox:
    def __init__(self, root):
        self.root = Path(root) / 'platform-sandbox-v1'
        self.workbench = Workbench(self.root / 'runtime', self.root / 'shops',
            StoreRegistry(self.root / 'registry.json', self.root / 'shops'))
        with self.workbench._workspace.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS sandbox_queue (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, store_id TEXT, packet TEXT,
                state TEXT DEFAULT 'pending', error TEXT DEFAULT '');
              CREATE TABLE IF NOT EXISTS sandbox_echoes (
                token TEXT PRIMARY KEY,store_id TEXT,conversation_id TEXT,text TEXT,
                remote_id TEXT,timestamp INTEGER);
            ''')

    def add_store(self, store_id, platform='pinduoduo', category='demo_billiards', price_delta=0):
        if not store_id.startswith('sandbox_'):
            raise ValueError('验收店铺需要 sandbox_ 前缀')
        if category not in self.workbench.fixtures:
            raise ValueError('未知的合成资料品类')
        if not any(s.store_id == store_id for s in self.workbench.registry.load(enabled_only=False)):
            self.workbench.create_store(store_id, '模拟验收店 ' + store_id, platform=platform,
                                        merchant_id='sandbox_merchant')
            kb = self.workbench.knowledge(store_id)
            fixture = copy.deepcopy(self.workbench.fixtures[category])
            for product in fixture['products']:
                if type(product.get('price')) in (int, float):
                    product['price'] += price_delta
            for kind in ('products', 'business'):
                rev = kb.stage(kind, fixture[kind], '合成验收资料', demo=True)
                kb.activate(rev, allow_demo=True, reviewed=True)
        spec = self.workbench.spec(store_id)
        self.workbench.live.bind(spec, 'sandbox:' + store_id)
        self.workbench.live.recover(spec)
        return spec

    @staticmethod
    def packet(message_id, text, conversation_id='same_buyer', role='buyer', timestamp=1800000000000,
               kind='text', product_id=''):
        return dict(message_id=message_id, content=text, conversation_id=conversation_id,
                    role=role, timestamp=timestamp, kind=kind, product_id=product_id)

    def enqueue(self, store_id, packet, event_key=None):
        self.workbench.spec(store_id)
        encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 16000:
            raise ValueError('模拟事件过大')
        event_key = event_key or uuid.uuid4().hex
        with self.workbench._workspace.connect() as db:
            existing = db.execute('SELECT store_id,packet,id FROM sandbox_queue WHERE event_key=?', (event_key,)).fetchone()
            if existing:
                if existing['store_id'] != store_id or existing['packet'] != encoded:
                    raise ValueError('同一事件标识的内容发生变化')
                return existing['id']
            cursor = db.execute('INSERT INTO sandbox_queue(event_key,store_id,packet) VALUES(?,?,?)',
                                (event_key, store_id, encoded))
            return cursor.lastrowid

    def drain(self):
        with self.workbench._workspace.connect() as db:
            pending = [dict(row) for row in db.execute("SELECT * FROM sandbox_queue WHERE state='pending' ORDER BY id")]
        results = []
        for row in pending:
            try:
                packet = json.loads(row['packet'])
                spec = self.workbench.spec(row['store_id'])
                kind = packet.get('kind', 'text')
                if kind not in ('text', 'product_card', 'emotion'):
                    packet['kind'] = 'control' if kind == 'withdraw' else 'unsupported'
                    packet['product_id'] = ''
                    packet['action'] = 'pause'
                else:
                    packet['action'] = 'reply' if kind in ('text', 'emotion') else 'context'
                event = Incoming(**packet)
                count = self.workbench.live.ingest(spec, 'sandbox:' + spec.store_id, event)
                state, error = ('received' if count else 'duplicate'), ''
            except (ValueError, TypeError, KeyError) as exc:
                state, error = 'rejected', str(exc)
            with self.workbench._workspace.connect() as db:
                db.execute('UPDATE sandbox_queue SET state=?,error=? WHERE id=?', (state,error,row['id']))
            results.append({'id':row['id'], 'state':state, 'error':error})
        return results

    def draft(self, store_id, conversation_id='same_buyer'):
        spec = self.workbench.spec(store_id)
        head = self.workbench.live.head(spec, conversation_id)
        if not head or head['paused'] or head['role'] != 'buyer' or head['action'] != 'reply':
            raise ValueError('该模拟会话不能自动回复，请检查消息或人工状态')
        result = self.workbench.chat(store_id, head['content'], session='sandbox-' + head['scope_key'])
        result.update(context_revision=head['context_revision'], delivery_channel='simulation', sent=False)
        self.workbench.inbox.save_draft(spec, conversation_id, head['sequence'], result)
        return result

    def send(self, store_id, conversation_id='same_buyer', fault='none'):
        if fault not in ('none','timeout','delayed','crash'):
            raise ValueError('未知的模拟故障')
        spec = self.workbench.spec(store_id)
        thread = self.workbench.inbox.get(spec, conversation_id)
        draft = thread['draft'] or {}
        if draft.get('knowledge_versions') != self.workbench.service(store_id, offline=True).versions():
            raise ValueError('知识已更新，旧草稿已取消')
        token = self.workbench.live.reserve(spec, 'sandbox:' + store_id, conversation_id,
            thread['draft_head'], draft.get('draft_reply',''))
        remote = 'synthetic-' + token
        stamp = self.workbench.live.head(spec,conversation_id)
        with self.workbench._workspace.connect() as db:
            timestamp = db.execute('SELECT MAX(timestamp) FROM live_message_meta WHERE scope_key=?', (stamp['scope_key'],)).fetchone()[0]
            db.execute('INSERT INTO sandbox_echoes VALUES(?,?,?,?,?,?)',
                (token,store_id,conversation_id,draft['draft_reply'],remote,timestamp+1))
        if fault == 'timeout':
            self.workbench.live.finish(token, 'delivery_unknown')
        elif fault != 'crash':
            self.workbench.live.finish(token, 'awaiting_echo', remote)
            if fault == 'none':
                self.echo(token)
        return self.workbench.live.delivery(token)

    def echo(self, token, correct=True):
        with self.workbench._workspace.connect() as db:
            row = db.execute('SELECT * FROM sandbox_echoes WHERE token=?', (token,)).fetchone()
        if not row:
            raise ValueError('未找到模拟回显')
        spec = self.workbench.spec(row['store_id'])
        event = Incoming(row['conversation_id'], row['remote_id'] if correct else 'wrong-' + row['remote_id'],
                         'agent', row['text'], row['timestamp'], 'context')
        self.workbench.live.ingest(spec, 'sandbox:' + spec.store_id, event)
        return self.workbench.live.delivery(token)

    def summary(self):
        with self.workbench._workspace.connect() as db:
            states = {r['state']: r['n'] for r in db.execute('SELECT state,COUNT(*) AS n FROM sandbox_queue GROUP BY state')}
            deliveries = {r['state']: r['n'] for r in db.execute('SELECT state,COUNT(*) AS n FROM live_deliveries GROUP BY state')}
        return {'kind':'offline_synthetic_transport', 'platform_compatibility_verified':False,
                'actual_model_calls':False, 'customer_messages_sent':False,
                'events':states, 'simulated_deliveries':deliveries}
