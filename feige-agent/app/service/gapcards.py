"""Problem cards (漏答卡片): classify, aggregate, retest, publish and observe.

A card is closed only by evidence: a linked candidate revision must pass a
retest of the original cases (and any boundary cases the merchant adds),
be published, and then answer the same problem in a later session.
Uploading a file, typing an answer or dismissing a reminder never completes
a card. Cards live in the per-store knowledge database, so they are isolated
by store exactly like the knowledge they describe.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import date

from .knowledge import now, redact
from .retrieval import normalize

CARD_TYPES = {
    "knowledge_missing": "知识缺失",
    "knowledge_error": "知识错误／过期",
    "product_link": "商品关联",
    "semantic": "语义误判",
    "platform_data": "平台数据缺失",
    "media": "媒体能力缺失",
}
STATUSES = {
    "open": "待处理",
    "assigned": "已分配／补充中",
    "retest": "待复测",
    "publish": "待发布",
    "observing": "观察中",
    "done": "已补全",
    "not_applicable": "不适用",
    "dev": "转开发",
    "merged": "已合并",
}
CLOSED = {"done", "not_applicable", "dev", "merged"}
# Customer-side conditions are asked in the conversation, not filed as cards.
CUSTOMER_SLOTS = {"destination", "budget", "use_date", "product", "order", "product_type",
                  "comparison_products", "variant"}
OPERATIONAL = {"model_connection"}
ORDER_INTENTS = {"order_status", "delivery", "dispatch"}
# Problems that a new standard answer must not paper over.
NO_SUPPLEMENT = {"semantic", "platform_data", "media"}
SUPPLEMENT_TYPES = {"knowledge_missing", "knowledge_error", "product_link"}


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def classify(result: dict) -> list[dict]:
    """Turn one reception result into zero or more card specifications."""
    missing = [m for m in result.get("missing") or [] if m not in OPERATIONAL]
    if not missing or result.get("model_error"):
        return []
    state = result.get("state") or {}
    product = result.get("product_id") or state.get("product_id") or ""
    intents = sorted(set(result.get("intents") or []))
    understanding = result.get("understanding") or {}
    specs = {}

    def add(card_type, fields, key_parts, extra=None):
        key = _key(card_type, *key_parts)
        spec = specs.setdefault(key, {"card_key": key, "card_type": card_type, "product_id": product,
                                      "fields": [], "intents": intents, **(extra or {})})
        spec["fields"] = sorted(set(spec["fields"]) | set(fields))

    for field in missing:
        if field == "platform_product":
            ref = state.get("platform_product_reference") or {}
            remote = "%s:%s" % (ref.get("platform", ""), ref.get("remote_product_id", ""))
            add("product_link", [field], ["", remote], {"product_id": "", "remote_product": remote})
        elif field == "data_source_unavailable" or (state.get("order_id") and set(intents) & ORDER_INTENTS
                                                    and field in {"status", "delivery_estimate", "dispatch_estimate"}):
            add("platform_data", [field], [product, field])
        elif field == "faq_expired":
            add("knowledge_error", [field], [product, normalize(result.get("_question", ""))],
                {"question_norm": normalize(result.get("_question", ""))})
        elif field == "intent":
            # An unclear message is a customer clarification, not a missing answer.
            if understanding.get("needs_clarification") and not (set(intents) - {"unknown"}):
                continue
            question = normalize(result.get("_question", ""))
            if not question:
                continue
            add("knowledge_missing", [field], [product, question, intents], {"question_norm": question})
        elif field in CUSTOMER_SLOTS:
            continue
        else:
            add("knowledge_missing", [field], [product, "fields"])
    # Field-level gaps of the same product share one card per type.
    merged = {}
    for spec in specs.values():
        if spec["card_type"] == "knowledge_missing" and "question_norm" not in spec:
            key = _key("knowledge_missing", spec["product_id"], "fields", spec["fields"])
            spec["card_key"] = key
        merged[spec["card_key"]] = spec
    return list(merged.values())


def suggestion(card_type, fields):
    if card_type == "product_link":
        return "修正商品映射：把平台商品编号关联到本店商品，或补齐该商品资料"
    if card_type == "knowledge_error":
        return "核对已过期或冲突的资料，发布新的有效版本"
    if card_type in NO_SUPPLEMENT:
        return {"semantic": "纠正意图并加入回归案例，交开发修正理解规则",
                "platform_data": "转开发排查平台读取或业务查询",
                "media": "转开发补齐图片／卡片识别能力"}[card_type]
    if fields == ["intent"]:
        return "补充常见问答，或关联已有问答的其他问法"
    kinds = set()
    for field in fields:
        if field.startswith(("attributes", "price", "stock", "variant", "products.")):
            kinds.add("商品资料")
        else:
            kinds.add("店铺政策")
    return "补充%s：%s" % ("、".join(sorted(kinds)), "、".join(fields))


def _customer_state(result):
    if result.get("action") == "handoff":
        return "已转人工"
    if result.get("answers"):
        return "已回答可确认部分，并说明尚缺资料"
    return "已告知顾客暂不能确认，未默认转人工"


def blocking_cards(db, revision_id):
    """Cards linked to a revision that has not passed retest against the current baseline."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gap_cards'").fetchone():
        return []
    blocked = []
    for row in db.execute("SELECT id,status,retest FROM gap_cards WHERE revision_id=? AND status IN ('assigned','retest','publish')",
                          (int(revision_id),)):
        if row["status"] != "publish":
            blocked.append(row["id"])
            continue
        report = json.loads(row["retest"] or "{}")
        kind = db.execute("SELECT kind FROM revisions WHERE id=?", (int(revision_id),)).fetchone()["kind"]
        baseline = {r["kind"]: r["id"] for r in db.execute("SELECT kind,id FROM revisions WHERE active=1") if r["kind"] != kind}
        if report.get("baseline") != baseline:
            db.execute("UPDATE gap_cards SET status='retest',note=?,updated_at=? WHERE id=?",
                       ("复测后其他资料已变化，需要重新复测", now(), row["id"]))
            blocked.append(row["id"])
    return blocked


class GapBoard:
    def __init__(self, kb):
        self.kb = kb
        with kb.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS gap_cards (
                id INTEGER PRIMARY KEY, card_key TEXT UNIQUE, card_type TEXT NOT NULL,
                product_id TEXT DEFAULT '', remote_product TEXT DEFAULT '', question_norm TEXT DEFAULT '',
                fields TEXT NOT NULL, intents TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                owner TEXT DEFAULT '', occurrences INTEGER DEFAULT 0, first_seen TEXT, last_seen TEXT,
                customer_state TEXT DEFAULT '', note TEXT DEFAULT '', revision_id INTEGER,
                retest TEXT, observation TEXT, published_at REAL, merged_into INTEGER,
                feedback_id INTEGER, reopened INTEGER DEFAULT 0, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS gap_evidence (
                id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL, interaction_id INTEGER,
                session_id TEXT, question TEXT, reply TEXT, input_state TEXT, platform_product TEXT,
                reference_date TEXT, channel TEXT, source TEXT DEFAULT 'reception', created_at TEXT,
                UNIQUE(card_id, interaction_id));
            CREATE TABLE IF NOT EXISTS gap_events (
                id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL, action TEXT NOT NULL,
                operator TEXT, detail TEXT, created_at TEXT);
            """)

    # ----- recording -------------------------------------------------
    def _event(self, db, card_id, action, operator="系统", detail=None):
        db.execute("INSERT INTO gap_events(card_id,action,operator,detail,created_at) VALUES(?,?,?,?,?)",
                   (card_id, action, operator, json.dumps(detail or {}, ensure_ascii=False), now()))

    def _upsert(self, db, spec, evidence, customer_state=""):
        row = db.execute("SELECT * FROM gap_cards WHERE card_key=?", (spec["card_key"],)).fetchone()
        stamp = now()
        if row and row["status"] == "merged" and row["merged_into"]:
            row = db.execute("SELECT * FROM gap_cards WHERE id=?", (row["merged_into"],)).fetchone()
        if not row:
            cursor = db.execute(
                "INSERT INTO gap_cards(card_key,card_type,product_id,remote_product,question_norm,fields,intents,status,"
                "occurrences,first_seen,last_seen,customer_state,note,updated_at) VALUES(?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                (spec["card_key"], spec["card_type"], spec.get("product_id") or "", spec.get("remote_product", ""),
                 spec.get("question_norm", ""), json.dumps(spec["fields"], ensure_ascii=False),
                 json.dumps(spec["intents"], ensure_ascii=False), "open", stamp, stamp, customer_state,
                 suggestion(spec["card_type"], spec["fields"]), stamp))
            card_id = cursor.lastrowid
            self._event(db, card_id, "created", detail={"fields": spec["fields"]})
        else:
            card_id = row["id"]
            intents = sorted(set(json.loads(row["intents"])) | set(spec["intents"]))
            status = row["status"]
            reopened = row["reopened"]
            if status in {"observing", "done"}:
                # The same problem after publishing: the fix did not hold.
                status, reopened = "open", reopened + 1
                self._event(db, card_id, "reopened", detail={"previous": row["status"]})
            db.execute("UPDATE gap_cards SET intents=?,status=?,reopened=?,last_seen=?,customer_state=?,updated_at=?,"
                       "note=CASE WHEN ?!=status THEN '发布后同类问题再次出现，需重新核对' ELSE note END WHERE id=?",
                       (json.dumps(intents, ensure_ascii=False), status, reopened, stamp,
                        customer_state or row["customer_state"], stamp, status, card_id))
        if evidence:
            inserted = db.execute(
                "INSERT OR IGNORE INTO gap_evidence(card_id,interaction_id,session_id,question,reply,input_state,platform_product,"
                "reference_date,channel,source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, evidence.get("interaction_id"), evidence.get("session_id", ""), redact(evidence.get("question", ""))[:2000],
                 redact(evidence.get("reply", ""))[:2000], json.dumps(evidence.get("input_state") or {}, ensure_ascii=False),
                 json.dumps(evidence.get("platform_product")) if evidence.get("platform_product") else None,
                 evidence.get("reference_date"), evidence.get("channel", ""), evidence.get("source", "reception"), stamp)).rowcount
            if inserted:
                db.execute("UPDATE gap_cards SET occurrences=occurrences+1 WHERE id=?", (card_id,))
        return card_id

    def observe_interaction(self, interaction_id, session_id, question, input_state, result):
        """Called for every saved reception turn: file new gaps, observe published fixes."""
        enriched = dict(result, _question=question)
        specs = classify(enriched)
        state = result.get("state") or {}
        ref = state.get("platform_product_reference")
        platform_product = ({"platform": ref["platform"], "remote_product_id": ref["remote_product_id"],
                             "message_id": ref["message_id"], "latest_is_card": True} if ref else None)
        compact = {k: v for k, v in (input_state or {}).items() if k not in {"messages", "last_turn"}}
        compact["messages"] = [{"role": m.get("role"), "content": str(m.get("content", ""))[:2000]}
                               for m in (input_state or {}).get("messages", [])[-8:]]
        evidence = {"interaction_id": interaction_id, "session_id": session_id, "question": question,
                    "reply": result.get("draft_reply", ""), "input_state": compact,
                    "platform_product": platform_product, "reference_date": result.get("reference_date"),
                    "channel": result.get("delivery_channel", "")}
        hit_keys = {spec["card_key"] for spec in specs}
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for spec in specs:
                self._upsert(db, spec, evidence, _customer_state(result))
            self._observe(db, interaction_id, session_id, question, result, hit_keys)

    def _observe(self, db, interaction_id, session_id, question, result, failed_keys):
        if result.get("model_error") or result.get("action") == "handoff" or not result.get('evidence'):
            return
        versions = result.get("knowledge_versions") or {}
        state = result.get("state") or {}
        missing = set(result.get("missing") or [])
        for card in db.execute("SELECT * FROM gap_cards WHERE status='observing'").fetchall():
            if card["card_key"] in failed_keys or card["revision_id"] not in versions.values():
                continue
            sessions = {r["session_id"] for r in db.execute("SELECT session_id FROM gap_evidence WHERE card_id=?", (card["id"],))}
            if session_id in sessions:
                continue  # A later turn of an original session is not a new session.
            fields = set(json.loads(card["fields"]))
            if card["card_type"] == "product_link":
                ref = state.get("platform_product_reference") or {}
                same = "%s:%s" % (ref.get("platform", ""), ref.get("remote_product_id", "")) == card["remote_product"]
                solved = same and bool(ref.get("local_product_id")) and "platform_product" not in missing
            elif card["question_norm"]:
                same = normalize(question) == card["question_norm"] or any(
                    normalize(r["question"]) == normalize(question)
                    for r in db.execute("SELECT question FROM gap_evidence WHERE card_id=?", (card["id"],)))
                solved = same and not (fields & missing) and "intent" not in missing
            else:
                same = (result.get("product_id") or "") == card["product_id"] and bool(
                    set(result.get("intents") or []) & set(json.loads(card["intents"])))
                solved = same and not (fields & missing)
            # An absent missing-field marker does not prove that field was asked
            # or answered. Require the matching leaf fact, not its parent object.
            paths={str(e.get('path','')) for e in result.get('evidence',[])}
            fact_fields=fields-{'intent','faq_expired','platform_product'}
            solved=solved and all(any(p==field or p.endswith('.'+field) for p in paths) for field in fact_fields)
            if not same or not solved:
                continue
            channel = result.get("delivery_channel", "")
            confirmed=bool(result.get('sent') and result.get('delivery_message_id') and
                           channel in {'live_confirmed','live_reviewed'})
            observation = {"interaction_id": interaction_id, "session_id": session_id, "channel": channel,
                           "real_reception": confirmed, "at": now(),
                           "answer": redact(result.get("draft_reply", ""))[:500]}
            db.execute("UPDATE gap_cards SET status=?,observation=?,note=?,updated_at=? WHERE id=?",
                       ('done' if confirmed else 'observing',json.dumps(observation, ensure_ascii=False),
                        "新会话已按发布后的资料回答并取得发送回显" if confirmed else "构造或草稿验证通过，尚非真实接待效果；等待新会话确认发送",
                        now(), card["id"]))
            self._event(db, card["id"], "observed", detail=observation)

    def file(self, card_type, question, product_id="", fields=None, session_id="", interaction_id=None,
             reply="", operator="店主", source="override"):
        """Create a card from a human correction or an unsupported media message."""
        if card_type not in CARD_TYPES:
            raise ValueError("问题卡片类型无效")
        fields = sorted(set(fields or ["intent"]))
        normalized = normalize(question)
        spec = {"card_key": _key(card_type, product_id or "", normalized, source), "card_type": card_type,
                "product_id": product_id or "", "fields": fields, "intents": [], "question_norm": normalized}
        state = {}
        if interaction_id:
            with self.kb.connect() as db:
                row = db.execute("SELECT input_state,result FROM interactions WHERE id=?", (int(interaction_id),)).fetchone()
            if row:
                state = json.loads(row["input_state"])
                spec["intents"] = json.loads(row["result"]).get("intents", [])
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            card_id = self._upsert(db, spec, {"interaction_id": interaction_id, "session_id": session_id,
                                              "question": question, "reply": reply, "input_state": state,
                                              "source": source}, "人工改判中")
            self._event(db, card_id, "filed", operator, {"source": source})
        return card_id

    # ----- reading ---------------------------------------------------
    def _present(self, db, row, with_evidence=False):
        value = dict(row)
        value["fields"] = json.loads(value["fields"])
        value["intents"] = json.loads(value["intents"])
        value["retest"] = json.loads(value["retest"] or "null")
        value["observation"] = json.loads(value["observation"] or "null")
        value["type_label"] = CARD_TYPES[value["card_type"]]
        value["status_label"] = STATUSES[value["status"]]
        value["privacy"] = "原文已脱敏（手机号、地址、编号、凭证）"
        value["allowed"] = self.allowed(value)
        sample = db.execute("SELECT question,reply,session_id,channel,created_at FROM gap_evidence WHERE card_id=? ORDER BY id DESC LIMIT ?",
                            (row["id"], 50 if with_evidence else 1)).fetchall()
        value["latest_question"] = sample[0]["question"] if sample else ""
        value["latest_reply"] = sample[0]["reply"] if sample else ""
        if with_evidence:
            value["evidence"] = [dict(r) for r in sample]
            value["events"] = [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in db.execute(
                "SELECT action,operator,detail,created_at FROM gap_events WHERE card_id=? ORDER BY id", (row["id"],))]
        return value

    @staticmethod
    def allowed(card):
        status, kind = card["status"], card["card_type"]
        if status in CLOSED:
            return []
        actions = ["assign", "not_applicable", "dev", "merge"]
        if kind in SUPPLEMENT_TYPES:
            actions.append("supplement")
        if kind == "knowledge_missing" and card.get("question_norm"):
            actions.append("link_existing")
        if kind == "product_link":
            actions.append("map_product")
        if kind in {"semantic", "knowledge_missing"}:
            actions.append("correct_intent")
        if card.get("revision_id") and status in {"retest", "publish", "assigned"}:
            actions += ["retest", "unlink"]
        return actions

    def list(self, include_closed=True, limit=200):
        clause = "" if include_closed else "WHERE status NOT IN ('done','not_applicable','dev','merged')"
        with self.kb.connect() as db:
            rows = db.execute("SELECT * FROM gap_cards %s ORDER BY CASE WHEN status IN ('done','not_applicable','dev','merged') THEN 1 ELSE 0 END,"
                              " occurrences DESC, id DESC LIMIT ?" % clause, (max(1, min(int(limit), 500)),)).fetchall()
            return [self._present(db, r) for r in rows]

    def get(self, card_id):
        with self.kb.connect() as db:
            row = db.execute("SELECT * FROM gap_cards WHERE id=?", (int(card_id),)).fetchone()
            if not row:
                raise ValueError("本店没有该问题卡片")
            return self._present(db, row, with_evidence=True)

    def summary(self):
        with self.kb.connect() as db:
            rows = db.execute("SELECT status, card_type, COUNT(*) n FROM gap_cards GROUP BY status, card_type").fetchall()
        by_status, by_type = {}, {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + r["n"]
            by_type[r["card_type"]] = by_type.get(r["card_type"], 0) + r["n"]
        # "不适用", "转开发" and "已补全" are counted separately, never added together.
        return {"by_status": by_status, "by_type": by_type,
                "open": sum(v for k, v in by_status.items() if k not in CLOSED),
                "done": by_status.get("done", 0), "not_applicable": by_status.get("not_applicable", 0),
                "dev": by_status.get("dev", 0)}

    # ----- operations ------------------------------------------------
    def _require(self, db, card_id, action):
        row = db.execute("SELECT * FROM gap_cards WHERE id=?", (int(card_id),)).fetchone()
        if not row:
            raise ValueError("本店没有该问题卡片")
        if action not in self.allowed(dict(row)):
            if row["status"] in CLOSED:
                raise ValueError("该卡片已%s，不能再操作" % STATUSES[row["status"]])
            if action == "supplement" and row["card_type"] in NO_SUPPLEMENT:
                raise ValueError("%s不能靠新增标准答案掩盖，请纠正意图或转开发排查" % CARD_TYPES[row["card_type"]])
            raise ValueError("该卡片当前不支持此操作")
        return row

    def assign(self, card_id, owner, operator="店主"):
        owner = str(owner or "").strip()[:60]
        if not owner:
            raise ValueError("请填写负责人")
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._require(db, card_id, "assign")
            status = "assigned" if row["status"] == "open" else row["status"]
            db.execute("UPDATE gap_cards SET owner=?,status=?,updated_at=? WHERE id=?", (owner, status, now(), row["id"]))
            self._event(db, row["id"], "assigned", operator, {"owner": owner})
        return self.get(card_id)

    def close(self, card_id, status, reason, operator="店主"):
        if status not in {"not_applicable", "dev"}:
            raise ValueError("只能标记为不适用或转开发")
        reason = str(reason or "").strip()[:500]
        if not reason:
            raise ValueError("请说明原因")
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._require(db, card_id, status)
            db.execute("UPDATE gap_cards SET status=?,note=?,updated_at=? WHERE id=?", (status, reason, now(), row["id"]))
            self._event(db, row["id"], status, operator, {"reason": reason})
        return self.get(card_id)

    def merge(self, source_id, target_id, operator="店主"):
        if int(source_id) == int(target_id):
            raise ValueError("不能与自身合并")
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = self._require(db, source_id, "merge")
            target = db.execute("SELECT * FROM gap_cards WHERE id=?", (int(target_id),)).fetchone()
            if not target or target["status"] in CLOSED:
                raise ValueError("目标卡片不存在或已关闭")
            if (source["card_type"], source["product_id"], source["remote_product"]) != (
                    target["card_type"], target["product_id"], target["remote_product"]):
                raise ValueError("不同类型、商品或平台商品的问题不能合并，即使问法相似")
            db.execute("UPDATE OR IGNORE gap_evidence SET card_id=? WHERE card_id=?", (target["id"], source["id"]))
            count = db.execute("SELECT COUNT(*) FROM gap_evidence WHERE card_id=?", (target["id"],)).fetchone()[0]
            fields = sorted(set(json.loads(source["fields"])) | set(json.loads(target["fields"])))
            intents = sorted(set(json.loads(source["intents"])) | set(json.loads(target["intents"])))
            db.execute("UPDATE gap_cards SET occurrences=?,fields=?,intents=?,last_seen=MAX(last_seen,?),updated_at=? WHERE id=?",
                       (count, json.dumps(fields, ensure_ascii=False), json.dumps(intents, ensure_ascii=False),
                        source["last_seen"], now(), target["id"]))
            db.execute("UPDATE gap_cards SET status='merged',merged_into=?,note=?,updated_at=? WHERE id=?",
                       (target["id"], "已合并到 #%s，证据保留" % target["id"], now(), source["id"]))
            self._event(db, source["id"], "merged", operator, {"into": target["id"]})
            self._event(db, target["id"], "merged_from", operator, {"from": source["id"]})
        return self.get(target_id)

    def _link(self, card_id, revision, operator, action, detail=None):
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM gap_cards WHERE id=?", (int(card_id),)).fetchone()
            rev = db.execute("SELECT active FROM revisions WHERE id=?", (int(revision),)).fetchone()
            if not rev or rev["active"]:
                raise ValueError("只能关联尚未发布的候选版本")
            db.execute("UPDATE gap_cards SET revision_id=?,status='retest',retest=NULL,note=?,updated_at=? WHERE id=?",
                       (int(revision), "资料已补充，尚未复测；补充资料本身不算补全", now(), row["id"]))
            self._event(db, row["id"], action, operator, {"revision": int(revision), **(detail or {})})
        return self.get(card_id)

    def supplement(self, card_id, kind, patch, source, operator="店主"):
        if kind not in {"products", "business", "faq"}:
            raise ValueError("请选择商品资料、店铺政策或常见问答")
        with self.kb.connect() as db:
            self._require(db, card_id, "supplement")
        if isinstance(patch, str):
            patch = json.loads(patch)
        revision = self.kb.stage_incremental(kind, patch, str(source or "").strip() or "问题卡片 #%s 补充资料" % card_id)
        return self._link(card_id, revision, operator, "supplemented", {"kind": kind})

    def link_existing(self, card_id, faq_index, operator="店主"):
        with self.kb.connect() as db:
            card = self._require(db, card_id, "link_existing")
            question = db.execute("SELECT question FROM gap_evidence WHERE card_id=? ORDER BY id LIMIT 1", (card["id"],)).fetchone()
        active = self.kb.active("faq")
        if not active or not 0 <= int(faq_index) < len(active["payload"]):
            raise ValueError("请选择本店已生效的问答")
        row = dict(active["payload"][int(faq_index)])
        if question and question["question"] not in row.get("aliases", []) and question["question"] != row["question"]:
            row["aliases"] = [*row.get("aliases", []), question["question"]]
        revision = self.kb.stage_incremental("faq", [row], "问题卡片 #%s 关联已有问答" % card_id)
        return self._link(card_id, revision, operator, "linked_existing", {"faq_index": int(faq_index)})

    def map_product(self, card_id, product_id, operator="店主"):
        with self.kb.connect() as db:
            card = self._require(db, card_id, "map_product")
        platform, _, remote = card["remote_product"].partition(":")
        products = self.kb.active("products")
        rows = products["payload"] if products else []
        product = next((p for p in rows if p["product_id"] == product_id), None)
        if not product:
            raise ValueError("请选择本店已生效的商品")
        owner = next((p["product_id"] for p in rows if (p.get("platform_ids") or {}).get(platform) == remote), None)
        if owner and owner != product_id:
            raise ValueError("该平台商品已关联到 %s，请先核对" % owner)
        row = {**product, "platform_ids": {**(product.get("platform_ids") or {}), platform: remote}}
        revision = self.kb.stage_incremental("products", {"upserts": [row]}, "问题卡片 #%s 修正商品映射" % card_id)
        return self._link(card_id, revision, operator, "mapped_product", {"product_id": product_id, "remote": card["remote_product"]})

    def correct_intent(self, card_id, expected_action, expected_contains, operator="店主"):
        from .learning import LearningLoop
        with self.kb.connect() as db:
            card = self._require(db, card_id, "correct_intent")
            evidence = db.execute("SELECT interaction_id FROM gap_evidence WHERE card_id=? AND interaction_id IS NOT NULL ORDER BY id LIMIT 1",
                                  (card["id"],)).fetchone()
        if not evidence:
            raise ValueError("该卡片没有可回放的接待记录")
        feedback = LearningLoop(self.kb).record(evidence["interaction_id"], expected_contains, expected_action)
        with self.kb.connect() as db:
            db.execute("UPDATE gap_cards SET card_type=CASE WHEN card_type='knowledge_missing' THEN 'semantic' ELSE card_type END,"
                       "status='dev',feedback_id=?,note=?,updated_at=? WHERE id=?",
                       (feedback, "已加入回归案例 #%s，待修正理解规则后复验" % feedback, now(), card["id"]))
            self._event(db, card["id"], "intent_corrected", operator, {"feedback": feedback, "expected_action": expected_action})
        return self.get(card_id)

    def unlink(self, card_id, operator="店主"):
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._require(db, card_id, "unlink")
            db.execute("UPDATE gap_cards SET revision_id=NULL,retest=NULL,status=?,note=?,updated_at=? WHERE id=?",
                       ("assigned" if row["owner"] else "open", "已解除候选版本关联", now(), row["id"]))
            self._event(db, row["id"], "unlinked", operator, {"revision": row["revision_id"]})
        return self.get(card_id)

    # ----- retest, publish, observe ---------------------------------
    def _cases(self, db, card, variants):
        cases = []
        for row in db.execute("SELECT * FROM gap_evidence WHERE card_id=? ORDER BY id LIMIT 20", (card["id"],)):
            if not row["question"]:
                continue
            cases.append({"question": row["question"], "state": json.loads(row["input_state"] or "{}"),
                          "platform_product": json.loads(row["platform_product"]) if row["platform_product"] else None,
                          "reference_date": row["reference_date"], "expect": "answered", "origin": "原问题"})
        base = cases[0] if cases else {"state": {}, "platform_product": None, "reference_date": None}
        for item in variants or []:
            if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip():
                raise ValueError("复测问法需要 question 文字")
            if item.get("expect", "answered") not in {"answered", "not_answered", "handoff"}:
                raise ValueError("复测预期只能是 answered、not_answered 或 handoff")
            state = dict(base["state"])
            if item.get("product_id") is not None:
                state["product_id"] = item["product_id"] or None
                if not item["product_id"]:
                    state.pop("product_id")
            cases.append({"question": item["question"].strip()[:2000], "state": state,
                          "platform_product": base["platform_product"] if item.get("same_card", True) else None,
                          "reference_date": base["reference_date"], "expect": item.get("expect", "answered"),
                          "must_contain": item.get("must_contain", ""), "must_not_contain": item.get("must_not_contain", ""),
                          "origin": "边界复测" if item.get("expect", "answered") != "answered" else "代表性变体"})
        return cases

    def retest(self, card_id, factory, variants=None, operator="店主", use_model=False):
        """Replay original cases and variants against the linked candidate revision only."""
        from .dialogue import ReceptionService
        from .learning import RevisionView
        with self.kb.connect() as db:
            card = self._require(db, card_id, "retest")
            cases = self._cases(db, card, variants)
        if not cases:
            raise ValueError("没有可复测的原问题")
        proposed = self.kb.preview(card["revision_id"])
        service = factory(RevisionView(self.kb, proposed))
        fields = set(json.loads(card["fields"]))
        results = []
        for case in cases:
            state = dict(case["state"])
            if state.get("last_activity"):
                state["last_activity"] = time.time()
            as_of = date.fromisoformat(case["reference_date"]) if case.get("reference_date") else service.today()
            replay = ReceptionService(service.source, service.knowledge, service.interpreter, as_of=as_of)
            result = replay.respond(case["question"], state, track_gaps=False, platform_product=case["platform_product"])
            missing = set(result.get("missing") or [])
            reply = result.get("draft_reply", "")
            if case["expect"] == "answered":
                passed = (not result.get("model_error") and result["action"] != "handoff" and not (fields & missing)
                          and "intent" not in missing and "platform_product" not in missing and bool(result.get("evidence")))
            elif case["expect"] == "handoff":
                passed = result["action"] == "handoff"
            else:
                # Boundary: the new knowledge must not answer a neighbouring question it does not cover.
                passed = not result.get("automatic_resolution")
            if case.get("must_contain") and case["must_contain"] not in reply:
                passed = False
            if case.get("must_not_contain") and case["must_not_contain"] in reply:
                passed = False
            results.append({"question": redact(case["question"]), "origin": case["origin"], "expect": case["expect"],
                            "passed": bool(passed), "action": result["action"], "missing": sorted(missing),
                            "answer": redact(reply)[:500], "evidence": [e.get("path") for e in result.get("evidence", [])][:8]})
        report = {"passed": all(r["passed"] for r in results), "cases": results, "revision": card["revision_id"],
                  "mode": "model_regression" if service.interpreter else "offline_regression",
                  "model": getattr(service.interpreter, "model", None) if service.interpreter else None,
                  "baseline": {r["kind"]: r["id"] for r in self.kb.revisions() if r["active"] and r["kind"] != proposed["kind"]},
                  "at": now()}
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE gap_cards SET retest=?,status=?,note=?,updated_at=? WHERE id=?",
                       (json.dumps(report, ensure_ascii=False), "publish" if report["passed"] else "retest",
                        "复测通过，可发布候选版本" if report["passed"] else "复测未通过，请修改资料后重新复测",
                        now(), card["id"]))
            self._event(db, card["id"], "retested", operator, {"passed": report["passed"], "mode": report["mode"]})
        return report

    def on_published(self, revision_id, factory):
        """After a publish: confirm live retrieval, then wait for a new session."""
        from .dialogue import ReceptionService
        with self.kb.connect() as db:
            cards = db.execute("SELECT * FROM gap_cards WHERE revision_id=? AND status='publish'", (int(revision_id),)).fetchall()
        changed = []
        for card in cards:
            service = factory(self.kb)
            fields = set(json.loads(card["fields"]))
            ok = True
            with self.kb.connect() as db:
                cases = [c for c in self._cases(db, card, None)]
            for case in cases[:5]:
                as_of = date.fromisoformat(case["reference_date"]) if case.get("reference_date") else service.today()
                state = dict(case["state"])
                if state.get("last_activity"):
                    state["last_activity"] = time.time()
                result = ReceptionService(service.source, service.knowledge, None, as_of=as_of).respond(
                    case["question"], state, track_gaps=False, platform_product=case["platform_product"])
                missing = set(result.get("missing") or [])
                ok = ok and not (fields & missing) and "intent" not in missing and "platform_product" not in missing
            with self.kb.connect() as db:
                db.execute("UPDATE gap_cards SET status=?,published_at=?,note=?,updated_at=? WHERE id=?",
                           ("observing" if ok else "retest", time.time() if ok else None,
                            "已发布，生效资料可检索；等待新的会话验证，没有新会话时保持观察" if ok else
                            "发布后生效资料检索未通过，请重新复测", now(), card["id"]))
                self._event(db, card["id"], "published" if ok else "publish_check_failed", detail={"revision": int(revision_id)})
            changed.append(card["id"])
        return changed

    def on_feedback_published(self, revision_id):
        """A correction revision that passed regression and was published covers matching question cards."""
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reports = [json.loads(r["evaluation"] or "{}") for r in db.execute(
                "SELECT evaluation FROM feedback WHERE revision_id=? AND status='active'", (int(revision_id),))]
            passed = {normalize(c["question"]) for r in reports if r.get("passed") for c in r.get("cases", []) if c.get("passed")}
            moved = []
            for card in db.execute("SELECT * FROM gap_cards WHERE status IN ('open','assigned') AND question_norm!=''").fetchall():
                questions = {normalize(r["question"]) for r in db.execute("SELECT question FROM gap_evidence WHERE card_id=?", (card["id"],))}
                if questions & passed:
                    report = next(r for r in reports if r.get("passed"))
                    db.execute("UPDATE gap_cards SET revision_id=?,retest=?,status='observing',published_at=?,note=?,updated_at=? WHERE id=?",
                               (int(revision_id), json.dumps({**report, "revision": int(revision_id)}, ensure_ascii=False), time.time(),
                                "纠错回归通过并发布；等待新的会话验证", now(), card["id"]))
                    self._event(db, card["id"], "published", detail={"revision": int(revision_id), "via": "correction_regression"})
                    moved.append(card["id"])
        return moved

    def on_rollback(self, kind, target):
        """A rollback below a card's revision removes the fix that card relied on."""
        with self.kb.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT c.id,c.owner FROM gap_cards c JOIN revisions r ON r.id=c.revision_id "
                              "WHERE r.kind=? AND c.revision_id>? AND c.status IN ('observing','done')",
                              (kind, int(target))).fetchall()
            for row in rows:
                db.execute("UPDATE gap_cards SET status=?,note=?,updated_at=? WHERE id=?",
                           ("assigned" if row["owner"] else "open", "关联知识版本已被回退，需要重新补充或复测", now(), row["id"]))
                self._event(db, row["id"], "rolled_back", detail={"kind": kind, "target": int(target)})
        return [r["id"] for r in rows]
