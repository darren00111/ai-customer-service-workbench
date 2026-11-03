"""Desktop/CLI workbench: real backend for offline demos and merchant onboarding."""
from __future__ import annotations

import copy
import json
import threading
from contextlib import nullcontext
from pathlib import Path
from ..config import SCRIPTS_DIR  # Ensure the merchant adapter is available for standalone callers.

from .data import SnapshotDataSource
from .dialogue import ReceptionService
from .knowledge import KnowledgeStore, now
from .sync import SyncManager
from .learning import LearningLoop
from .commerce import CommerceLearning, validate_sales
from ..platforms import platform_info
from .multichannel import WorkspaceData, SharedCatalog, ChannelInbox
from .gapcards import GapBoard
from .overrides import OverrideBook

FIXTURES = Path(__file__).with_name("fixtures.json")


class Workbench:
    def __init__(self, root: Path, stores_dir: Path, registry=None, models=None):
        self.root, self.stores_dir, self.registry = Path(root), Path(stores_dir), registry
        self.models = models
        self._sessions = {}
        self._lock = threading.RLock()
        self._workspace = WorkspaceData(self.root / "multichannel.sqlite3")
        self.shared = SharedCatalog(self._workspace)
        self.inbox = ChannelInbox(self._workspace)
        from ..channels.ledger import LiveLedger
        self.live = LiveLedger(self._workspace)
        from ..channels.history_context import HistoryContext
        self.history = HistoryContext(self._workspace)
        from ..channels.automation import ChannelAutomation
        self.automation = ChannelAutomation(self._workspace)
        self._pilots = {}
        self._channel_workers = {}
        self._closing_channels = set()
        self._handoff_errors = {}
        self.fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))

    def stores(self):
        rows = [{"store_id": key, "name": value["name"], "demo": True}
                for key, value in self.fixtures.items()]
        if self.registry:
            rows += [{"store_id": s.store_id, "name": s.name, "demo": False,
                      "platform": s.platform, "platform_name": platform_info(s.platform)['name'],
                      "merchant_id": s.merchant_id} for s in self.registry.load(enabled_only=False)]
        return rows

    def spec(self, store_id):
        if not self.registry or store_id in self.fixtures:
            raise ValueError("请先选择已创建的店铺，模拟店不参与跨店资料共享或会话导入")
        spec = next((row for row in self.registry.load(enabled_only=False) if row.store_id == store_id),None)
        if not spec:
            raise ValueError("未找到该店铺")
        self._workspace.bind(spec)
        return spec

    def knowledge(self, store_id):
        if store_id not in {row["store_id"] for row in self.stores()}:
            raise ValueError("店铺不存在，请先创建店铺")
        if store_id in self.fixtures:
            store = KnowledgeStore(self.root / "demo", store_id)
            fixture = self.fixtures[store_id]
            for kind, value in (("products", fixture["products"]), ("business", fixture["business"])):
                if not store.active(kind):
                    version = store.stage(kind, value, "内置模拟资料", demo=True)
                    store.activate(version, allow_demo=True)
            return store
        self.spec(store_id)
        return KnowledgeStore(self.root / "shops", store_id)

    def info(self, store_id):
        kb = self.knowledge(store_id)
        info = kb.readiness()
        info["examples"] = self.fixtures.get(store_id, {}).get("examples", [])
        model = self.models.status() if self.models else {"configured": False, "enabled": False, "connected": False}
        info["model"] = model
        info["model_connected"] = model["connected"]
        info["mode"] = ("千问语义理解 · " + model["model"]) if model["enabled"] else "离线规则验证"
        info["automatic_sending"] = False
        if store_id in self.fixtures:
            info['connection'] = {'name':'模拟店铺','status':'仅供试聊，不能连接真实消息','runtime_available':False}
            info['common_catalog'] = {'products':[], 'links':[]}
            info['channel_threads'] = []
        else:
            spec = self.spec(store_id)
            info['connection'] = platform_info(spec.platform)
            info['merchant_id'] = spec.merchant_id
            info['common_catalog'] = self.shared.list(spec)
            info['channel_threads'] = self.inbox.list(spec)
            info['pilot'] = self._pilots[store_id].status() if store_id in self._pilots else {
                'state': 'disconnected', 'bound': False, 'automatic_sending': False, 'production_verified': False}
            info['channel_monitor'] = self._channel_workers[store_id].status() if store_id in self._channel_workers else {'running':False}
            info['automatic_sending']=bool(info['channel_monitor'].get('running') and info['channel_monitor'].get('mode') in ('automatic','store'))
            if spec.platform in ('pinduoduo','taobao'):
                info['store_automation'] = self.store_automation_status(store_id)
        info["sync_sources"] = self.sync(store_id).list()
        info["feedback"] = LearningLoop(kb).list()
        info["interactions"] = LearningLoop(kb).interactions()
        info["tasks"] = kb.tasks()
        board = GapBoard(kb)
        info["cards"] = board.list(include_closed=True, limit=100)
        info["card_summary"] = board.summary()
        info["publish_history"] = kb.publish_history(20)
        info["overrides"] = OverrideBook(kb).list(limit=30)
        products=kb.active('products')
        info['catalog']=[{'product_id':p['product_id'],'title':p['title']} for p in products['payload']] if products else []
        from .retrieval import validity
        faq=kb.active('faq')
        info['merchant_knowledge']={'faq':[{'question':row['question'],'answer':row['answer']}
                                         for row in (faq['payload'] if faq else []) if validity(row)=='valid'][:50]}
        business = self.service(store_id,offline=True).source.business
        commerce = CommerceLearning(kb)
        service=self.service(store_id,offline=True)
        info['commerce'] = commerce.metrics(business,service.versions(),service.source.products)
        info['learning_insights'] = commerce.insights()
        with kb.connect() as db:
            metrics = db.execute("SELECT COUNT(*) AS total, COALESCE(SUM(json_extract(result,'$.automatic_resolution')=1),0) AS grounded_replies,"
                                 "COALESCE(SUM(json_extract(result,'$.action')='ask_followup'),0) AS followups,"
                                 "COALESCE(SUM(json_extract(result,'$.action')='handoff'),0) AS handoffs FROM interactions").fetchone()
        info["metrics"] = dict(metrics)
        return info

    def sync(self, store_id):
        return SyncManager(self.knowledge(store_id), store_id in self.fixtures)

    def service(self, store_id, knowledge=None, offline=False):
        kb = knowledge or self.knowledge(store_id)
        products, business = kb.active("products"), kb.active("business")
        from merchant import read_manifest
        manifest_path = self.stores_dir / store_id / "merchant.yaml"
        base_business = read_manifest(manifest_path)["business"] if manifest_path.exists() else {}
        source = SnapshotDataSource(store_id, business["payload"] if business else base_business,
                                    products["payload"] if products else [], self.fixtures.get(store_id, {}).get("orders"),
                                    demo=store_id in self.fixtures,
                                    provenance={"products": products or {}, "business": business or {}})
        interpreter = self.models.interpreter() if self.models and not offline else None
        return ReceptionService(source, kb, interpreter)

    def chat(self, store_id: str, text: str, session: str = "default", reset: bool = False,
             demo_order: bool = False):
        if len(session) > 100:
            raise ValueError("会话标识过长")
        with self._lock:
            kb = self.knowledge(store_id)
            self.sync(store_id).poll()
            demo = store_id in self.fixtures
            key = (store_id, session)
            state = {} if reset else copy.deepcopy(self._sessions.get(key) or kb.session_state(session))
            authorized = ["DEMO-ORDER-1"] if demo and demo_order and store_id == "demo_billiards" else []
            if authorized:
                state["order_id"] = authorized[0]
            elif not demo_order:
                state.pop("order_id", None)
            result = self.service(store_id).respond(text, state, authorized_order_ids=authorized, session_id=session)
            result['delivery_channel']='simulation'
            result["interaction_id"] = kb.save_turn(session, text, state, result)
            self._sessions[key] = result["state"]
            self.learning_refresh(store_id)
            if len(self._sessions) > 500:
                del self._sessions[next(iter(self._sessions))]
            return result

    def stage(self, store_id, kind, content, source, format="json"):
        if len(content) > 5_000_000:
            raise ValueError("单次导入限5MB，请拆分")
        kb = self.knowledge(store_id)
        demo = store_id in self.fixtures
        if kind == "products":
            revision = kb.import_products(content, format, source, demo)
        elif kind == "history":
            revision = kb.learn_history(content, source, demo)
        elif kind in {"faq", "business"}:
            revision = kb.stage(kind, json.loads(content), source, demo)
        else:
            raise ValueError("不支持的导入类型")
        return kb.preview(revision)

    def activate(self, store_id, revision: int, reviewed: bool, operator="店主"):
        if not reviewed:
            raise ValueError("请先预览并核对资料")
        with self._lock:
            kb = self.knowledge(store_id)
            kb.activate(int(revision), allow_demo=store_id in self.fixtures, reviewed=True, operator=operator)
            board = GapBoard(kb)
            board.on_published(int(revision), lambda view: self.service(store_id, view, offline=True))
            board.on_feedback_published(int(revision))
        return self.info(store_id)

    def rollback(self, store_id, kind, reason, target=None, operator="店主"):
        """Restore the previously live revision; unsent drafts built on the newer one become stale."""
        with self._lock:
            kb = self.knowledge(store_id)
            restored = kb.rollback(kind, reason, operator, target, allow_demo=store_id in self.fixtures)
            reopened = GapBoard(kb).on_rollback(kind, restored)
        return {"restored": restored, "reopened_cards": reopened, "history": kb.publish_history()}

    def publish_history(self, store_id):
        kb = self.knowledge(store_id)
        return {"history": kb.publish_history(),
                "rollback_targets": {kind: kb.rollback_target(kind) for kind in ("products", "business", "faq")}}

    # ----- problem cards ---------------------------------------------
    def cards(self, store_id, include_closed=True):
        board = GapBoard(self.knowledge(store_id))
        return {"cards": board.list(include_closed), "summary": board.summary()}

    def card(self, store_id, card_id):
        return GapBoard(self.knowledge(store_id)).get(card_id)

    def card_action(self, store_id, card_id, action, payload=None, operator="店主"):
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValueError("操作内容无效")
        with self._lock:
            board = GapBoard(self.knowledge(store_id))
            if action == "assign":
                return board.assign(card_id, payload.get("owner"), operator)
            if action in {"not_applicable", "dev"}:
                return board.close(card_id, action, payload.get("reason"), operator)
            if action == "merge":
                return board.merge(card_id, int(payload.get("target_id") or 0), operator)
            if action == "supplement":
                return board.supplement(card_id, payload.get("kind"), payload.get("content"), payload.get("source"), operator)
            if action == 'fill_form':
                card=board.get(card_id)
                value=payload.get('value')
                if not isinstance(value,str) or not 1<=len(value.strip())<=2000:
                    raise ValueError('请填写1–2000字已核对的内容')
                source=payload.get('source') or '商家通过问题卡片表单补充，待核对'
                if payload.get('form')=='attribute':
                    pid=card['product_id'] or payload.get('product_id')
                    active=self.knowledge(store_id).active('products')
                    product=next((p for p in (active['payload'] if active else []) if p['product_id']==pid),None)
                    if product is None:
                        raise ValueError('请选择本店已启用的商品')
                    if card.get('revision_id'):
                        previous=self.knowledge(store_id).preview(card['revision_id'])
                        if previous['kind']=='products' and not previous['active']:
                            if previous['base_revision']!=active['id']:
                                raise ValueError('正式商品资料已更新，请先重新核对原候选资料')
                            product=next((p for p in previous['payload'] if p['product_id']==pid),product)
                    field=payload.get('field')
                    if not isinstance(field,str) or not 1<=len(field.strip())<=60:
                        raise ValueError('请填写属性名称，例如材质或长度')
                    attributes=copy.deepcopy(product.get('attributes') or {})
                    if isinstance(attributes.get('value'),dict):attributes['value'][field.strip()]=value.strip()
                    else:attributes[field.strip()]=value.strip()
                    kind,content='products',{'upserts':[{'product_id':pid,'attributes':attributes}]}
                elif payload.get('form')=='faq':
                    question=payload.get('question') or card['latest_question']
                    if not isinstance(question,str) or not 1<=len(question.strip())<=2000:
                        raise ValueError('请填写标准问题')
                    entry={'question':question.strip(),'answer':value.strip()}
                    if card['product_id']:entry['product_ids']=[card['product_id']]
                    kind,content='faq',[entry]
                elif payload.get('form')=='shipping' and payload.get('field') in ('lead_time','origin'):
                    shipping={}
                    if card.get('revision_id'):
                        previous=self.knowledge(store_id).preview(card['revision_id'])
                        if previous['kind']=='business' and not previous['active']:
                            active=self.knowledge(store_id).active('business')
                            if previous['base_revision']!=(active['id'] if active else 0):
                                raise ValueError('正式店铺政策已更新，请先重新核对原候选资料')
                            shipping=copy.deepcopy(previous['payload'].get('shipping') or {})
                    shipping[payload['field']]=value.strip()
                    kind,content='business',{'shipping':shipping}
                else:raise ValueError('请选择商品属性、常见问答或发货规则')
                return board.supplement(card_id,kind,content,source,operator)
            if action == "link_existing":
                return board.link_existing(card_id, int(payload.get("faq_index", -1)), operator)
            if action == "map_product":
                return board.map_product(card_id, payload.get("product_id"), operator)
            if action == "correct_intent":
                return board.correct_intent(card_id, payload.get("expected_action", "reply"), payload.get("expected_contains", ""), operator)
            if action == "unlink":
                return board.unlink(card_id, operator)
            if action == "retest":
                use_model = bool(payload.get("use_model"))
                if use_model and (not self.models or not self.models.status()['enabled']):
                    raise ValueError('请先启用千问模型')
                variants = payload.get("variants") or []
                if isinstance(variants, str):
                    variants = json.loads(variants) if variants.strip() else []
                report = board.retest(card_id, lambda view: self.service(store_id, view, offline=not use_model), variants, operator)
                return {**board.get(card_id), "report": report}
        raise ValueError("不支持的卡片操作")

    # ----- human override --------------------------------------------
    def override(self, store_id, interaction_id, changes, reason, scope="session_only",
                 special_offer=False, conversation_id="", operator="店主"):
        with self._lock:
            kb = self.knowledge(store_id)
            book = OverrideBook(kb)
            with kb.connect() as db:
                original=db.execute('SELECT session_id,result FROM interactions WHERE id=?',(int(interaction_id),)).fetchone()
            if not original:raise ValueError('本店未找到该次接待记录')
            spec=self.spec(store_id)
            with self._workspace.connect() as db:
                linked=db.execute("SELECT conversation_id FROM channel_threads WHERE merchant_id=? AND platform=? AND store_id=? AND 'inbox-'||scope_key=?",
                    (spec.merchant_id,spec.platform,spec.store_id,original['session_id'])).fetchone()
            if conversation_id and (not linked or conversation_id!=linked['conversation_id']):
                raise ValueError('改判会话与原接待记录不一致，请刷新后选择正确记录')
            conversation_id=linked['conversation_id'] if linked else ''
            pilot=self._pilots.get(store_id)
            with pilot.lock if pilot and conversation_id else nullcontext():
                thread=self.inbox.get(spec,conversation_id) if conversation_id else None
                if thread:
                    latest=thread['messages'][-1]
                    saved=json.loads(original['result'])
                    current_draft=thread.get('draft') or {}
                    if not (saved.get('source_message_id')==latest['message_id'] or
                            current_draft.get('interaction_id')==int(interaction_id)):
                        raise ValueError('原接待记录已不是当前消息，请刷新后重新改判')
                record = book.record(interaction_id, changes, reason, scope, operator, special_offer, conversation_id)
                invalidated=self.inbox.record_correction(spec,conversation_id,latest['sequence'],record['override_id'],record['changes']) if thread else None
            session = record["session_id"]
            # 1. Correct the session state in place, keeping other confirmed conditions.
            state = copy.deepcopy(self._sessions.get((store_id, session)) or kb.session_state(session))
            if state:
                state = OverrideBook.apply_to_state(state, record["changes"])
                state["override_generation"] = int(state.get("override_generation", 0)) + 1
                self._sessions[(store_id, session)] = state
                with kb.connect() as db:
                    db.execute("UPDATE sessions SET state=?,updated_at=? WHERE session_id=?",
                               (json.dumps(state, ensure_ascii=False), now(), session))
            # 2. Cancel the old draft and any generation in flight for the platform thread.
            book.mark_invalidated(record["override_id"])
            record.update(draft_invalidation=invalidated or {"draft_cancelled": True, "sent": False},
                          manual_reply_preview=record["changes"].get("answer", ""),
                          hint="改判已保存，旧草稿已作废；修改后的回复需要预览并明确点击发送，保存改判不会发给顾客。")
            return record

    def overrides(self, store_id, session_id=None):
        return OverrideBook(self.knowledge(store_id)).list(session_id)

    def correct(self, store_id, interaction_id, correction, action="reply"):
        loop = LearningLoop(self.knowledge(store_id))
        feedback = loop.record(interaction_id, correction, action)
        return {"feedback_id": feedback}

    def propose_correction(self, store_id, feedback_id):
        return LearningLoop(self.knowledge(store_id)).propose(feedback_id, store_id in self.fixtures)

    def evaluate(self, store_id, revision=None, use_model=False):
        if use_model and (not self.models or not self.models.status()['enabled']):
            raise ValueError('请先启用千问模型')
        loop = LearningLoop(self.knowledge(store_id))
        return (loop.evaluate_revision(revision, lambda view: self.service(store_id, view, offline=not use_model)) if revision else
                loop.evaluate(self.service(store_id, offline=not use_model)))

    def learning_refresh(self, store_id):
        return CommerceLearning(self.knowledge(store_id)).refresh()

    def save_sales_policy(self, store_id, policy):
        value=validate_sales(policy)
        kb=self.knowledge(store_id)
        products=kb.active('products')
        ids={p['product_id'] for p in products['payload']} if products else set()
        if not set(value['preferred_product_ids'])<=ids:
            raise ValueError('优先商品必须来自本店已启用商品')
        if kb.active('business'):
            version=kb.stage_incremental('business',{'sales':value},'商家调整接待与学习策略',store_id in self.fixtures)
        else:
            business=copy.deepcopy(self.service(store_id,offline=True).source.business)
            business['sales']=value
            version=kb.stage('business',business,'商家调整接待与学习策略',store_id in self.fixtures,base_revision=0)
        kb.activate(version,allow_demo=store_id in self.fixtures,reviewed=True)
        return self.info(store_id)

    def import_outcomes(self,store_id,content):
        if not isinstance(content,str) or len(content)>5_000_000:
            raise ValueError('成交事件文件过大')
        return CommerceLearning(self.knowledge(store_id)).ingest(json.loads(content))

    def create_store(self, store_id, name, category="通用零售", platform="feige", merchant_id="local"):
        from merchant import default_manifest
        import yaml
        if not self.registry:
            raise ValueError("当前环境未配置客户注册表")
        if store_id.startswith("demo_") or len(name.strip()) not in range(1, 101):
            raise ValueError("请使用普通店铺标识，并填写1–100字店铺名称")
        if not isinstance(category, str) or not 1 <= len(category.strip()) <= 100:
            raise ValueError("请填写有效店铺品类")
        with self._lock:
            existing = self.registry.load(enabled_only=False)
            port = next(p for p in range(9222, 65536) if p not in {s.cdp_port for s in existing})
            platform_info(platform)
            manifest = default_manifest(store_id, name.strip(), port, platform, merchant_id)
            manifest["business"]["category"] = category.strip()
            manifest["business"]["reception"] = {"enabled": True}
            if any(s.store_id == store_id for s in existing):
                raise ValueError("该店铺标识已存在")
            target = self.stores_dir / store_id
            target.mkdir(parents=True, exist_ok=False)
            with (target / "merchant.yaml").open("x", encoding="utf-8") as file:
                yaml.safe_dump(manifest, file, allow_unicode=True, sort_keys=False)
            return self.info(store_id)

    def save_common_product(self,store_id,product_key,content,source,reviewed=False):
        if not isinstance(content,str) or len(content)>150_000:
            raise ValueError('共用商品资料过大')
        revision = self.shared.save(self.spec(store_id),product_key,json.loads(content),source,reviewed)
        return {'revision':revision}

    def apply_common_product(self,store_id,product_id,shared_revision,reviewed=False):
        return self.shared.stage_for_store(self.spec(store_id),self.knowledge(store_id),product_id,shared_revision,reviewed)

    def import_conversations(self,store_id,content):
        with self._lock:
            return self.inbox.import_conversations(self.spec(store_id),content)

    def imported_conversation(self,store_id,conversation_id):
        thread = self.inbox.get(self.spec(store_id),conversation_id)
        thread['live'] = self.live.is_live(self.spec(store_id), conversation_id)
        thread['deliveries']=self.live.history(self.spec(store_id),conversation_id) if thread['live'] else []
        if thread['live'] and thread['messages']:
            thread['observed_history'] = self.history.list(self.spec(store_id),conversation_id)
            _,refs=self.live.product_context(self.spec(store_id),conversation_id,thread['messages'][0]['sequence'],thread['messages'][-1]['sequence'])
            thread['product_references']=list(refs.values())
        if thread['draft'] and thread['draft'].get('knowledge_versions') != self.service(store_id,offline=True).versions():
            thread['draft'] = None
            thread['draft_stale'] = True
        return thread

    def generate_imported_draft(self,store_id,conversation_id):
        spec = self.spec(store_id)
        thread = self.inbox.get(spec,conversation_id)
        if thread['paused']:
            raise ValueError('此会话已暂停，请先在原平台核对人工处理情况')
        messages = thread['messages']
        if not messages or messages[-1]['role']!='buyer':
            raise ValueError('最后一条不是顾客消息，无需生成回复草稿')
        # Reconstruct slots without paying to re-run a model for each historical turn.
        # Observed agent messages remain context only, never product or policy facts.
        state = {}
        corrections=self.inbox.corrections(spec,conversation_id)
        offline = self.service(store_id,offline=True)
        is_live = self.live.is_live(spec, conversation_id)
        handled=self.live.handled_handoffs(spec,conversation_id) if is_live else set()
        if messages[-1]['message_id'] in handled:
            raise ValueError('本轮已记录人工处理，请等待顾客的新消息')
        if is_live:
            selected,prior,refs=self.history.bundle(spec,conversation_id,messages,
                offline.source.business.get('reception',{}).get('context_ttl_seconds',3600))
        else:
            selected,prior,refs=messages[-24:],[],{}
        history = [{'role':row['role'],'content':row['content'],**({'human_handled':True} if row['message_id'] in handled else {})} for row in selected]
        from .platform_context import product_history
        if prior:state['product_history']=product_history(offline.source.products,spec.platform,prior)
        current_reference=prior[-1] if prior else None
        def platform_reference(message):
            nonlocal current_reference
            if message['message_id'] in refs:current_reference=refs[message['message_id']]
            if current_reference:
                return {'platform':spec.platform,'remote_product_id':current_reference['product_id'],
                        'message_id':current_reference['message_id'],'latest_is_card':current_reference['message_id']==message['message_id']}
            return None
        for message in selected[:-1]:
            reference=platform_reference(message)
            if message['role']=='buyer':
                state = offline.respond(message['content'],state,track_gaps=False,platform_product=reference)['state']
            if message.get('sequence') in corrections:
                state=OverrideBook.apply_to_state(state,corrections[message['sequence']])
            if message['message_id'] in handled:
                state.pop('after_sale_open',None)
        state.pop('order_id',None)  # Imported customer text does not authorize an order query.
        state['messages'] = history[:-1]
        result = self.service(store_id).respond(history[-1]['content'],state,messages=history,live=is_live,
                                               authorized_order_ids=[],session_id='inbox-'+thread['scope_key'],platform_product=platform_reference(selected[-1]),
                                               correction=corrections.get(selected[-1].get('sequence')))
        if result.get('knowledge_versions') != self.service(store_id,offline=True).versions():
            raise ValueError('本店资料已更新，旧草稿已取消，请重新生成')
        result['draft_resolution'] = result.get('automatic_resolution',False)
        result['automatic_resolution'] = False
        result['delivery_channel'] = 'live_draft' if is_live else 'imported_draft'
        result['sent'] = False
        result['context_revision'] = thread['context_revision']
        result['source_message_id']=messages[-1]['message_id']
        result['interaction_id'] = self.knowledge(store_id).save_turn(
            'inbox-'+thread['scope_key'],history[-1]['content'],state,result,include_agent=False)
        return self.inbox.save_draft(spec,conversation_id,messages[-1]['sequence'],result)

    def _pilot(self, store_id):
        from ..channels.pinduoduo import PinduoduoPilot
        from ..channels.taobao import QianniuPilot
        spec = self.spec(store_id)
        if spec.platform not in ('pinduoduo', 'taobao'):
            raise ValueError('此连接用于淘宝和拼多多')
        with self._lock:
            if store_id not in self._pilots:
                self._pilots[store_id] = (QianniuPilot(spec, self.live) if spec.platform == 'taobao' else
                    PinduoduoPilot(spec, self.root.parent / 'profiles' / 'pinduoduo', self.live,
                                   on_history=lambda rows:self.stage_observed_history(store_id,rows)))
            return self._pilots[store_id]

    def stage_observed_history(self,store_id,rows):
        """Extract review candidates only from this store's observed records."""
        import hashlib
        from .platform_context import resolve_product
        spec=self.spec(store_id)
        kb=self.knowledge(store_id)
        active=kb.active('products')
        products=active['payload'] if active else []
        conversations={}
        for row in rows:
            conversations.setdefault(row.conversation_id,[]).append(row)
        revisions=[]
        for cid,batch in conversations.items():
            # A callback cannot promote arbitrary or imported text to a source.
            scope=self.inbox.scope(spec,cid)
            with self._workspace.connect() as db:
                binding=db.execute('SELECT account_key FROM live_accounts WHERE store_id=?',(store_id,)).fetchone()
                for row in batch:
                    stored=db.execute('SELECT role,content,timestamp,product_id,kind,account_key FROM observed_history WHERE scope_key=? AND message_id=?',
                                      (scope.key,row.message_id)).fetchone()
                    if not stored:
                        stored=db.execute('''SELECT m.role,m.content,l.timestamp,l.product_id,l.kind,l.account_key FROM channel_messages m JOIN live_message_meta l
                          ON m.scope_key=l.scope_key AND m.message_id=l.message_id WHERE m.scope_key=? AND m.message_id=?''',
                          (scope.key,row.message_id)).fetchone()
                    if not binding or not stored or stored['account_key']!=binding['account_key'] or any(stored[k]!=getattr(row,k) for k in ('role','content','timestamp','product_id','kind')):
                        raise ValueError('历史候选缺少本店已观察原文，未提炼')
            ordered=sorted(batch,key=lambda row:(row.timestamp,row.message_id))
            groups=[];group={'messages':[]}
            for row in ordered:
                if row.kind=='product_card':
                    if group['messages']:groups.append(group)
                    group={'messages':[]}
                    pid=resolve_product(products,spec.platform,row.product_id)
                    if pid:group['product_id']=pid
                elif row.kind in ('text','emotion'):
                    group['messages'].append({'role':row.role,'content':row.content})
                else:
                    if group['messages']:groups.append(group)
                    group={'messages':[]}
            if group['messages']:groups.append(group)
            if not any(a['role']=='buyer' and b['role']=='agent' for g in groups
                       for a,b in zip(g['messages'],g['messages'][1:])):
                continue
            content=json.dumps(groups,ensure_ascii=False,sort_keys=True)
            digest=hashlib.sha256(content.encode()).hexdigest()
            source='平台历史候选，需核对 · '+spec.platform+' · '+scope.key+' · '+digest
            with kb.connect() as db:
                old=db.execute("SELECT id FROM revisions WHERE kind='faq' AND source=? ORDER BY id DESC LIMIT 1",(source,)).fetchone()
            revision=old['id'] if old else kb.learn_history(content,source)
            revisions.append(revision)
        return revisions

    def pilot_command(self, store_id, action, account_key='', reviewed=False):
        if action == 'inspect_qianniu':
            if self.spec(store_id).platform != 'taobao':
                raise ValueError('请先选择淘宝店铺')
            from ..channels.taobao import inspect_qianniu
            return inspect_qianniu()
        if action not in ('open_login', 'inspect', 'bind', 'poll', 'stop'):
            raise ValueError('不支持的连接操作')
        pilot = self._pilot(store_id)
        if action in ('inspect','open_login','stop'):
            state = self.stop_channel_monitor(store_id)
            if state.get('stopping'):
                raise ValueError('上一轮接待正在结束，请稍候再检查或断开连接')
        if action == 'bind':
            return pilot.bind(account_key, reviewed)
        if action == 'stop':
            pilot.close()
            return pilot.status()
        result=getattr(pilot, action)()
        if action=='poll':self.reconcile_channel_deliveries(store_id)
        return result

    def capture_product_revision(self,store_id):
        pilot=self._pilot(store_id)
        if pilot.spec.platform!='pinduoduo' or not pilot.bound:
            raise ValueError('请先绑定拼多多店铺，再在其客服页面打开商品列表或商品详情')
        with pilot.lock:
            pilot.poll()
            if pilot.catalog.error:
                raise ValueError(pilot.catalog.error+'；请先重新采集，旧资料未启用')
            if pilot._identity().key!=pilot.account.key:
                pilot.close();raise ValueError('登录店铺已变化，商品采集已取消')
            rows=copy.deepcopy(list(pilot.catalog.products.values()))
            if not rows:
                raise ValueError(pilot.catalog.error or '还未采集到商品，请在已绑定的拼多多客服页面打开商品列表或详情，然后重试')
            kb=self.knowledge(store_id)
            active=kb.active('products')
            existing={r['product_id']:r for r in active['payload']} if active else {}
            for row in rows:
                row['attributes']={**existing.get(row['product_id'],{}).get('attributes',{}),**row['attributes']}
            source='拼多多商家页面采集，待核对 · 店铺'+pilot.account.mall_id
            # Repeated clicks on an unchanged snapshot reuse the same staged version.
            import hashlib
            digest=hashlib.sha256(json.dumps({'rows':rows,'base':active['id'] if active else 0},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            with self._workspace.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS captured_product_revisions(store_id TEXT,account_key TEXT,digest TEXT,revision INTEGER,PRIMARY KEY(store_id,account_key,digest))')
                prior=db.execute('SELECT revision FROM captured_product_revisions WHERE store_id=? AND account_key=? AND digest=?',
                                 (store_id,pilot.account.key,digest)).fetchone()
                revision=prior[0] if prior else kb.stage_incremental('products',rows,source)
                if not prior:
                    db.execute('INSERT INTO captured_product_revisions VALUES(?,?,?,?)',(store_id,pilot.account.key,digest,revision))
            return {'revision_id':revision,'captured_products':len(rows),'source':source,
                    'hint':'仅生成待核对版本；没有导入实时规格价格、库存、订单或物流，不会自动启用'}

    def send_live_draft(self, store_id, conversation_id, reviewed=False, automatic=False, cancelled=None, authorization_generation=None):
        if reviewed is not True:
            raise ValueError('请先核对当前顾客、草稿与店铺资料')
        pilot = self._pilot(store_id)
        thread = self.imported_conversation(store_id, conversation_id)
        draft = thread.get('draft')
        if not thread['live'] or not draft or draft.get('action') not in ('reply', 'ask_followup'):
            raise ValueError('此会话没有可发送的实时草稿，售后请在原平台处理')
        def validate():
            if authorization_generation is not None:
                self.require_store_automation(store_id,authorization_generation)
            if cancelled and cancelled():
                raise ValueError('接待已停止，本轮回复已取消')
            current = self.imported_conversation(store_id, conversation_id)
            if current.get('draft') != draft or current['paused']:
                raise ValueError('新消息、人工接管或资料更新使旧草稿失效，请重新生成')
        if automatic and (not draft.get('model_connected') or not draft.get('can_send')):
            raise ValueError('自动发送需要当前模型验证与事实校验通过')
        delivery = pilot.send_reviewed(conversation_id, thread['draft_head'], draft['draft_reply'], validate, automatic)
        if delivery['sent']:
            self.confirm_channel_delivery(store_id, draft, delivery, automatic)
        return delivery

    def confirm_channel_delivery(self, store_id, draft, delivery, automatic=False):
        if not delivery.get('sent') or delivery.get('state') != 'confirmed' or not draft.get('interaction_id'):
            return
        kb = self.knowledge(store_id)
        with kb.connect() as db:
            row = db.execute('SELECT session_id,question,input_state,result FROM interactions WHERE id=?', (draft['interaction_id'],)).fetchone()
            if not row:
                return
            result = json.loads(row['result'])
            if result.get('sent') and result.get('delivery_message_id') != delivery['remote_message_id']:
                raise ValueError('此回复的发送回执与已有记录不一致，请在原平台核对')
            # Retrying a confirmed receipt also repairs a crash between these two databases.
            if not result.get('sent'):
                result.update(sent=True, delivery_channel='live_confirmed' if automatic else 'live_reviewed',
                          automatic_resolution=bool(automatic and result.get('draft_resolution')),
                          delivery_message_id=delivery['remote_message_id'])
            db.execute('UPDATE interactions SET result=? WHERE id=?', (json.dumps(result,ensure_ascii=False),draft['interaction_id']))
            db.execute('INSERT OR IGNORE INTO conversation_events(session_id,reference,role,content,created_at) VALUES(?,?,?,?,?)',
                       (row['session_id'],'platform:'+delivery['remote_message_id'],'agent',result['draft_reply'],
                        now()))
        CommerceLearning(kb).record_reception(draft['interaction_id'],row['session_id'],result)
        observed=dict(result,state={'product_id':result.get('product_id'),
            'platform_product_reference':result.get('platform_product_reference')})
        GapBoard(kb).observe_interaction(draft['interaction_id'],row['session_id'],row['question'],json.loads(row['input_state']),observed)
        self.live.acknowledge_learning(delivery['token'])

    def reconcile_channel_deliveries(self,store_id):
        kb=self.knowledge(store_id)
        for receipt in self.live.unreconciled(self.spec(store_id)):
            with kb.connect() as db:
                row=db.execute('SELECT result FROM interactions WHERE id=?',(receipt['interaction_id'],)).fetchone()
            if row:
                draft=json.loads(row['result']);draft['interaction_id']=receipt['interaction_id']
                self.confirm_channel_delivery(store_id,draft,self.live.delivery(receipt['token']),bool(receipt['automatic']))

    def close_uncertain_delivery(self,store_id,conversation_id,token,reviewed=False):
        self.live.close_uncertain(self.spec(store_id),conversation_id,token,reviewed)
        if store_id in self._pilots:
            with self._pilots[store_id].lock:
                self._pilots[store_id].eligible_heads.pop(conversation_id,None)
        return {'hint':'已记录本轮人工处理；旧回复不会重发，也不计为确认发送。后续新咨询可继续接待；已有其他暂停原因的会话需另行核对恢复。'}

    def channel_handoff(self, store_id, conversation_id, draft):
        # Save_draft committed the original head and alert together. Publish from
        # that durable row, never from a newer message that arrived meanwhile.
        return self.reconcile_channel_handoffs(store_id)

    def reconcile_channel_handoffs(self,store_id):
        from ..notifications import HandoffOutbox
        spec=self.spec(store_id)
        with self._workspace.connect() as db:
            rows=db.execute('''SELECT o.scope_key,o.head,o.payload FROM channel_handoff_outbox o
              JOIN channel_threads t ON o.scope_key=t.scope_key WHERE o.published=0
              AND t.merchant_id=? AND t.platform=? AND t.store_id=? ORDER BY o.head LIMIT 100''',
              (spec.merchant_id,spec.platform,spec.store_id)).fetchall()
        completed=0
        self._handoff_errors.pop(store_id,None)
        for row in rows:
            try:
                HandoffOutbox(self.root.parent/'notifications.jsonl').publish(json.loads(row['payload']))
            except OSError:
                self._handoff_errors[store_id]='人工待办提醒尚未写入，后台会重试；该会话保持暂停'
                break
            with self._workspace.connect() as db:
                db.execute('UPDATE channel_handoff_outbox SET published=1 WHERE scope_key=? AND head=?',(row['scope_key'],row['head']))
            completed+=1
        return completed

    def complete_channel_handoff(self,event):
        store_id=event.get('store_id');cid=event.get('conversation_id')
        spec=self.spec(store_id)
        pilot=self._pilots.get(store_id)
        with pilot.lock if pilot else self._lock:
            resumed=self.live.complete_handoff(spec,cid,event)
            if pilot and pilot.eligible_heads.get(cid)==event.get('message_id'):
                pilot.eligible_heads.pop(cid,None)
        return resumed

    def _store_automation_checks(self,store_id):
        spec=self.spec(store_id)
        pilot=self._pilots.get(store_id)
        model=self.models.status() if self.models else {}
        reasons=[]
        if not pilot or not pilot.bound or not pilot.account:
            reasons.append('请先连接并核对当前店铺身份')
        elif spec.platform=='taobao' and not pilot.status().get('verified_message_metadata'):
            reasons.append('千牛逐条消息身份尚未通过客户端核实')
        elif spec.platform=='pinduoduo' and not pilot.status().get('observed_sockets'):
            reasons.append('尚未观察到当前店铺的消息连接')
        if not model.get('configured') or not model.get('enabled'):
            reasons.append('请先配置并启用千问模型')
        kb=self.knowledge(store_id)
        for kind,label in (('products','商品'),('business','店铺政策')):
            active=kb.active(kind)
            if not active or active['demo'] or (kind=='products' and not active['payload']):
                reasons.append('请先核对并启用本店'+label+'资料')
        receipt_token=''
        if pilot and pilot.bound and pilot.account and model.get('model'):
            versions=self.service(store_id,offline=True).versions()
            for receipt in self.automation.confirmed_tests(spec,pilot.account.key):
                with kb.connect() as db:
                    row=db.execute('SELECT result FROM interactions WHERE id=?',(receipt['interaction_id'],)).fetchone()
                result=json.loads(row['result']) if row else {}
                if (result.get('sent') and result.get('model_connected') and result.get('automatic_resolution')
                    and result.get('delivery_channel')=='live_confirmed'
                    and result.get('delivery_message_id')==receipt['remote_message_id']
                    and result.get('mode')=='qwen/'+model['model'] and result.get('knowledge_versions')==versions):
                    receipt_token=receipt['token'];break
        if not receipt_token:
            reasons.append('请先用当前模型和店铺资料完成一次指定测试顾客的自动收发，并取得发送确认')
        return reasons,receipt_token

    def store_automation_status(self,store_id):
        spec=self.spec(store_id)
        state=self.automation.status(spec)
        reasons,_=self._store_automation_checks(store_id)
        return {**state,'can_enable':not reasons,'requirements':reasons}

    def set_store_automation(self,store_id,enabled,reviewed=False):
        spec=self.spec(store_id)
        if type(enabled) is not bool or spec.platform not in ('pinduoduo','taobao'):
            raise ValueError('请选择淘宝或拼多多店铺')
        if enabled:
            if reviewed is not True:
                raise ValueError('请确认仅在当前已核对的店铺开启全店自动接待')
            pilot=self._pilot(store_id)
            with pilot.lock:
                pilot.poll()
                reasons,receipt=self._store_automation_checks(store_id)
                if reasons:raise ValueError('；'.join(reasons))
                if not self.models.status().get('connected'):
                    raise ValueError('当前模型尚未验证，请完成指定测试顾客收发后再开启')
                saved=self.automation.write(spec,True,pilot.account.key,self.models.status(),receipt)
        else:
            saved=self.automation.write(spec,False)
        # Generation changes invalidate in-flight work before stopping its worker.
        self.stop_channel_monitor(store_id)
        return saved

    def require_store_automation(self,store_id,generation=None):
        spec=self.spec(store_id)
        saved=self.automation.status(spec)
        pilot=self._pilots.get(store_id)
        if (not saved['enabled'] or (generation is not None and saved['generation']!=generation)
            or store_id in self._closing_channels):
            raise ValueError('本店全店自动接待已关闭或设置已变化，本轮回复取消')
        if not pilot or not pilot.bound or not pilot.account or saved['account_key']!=pilot.account.key:
            raise ValueError('请重新登录并核对已授权的同一店铺账号')
        if spec.platform=='taobao' and not pilot.status().get('verified_message_metadata'):
            raise ValueError('千牛逐条消息身份尚未核实，不能开启全店自动发送')
        model=self.models.status() if self.models else {}
        if not model.get('configured') or not model.get('enabled') or saved['model_signature']!=self.automation.signature(model):
            raise ValueError('模型配置已变化或停用，请完成测试后重新开启本店自动接待')
        for kind in ('products','business'):
            active=self.knowledge(store_id).active(kind)
            if not active or active['demo'] or (kind=='products' and not active['payload']):
                raise ValueError('本店已启用资料不完整，请先补全再开始接待')
        return saved

    def stop_store_reception(self,store_id):
        self._closing_channels.add(store_id)
        state=self.stop_channel_monitor(store_id)
        self._finish_channel_stop(store_id)
        return state

    def _finish_channel_stop(self,store_id):
        if store_id not in self._closing_channels:return
        worker=self._channel_workers.get(store_id)
        if worker and (worker.status()['running'] or worker.status()['stopping']):return
        if store_id in self._pilots:self._pilots[store_id].close()
        self._closing_channels.discard(store_id)

    def start_channel_monitor(self, store_id, mode='draft', conversations=None, reviewed=False):
        from ..channels.reception_worker import ChannelReceptionWorker
        if mode not in ('draft','automatic','store') or reviewed is not True:
            raise ValueError('请确认开始持续接待检查')
        pilot = self._pilot(store_id)
        self._finish_channel_stop(store_id)
        if store_id in self._closing_channels:
            raise ValueError('上一轮接待尚未结束，请稍候再启动')
        if not pilot.bound:
            raise ValueError('请先完成平台登录与店铺绑定')
        conversations = conversations or []
        if not isinstance(conversations,list) or len(conversations)>20 or any(not isinstance(v,str) or not 1<=len(v)<=200 for v in conversations):
            raise ValueError('请从本店已采集会话中选择测试顾客')
        if mode == 'automatic':
            if pilot.spec.platform == 'taobao' and not pilot.status().get('verified_message_metadata'):
                raise ValueError('千牛尚未核实逐条消息的作者与标识，请先完成客户端结构联调')
            if not conversations:
                raise ValueError('真实验收完成前，自动发送必须限定到明确的测试顾客')
            if not self.models or not self.models.status().get('enabled'):
                raise ValueError('请先启用并验证千问模型')
            for cid in conversations:
                if not self.live.is_live(self.spec(store_id),cid):
                    raise ValueError('测试顾客必须来自本店已连接的平台消息')
        authorization_generation=None
        if mode=='store':
            if conversations:raise ValueError('全店接待不使用测试顾客名单')
            authorization_generation=self.require_store_automation(store_id)['generation']
        with self._lock:
            if self.stop_channel_monitor(store_id).get('stopping'):
                raise ValueError('上一轮接待仍在结束，结束后才能重新启动')
            worker = ChannelReceptionWorker(self,store_id,mode,conversations,authorization_generation)
            self._channel_workers[store_id] = worker
            worker.start()
        return worker.status()

    def stop_channel_monitor(self,store_id):
        if store_id in self._channel_workers:
            self._channel_workers[store_id].stop()
            return self._channel_workers[store_id].status()
        return {'running':False,'stopping':False}

    def close_connections(self):
        for worker in tuple(self._channel_workers.values()):
            worker.stop()
        for pilot in tuple(self._pilots.values()):
            pilot.close()

    def channel_runtime(self):
        output = {}
        specs=self.registry.load(enabled_only=False) if self.registry else []
        for spec in specs:
            if spec.platform not in ('pinduoduo','taobao'):continue
            store_id=spec.store_id
            self.reconcile_channel_handoffs(store_id)
            self._finish_channel_stop(store_id)
            pilot=self._pilots.get(store_id)
            self._workspace.bind(spec)
            with self._workspace.connect() as db:
                counts = db.execute("""SELECT COUNT(*) AS total,COALESCE(SUM(d.automatic=1),0) AS automatic FROM live_deliveries d JOIN channel_threads t
                   ON d.scope_key=t.scope_key WHERE t.merchant_id=? AND t.platform=? AND t.store_id=?
                   AND d.state='confirmed'""",(spec.merchant_id,spec.platform,spec.store_id)).fetchone()
            output[store_id]={'connection':pilot.status() if pilot else {'state':'disconnected','bound':False},
                'confirmed_deliveries':counts['total'],'automatic_deliveries':counts['automatic'],
                'store_automation':self.automation.status(spec),
                'handoff_error':self._handoff_errors.get(store_id,''),
                'monitor':self._channel_workers[store_id].status() if store_id in self._channel_workers else {'running':False}}
        return output
