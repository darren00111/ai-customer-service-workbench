"""Continuous, per-store receive/understand/recheck/send loop.

Automatic sends are limited to explicitly selected acceptance conversations
until that platform/store has completed its live acceptance.
"""
import threading
import time


class ChannelReceptionWorker:
    def __init__(self, workbench, store_id, mode='draft', conversations=(), authorization_generation=None):
        if mode=='store' and (type(authorization_generation) is not int or authorization_generation<1):
            raise ValueError('全店接待缺少当前店铺的开启记录')
        self.workbench, self.store_id = workbench, store_id
        self.mode, self.conversations = mode, frozenset(conversations)
        self.authorization_generation=authorization_generation
        self.stop_event = threading.Event()
        self.thread = None
        self.processed = {}
        self.pending = {}
        self.last_error = ''
        self.last_tick = None
        self.generated = self.confirmed = 0
        self.rounds = self.handoffs = 0
        self.lock = threading.RLock()

    def status(self):
        alive = bool(self.thread and self.thread.is_alive())
        return {'running': alive and not self.stop_event.is_set(), 'stopping': alive and self.stop_event.is_set(),
                'mode': self.mode, 'test_conversations': sorted(self.conversations),
                'scope':'store' if self.mode=='store' else 'selected_test' if self.mode=='automatic' else 'draft',
                'generated': self.generated, 'confirmed': self.confirmed,
                'rounds':self.rounds,'handoffs':self.handoffs,
                'pending_confirmations': len(self.pending), 'error': self.last_error,
                'last_tick': self.last_tick, 'production_verified': False}

    def tick(self):
        with self.lock:
            if self.stop_event.is_set():
                return
            w, store = self.workbench, self.store_id
            if self.mode=='store':w.require_store_automation(store,self.authorization_generation)
            pilot = w._pilot(store)
            pilot.poll()
            w.reconcile_channel_deliveries(store)
            w.reconcile_channel_handoffs(store)
            self.last_tick = time.time()
            self.rounds += 1
            for token, pending in tuple(self.pending.items()):
                delivery = w.live.delivery(token)
                if delivery['sent']:
                    w.confirm_channel_delivery(store, pending['draft'], delivery, True)
                    self.confirmed += 1
                    del self.pending[token]
                elif delivery['state']=='manually_closed':
                    del self.pending[token]
                elif time.monotonic() - pending['started'] > 15:
                    w.live.finish(token, 'delivery_unknown')
                    self.last_error = '有回复未取得回显，请在原平台核对；该会话不会重复发送'
                    del self.pending[token]
            spec = w.spec(store)
            knowledge_version=tuple(sorted(w.service(store,offline=True).versions().items()))
            self.processed={cid:revision for cid,revision in self.processed.items() if cid in pilot.eligible_heads}
            # The UI lists only the most recent 100 threads. Reception must cover
            # every observed pending customer, including those beyond that page.
            for cid in tuple(pilot.eligible_heads):
                if self.stop_event.is_set():
                    break
                head = w.live.head(spec, cid)
                if not head or head['paused'] or head['role']!='buyer' or head.get('action')!='reply':continue
                revision = (head.get('sequence'),head.get('context_revision',0),knowledge_version)
                if not head or self.processed.get(cid) == revision:
                    continue
                # One failed turn is not retried silently and cannot loop up API cost.
                self.processed[cid] = revision
                try:
                    if self.mode=='store':w.require_store_automation(store,self.authorization_generation)
                    draft = w.generate_imported_draft(store, cid)
                    self.generated += 1
                    if self.stop_event.is_set():
                        break
                    if draft.get('action') == 'handoff':
                        self.handoffs += 1
                        w.channel_handoff(store, cid, draft)
                        continue
                    if self.mode not in ('automatic','store') or (self.mode=='automatic' and cid not in self.conversations):
                        continue
                    if not draft.get('model_connected') or not draft.get('can_send'):
                        self.last_error = '模型未通过当前轮验证，本轮只保留草稿'
                        continue
                    if draft.get('action') not in ('reply', 'ask_followup') or (draft['action'] == 'reply' and not draft.get('draft_resolution')):
                        continue
                    options={'authorization_generation':self.authorization_generation} if self.mode=='store' else {}
                    delivery = w.send_live_draft(store, cid, True, automatic=True, cancelled=self.stop_event.is_set,**options)
                    if delivery['sent']:
                        self.confirmed += 1
                    elif delivery['state'] == 'awaiting_echo':
                        self.pending[delivery['token']] = {'draft': draft, 'started': time.monotonic()}
                    else:
                        self.last_error = delivery['hint']
                except (ValueError, OSError) as exc:
                    self.last_error = str(exc)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def run():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except (ValueError,OSError) as exc:
                    self.last_error=str(exc)
                    self.stop_event.set()
                    break
                except Exception:
                    self.last_error = '连接或消息读取中断，请检查平台登录并重新连接'
                    self.stop_event.set()
                    break
                self.stop_event.wait(2)
        self.thread = threading.Thread(target=run, name='reception-'+self.store_id, daemon=True)
        self.thread.start()

    def stop(self, wait=2):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=wait)
        return not bool(self.thread and self.thread.is_alive())
