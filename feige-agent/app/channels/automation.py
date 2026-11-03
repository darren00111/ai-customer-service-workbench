"""Store-wide permission is scoped and revocable; it is not an acceptance certificate."""
import json

from ..service.knowledge import now


class ChannelAutomation:
    def __init__(self,workspace):
        self.workspace=workspace
        with workspace.connect() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS channel_automation (
                merchant_id TEXT,platform TEXT,store_id TEXT,account_key TEXT,
                enabled INTEGER DEFAULT 0,generation INTEGER DEFAULT 0,
                model_signature TEXT,receipt_token TEXT,updated_at TEXT,
                PRIMARY KEY(merchant_id,platform,store_id));
              CREATE TABLE IF NOT EXISTS channel_automation_audit (
                id INTEGER PRIMARY KEY,merchant_id TEXT,platform TEXT,store_id TEXT,
                account_key TEXT,enabled INTEGER,generation INTEGER,created_at TEXT);
            ''')

    def status(self,spec):
        with self.workspace.connect() as db:
            row=db.execute('SELECT * FROM channel_automation WHERE merchant_id=? AND platform=? AND store_id=?',
                (spec.merchant_id,spec.platform,spec.store_id)).fetchone()
        return {**(dict(row) if row else {'enabled':False,'generation':0,'account_key':'','model_signature':''}),
                'enabled':bool(row and row['enabled']),'production_verified':False}

    @staticmethod
    def signature(model):
        return json.dumps([model.get('provider','qwen'),model.get('endpoint',''),model.get('model','')],sort_keys=True)

    def confirmed_tests(self,spec,account_key):
        with self.workspace.connect() as db:
            return [dict(r) for r in db.execute('''SELECT d.* FROM live_deliveries d
              JOIN channel_threads t ON d.scope_key=t.scope_key
              WHERE t.merchant_id=? AND t.platform=? AND t.store_id=? AND d.account_key=?
              AND d.state='confirmed' AND d.automatic=1 AND d.learning_recorded=1
              AND d.interaction_id IS NOT NULL ORDER BY d.created_at DESC LIMIT 100''',
              (spec.merchant_id,spec.platform,spec.store_id,account_key))]

    def write(self,spec,enabled,account_key='',model=None,receipt_token=''):
        if spec.platform not in ('pinduoduo','taobao') or type(enabled) is not bool:
            raise ValueError('本店自动接待配置无效')
        with self.workspace.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if enabled:
                from .ledger import LiveLedger
                LiveLedger._check_account(db,spec,account_key)
                receipt=db.execute('''SELECT d.token FROM live_deliveries d JOIN channel_threads t ON d.scope_key=t.scope_key
                  WHERE d.token=? AND d.account_key=? AND d.state='confirmed' AND d.automatic=1 AND d.learning_recorded=1
                  AND t.merchant_id=? AND t.platform=? AND t.store_id=?''',
                  (receipt_token,account_key,spec.merchant_id,spec.platform,spec.store_id)).fetchone()
                if not receipt:raise ValueError('本店尚未取得指定测试顾客自动收发的确认结果')
            old=db.execute('SELECT generation FROM channel_automation WHERE merchant_id=? AND platform=? AND store_id=?',
                (spec.merchant_id,spec.platform,spec.store_id)).fetchone()
            generation=(old['generation'] if old else 0)+1
            db.execute('''INSERT INTO channel_automation VALUES(?,?,?,?,?,?,?,?,?)
              ON CONFLICT(merchant_id,platform,store_id) DO UPDATE SET account_key=excluded.account_key,
              enabled=excluded.enabled,generation=excluded.generation,model_signature=excluded.model_signature,
              receipt_token=excluded.receipt_token,updated_at=excluded.updated_at''',
              (spec.merchant_id,spec.platform,spec.store_id,account_key,int(enabled),generation,
               self.signature(model or {}),receipt_token,now()))
            db.execute('INSERT INTO channel_automation_audit(merchant_id,platform,store_id,account_key,enabled,generation,created_at) VALUES(?,?,?,?,?,?,?)',
                (spec.merchant_id,spec.platform,spec.store_id,account_key,int(enabled),generation,now()))
        return self.status(spec)
