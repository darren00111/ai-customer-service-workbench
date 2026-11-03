"""Store-isolated, versioned knowledge with explicit activation and rollback."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(text: str) -> str:
    # History learning retains question patterns, not contact/order identifiers.
    text = re.sub(r"\bsk-[A-Za-z0-9_.-]{20,}", "[凭证]", text)
    text = re.sub(r"(?i)(?:api[_ -]?key|access[_ -]?token|authorization|cookie|密码|验证码)\s*[:=：]\s*[^\n；;]+", "[凭证]", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱]", text)
    text = re.sub(r"(?<!\d)\d{8,}(?!\d)", "[编号]", text)
    text = re.sub(r"(?:收货地址|详细地址|地址|收件人|姓名)\s*[:：]\s*[^\n；;]+", "[个人信息]", text)
    return text


class KnowledgeStore:
    def __init__(self, root: Path | str, store_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", store_id):
            raise ValueError("店铺标识无效")
        self.store_id = store_id
        self.path = Path(root) / store_id / "knowledge.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                source TEXT NOT NULL, created_at TEXT NOT NULL, digest TEXT NOT NULL,
                demo INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS active_revision ON revisions(kind) WHERE active=1;
            CREATE TABLE IF NOT EXISTS gaps (
                id INTEGER PRIMARY KEY, question TEXT UNIQUE, occurrences INTEGER DEFAULT 1,
                created_at TEXT, resolved INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversation_events (
                id INTEGER PRIMARY KEY, session_id TEXT, reference TEXT, role TEXT,
                content TEXT, created_at TEXT, UNIQUE(session_id,reference));
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY, session_id TEXT, question TEXT, input_state TEXT,
                result TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY, interaction_id INTEGER, correction TEXT,
                expected_action TEXT, revision_id INTEGER, status TEXT DEFAULT 'pending',
                evaluation TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS sync_sources (
                id INTEGER PRIMARY KEY, path TEXT, kind TEXT, format TEXT, mode TEXT,
                auto_publish INTEGER DEFAULT 0, interval_seconds INTEGER DEFAULT 60,
                digest TEXT DEFAULT '', last_revision INTEGER, last_error TEXT DEFAULT '',
                next_poll REAL DEFAULT 0, enabled INTEGER DEFAULT 1,
                UNIQUE(path,kind));
            CREATE TABLE IF NOT EXISTS knowledge_tasks (
                id INTEGER PRIMARY KEY, task_key TEXT UNIQUE, question TEXT,
                product_id TEXT, missing TEXT, intents TEXT, occurrences INTEGER DEFAULT 1,
                status TEXT DEFAULT 'open', note TEXT DEFAULT '', created_at TEXT);
            CREATE TABLE IF NOT EXISTS publish_log (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, from_revision INTEGER,
                to_revision INTEGER NOT NULL, action TEXT NOT NULL, operator TEXT NOT NULL,
                reason TEXT DEFAULT '', created_at TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(revisions)")}
            if 'evaluation_state' not in {row['name'] for row in db.execute('PRAGMA table_info(feedback)')}:
                db.execute("ALTER TABLE feedback ADD COLUMN evaluation_state TEXT DEFAULT ''")
            for column, declaration in (("base_revision", "INTEGER"), ("activated_at", "TEXT")):
                if column not in columns:
                    db.execute("ALTER TABLE revisions ADD COLUMN %s %s" % (column, declaration))
            if "lease_until" not in {row["name"] for row in db.execute("PRAGMA table_info(sync_sources)")}:
                db.execute("ALTER TABLE sync_sources ADD COLUMN lease_until REAL DEFAULT 0")
            if "trusted_outcomes" not in {row["name"] for row in db.execute("PRAGMA table_info(sync_sources)")}:
                db.execute("ALTER TABLE sync_sources ADD COLUMN trusted_outcomes INTEGER DEFAULT 0")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def stage(self, kind: str, payload, source: str, demo: bool = False, base_revision=None) -> int:
        if kind not in {"products", "faq", "business"}:
            raise ValueError("不支持的知识类型")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("请注明资料来源")
        if kind in {"products", "faq"}:
            if not isinstance(payload, list) or (kind == "faq" and not payload):
                raise ValueError("资料必须是列表，问答不能为空")
            ids = []
            for row in payload:
                if not isinstance(row, dict):
                    raise ValueError("资料行必须是对象")
                if kind == "products":
                    pid = row.get("product_id")
                    if (not isinstance(pid, str) or not pid.strip() or
                            not isinstance(row.get("title"), str) or not row["title"].strip()):
                        raise ValueError("商品需要 product_id 和 title")
                    ids.append(pid)
                    if 'platform_ids' in row:
                        mappings=row['platform_ids']
                        if (not isinstance(mappings,dict) or set(mappings)-{'pinduoduo','taobao','feige','xiaohongshu'} or
                            any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}',v) for v in mappings.values())):
                            raise ValueError('platform_ids 必须是平台与本店商品标识的映射')
                    if not isinstance(row.get("aliases", []), list) or any(
                            not isinstance(a, str) or not a.strip() for a in row.get("aliases", [])):
                        raise ValueError("aliases 必须是列表")
                    if "attributes" in row and not isinstance(row["attributes"], dict):
                        raise ValueError("attributes 必须是属性对象")
                    if "variants" in row and (not isinstance(row["variants"], list) or
                                              any(not isinstance(v, dict) for v in row["variants"])):
                        raise ValueError("variants 必须是规格对象列表")
                elif not isinstance(row.get("question"), str) or not isinstance(row.get("answer"), str):
                    raise ValueError("问答需要 question 和 answer 文字")
                elif not row["question"].strip() or not row["answer"].strip():
                    raise ValueError("问题与答案不能为空")
                if kind == "faq" and (not isinstance(row.get("aliases", []), list) or any(
                        not isinstance(a, str) or not a.strip() for a in row.get("aliases", []))):
                    raise ValueError("问答 aliases 必须是文字列表")
                if kind == "faq" and (not isinstance(row.get("product_ids", []), list) or any(
                        not isinstance(p, str) or not p for p in row.get("product_ids", []))):
                    raise ValueError("问答 product_ids 必须是商品标识列表")
            if len(ids) != len(set(ids)):
                raise ValueError("商品标识重复")
        elif not isinstance(payload, dict):
            raise ValueError("店铺政策必须是对象")
        if kind == "business":
            if "sales" in payload:
                from .commerce import validate_sales
                validate_sales(payload["sales"])
            for section in ("shipping", "policies", "promotions", "reception"):
                if section in payload and not isinstance(payload[section], dict):
                    raise ValueError("店铺政策 %s 必须是对象" % section)
            shipping = payload.get("shipping", {})
            if "delivery_by_region" in shipping and not isinstance(shipping["delivery_by_region"], dict):
                raise ValueError("delivery_by_region 必须是地区与时效的映射")
        packed = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(packed) > 5_000_000:
            raise ValueError("单次导入资料过大，请拆分")
        with self.connect() as db:
            cursor = db.execute("INSERT INTO revisions(kind,payload,source,created_at,digest,demo,base_revision) VALUES(?,?,?,?,?,?,?)",
                                (kind, packed, source, now(), hashlib.sha256(packed.encode()).hexdigest(), int(demo), base_revision))
            return cursor.lastrowid

    def activate(self, revision_id: int, allow_demo: bool = False, reviewed: bool = False,
                 operator: str = "店主", reason: str = "", rollback: bool = False) -> None:
        """Atomically switch the active revision of one kind and audit the switch.

        A rollback may only restore a revision that has been active before; it
        re-uses that earlier review instead of a regression baseline that has
        since moved, and it is recorded separately from a normal publish.
        """
        operator = (str(operator or "店主").strip() or "店主")[:60]
        reason = str(reason or "").strip()[:500]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM revisions WHERE id=?", (revision_id,)).fetchone()
            if row is None:
                raise ValueError("没有找到本店的知识版本")
            if row["demo"] and not allow_demo:
                raise ValueError("模拟资料只能在演示环境中启用")
            current = db.execute("SELECT id FROM revisions WHERE kind=? AND active=1", (row["kind"],)).fetchone()
            if current and current["id"] == row["id"]:
                return  # Re-activating the live revision is a no-op, not a new publish.
            if rollback:
                if not row["activated_at"]:
                    raise ValueError("只能回退到曾经生效的版本；新版本请走核对与复测后发布")
                if not reason:
                    raise ValueError("回退需要填写原因，便于以后追溯")
            if row["kind"] == "faq" and any(item.get("review_required") for item in json.loads(row["payload"])) and not reviewed:
                raise ValueError("历史问答需要核对全部候选内容后确认启用")
            if row["base_revision"] is not None and not row["activated_at"] and row["base_revision"] != (current["id"] if current else 0):
                raise ValueError("资料已被其他更新改变，请重新生成增量版本")
            from .gapcards import blocking_cards
            blocked = [] if rollback else blocking_cards(db, revision_id)
            if blocked:
                raise ValueError("问题卡片 %s 关联此版本但尚未通过复测，请先复测或解除关联" % "、".join("#%s" % i for i in blocked))
            for feedback in ([] if rollback else db.execute("SELECT evaluation FROM feedback WHERE revision_id=? AND status!='retired'", (revision_id,))):
                evaluation = json.loads(feedback["evaluation"] or "{}")
                if not evaluation.get("passed"):
                    raise ValueError("纠错版本尚未通过回归验证")
                baseline = {r["kind"]: r["id"] for r in db.execute("SELECT kind,id FROM revisions WHERE active=1")
                            if r["kind"] != row["kind"]}
                if evaluation.get("baseline") != baseline:
                    raise ValueError("回归后其他资料已变化，请重新验证纠错版本")
            db.execute("UPDATE revisions SET active=0 WHERE kind=?", (row["kind"],))
            db.execute("UPDATE revisions SET active=1,activated_at=? WHERE id=?", (now(), revision_id))
            db.execute("INSERT INTO publish_log(kind,from_revision,to_revision,action,operator,reason,created_at) VALUES(?,?,?,?,?,?,?)",
                       (row["kind"], current["id"] if current else None, revision_id,
                        "rollback" if rollback else "publish", operator, reason, now()))
            db.execute("UPDATE feedback SET status='active' WHERE revision_id=? AND status!='retired'", (revision_id,))
            for feedback in db.execute("SELECT evaluation FROM feedback WHERE revision_id=? AND status!='retired'", (revision_id,)):
                for case in json.loads(feedback["evaluation"] or "{}").get("cases", []):
                    if case.get("passed"):
                        db.execute("UPDATE knowledge_tasks SET status='resolved',note='纠错回归通过并启用' "
                                   "WHERE question=? AND COALESCE(product_id,'')=?", (case["question"], case.get("product_id") or ""))
                        db.execute("UPDATE gaps SET resolved=1 WHERE question=? AND NOT EXISTS "
                                   "(SELECT 1 FROM knowledge_tasks WHERE question=? AND status='open')", (case["question"], case["question"]))

    def active(self, kind: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM revisions WHERE kind=? AND active=1", (kind,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def publish_history(self, limit=50) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM publish_log ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),))]

    def rollback_target(self, kind: str) -> int | None:
        """The revision that was live immediately before the current one."""
        current = self.active(kind)
        if not current:
            return None
        with self.connect() as db:
            row = db.execute("SELECT from_revision FROM publish_log WHERE kind=? AND to_revision=? AND from_revision IS NOT NULL "
                             "ORDER BY id DESC LIMIT 1", (kind, current["id"])).fetchone()
            if row:
                return row["from_revision"]
            # Revisions activated before the audit log existed.
            row = db.execute("SELECT id FROM revisions WHERE kind=? AND activated_at IS NOT NULL AND id<? ORDER BY id DESC LIMIT 1",
                             (kind, current["id"])).fetchone()
        return row["id"] if row else None

    def rollback(self, kind: str, reason: str, operator: str = "店主", target: int | None = None, allow_demo: bool = False) -> int:
        target = int(target) if target else self.rollback_target(kind)
        if not target:
            raise ValueError("没有可回退的上一生效版本")
        if self.preview(target)["kind"] != kind:
            raise ValueError("回退目标与资料类型不一致")
        self.activate(target, allow_demo=allow_demo, reviewed=True, operator=operator, reason=reason, rollback=True)
        return target

    def revisions(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,kind,source,created_at,digest,demo,active,base_revision,activated_at FROM revisions ORDER BY id DESC")]

    def stage_incremental(self, kind: str, patch, source: str, demo=False) -> int:
        import copy
        from .retrieval import normalize
        active = self.active(kind)
        original = copy.deepcopy(active["payload"] if active else ({} if kind == "business" else []))
        if kind == "products":
            upserts = patch.get("upserts", []) if isinstance(patch, dict) else patch
            deletions = patch.get("delete_ids", []) if isinstance(patch, dict) else []
            if not isinstance(upserts, list) or not isinstance(deletions, list) or any(not isinstance(p, str) for p in deletions):
                raise ValueError("增量商品需提供 upserts 列表和 delete_ids 列表")
            merged = {row["product_id"]: row for row in original}
            seen = set()
            for row in upserts:
                if not isinstance(row, dict) or not isinstance(row.get("product_id"), str):
                    raise ValueError("增量商品缺少 product_id")
                pid = row["product_id"]
                if pid in seen or pid in deletions:
                    raise ValueError("增量商品包含重复或冲突标识")
                seen.add(pid)
                merged[pid] = {**merged.get(pid, {}), **row}
            for pid in deletions:
                merged.pop(pid, None)
            payload = list(merged.values())
        elif kind == "business":
            if not isinstance(patch, dict):
                raise ValueError("店铺增量政策必须是对象")
            def merge(left, right):
                for k, v in right.items():
                    if isinstance(left.get(k), dict) and isinstance(v, dict):
                        merge(left[k], v)
                    else:
                        left[k] = v
            merge(original, patch)
            payload = original
        elif kind == "faq":
            if not isinstance(patch, list):
                raise ValueError("增量问答必须是列表")
            def key(row):
                return (normalize(row["question"]), tuple(sorted(row.get("product_ids", []))))
            merged = {key(row): row for row in original}
            for row in patch:
                if not isinstance(row, dict) or not isinstance(row.get("question"), str):
                    raise ValueError("增量问答缺少 question")
                merged[key(row)] = row
            payload = list(merged.values())
        else:
            raise ValueError("不支持的增量类型")
        return self.stage(kind, payload, source, demo, active["id"] if active else 0)

    def save_turn(self, session_id, question, input_state, result, include_agent=True) -> int:
        last = result["state"].get("last_turn", {})
        with self.connect() as db:
            for role, item in last.items():
                if role == "agent" and not include_agent:
                    continue
                db.execute("INSERT OR IGNORE INTO conversation_events(session_id,reference,role,content,created_at) VALUES(?,?,?,?,?)",
                           (session_id, item["reference"], role, item["content"], now()))
            db.execute("INSERT INTO sessions(session_id,state,updated_at) VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
                       (session_id, json.dumps(result["state"], ensure_ascii=False), now()))
            saved = {k: v for k, v in result.items() if k != "state"}
            if result['state'].get('platform_product_reference'):
                saved['platform_product_reference']=result['state']['platform_product_reference']
            compact = {k: v for k, v in input_state.items() if k not in {"messages", "last_turn"}}
            compact["messages"] = [{"role": m.get("role"), "content": str(m.get("content", ""))[:2000]}
                                   for m in input_state.get("messages", [])[-8:]]
            cursor = db.execute("INSERT INTO interactions(session_id,question,input_state,result,created_at) VALUES(?,?,?,?,?)",
                                (session_id, question, json.dumps(compact, ensure_ascii=False), json.dumps(saved, ensure_ascii=False), now()))
            interaction_id = cursor.lastrowid
        from .gapcards import GapBoard
        GapBoard(self).observe_interaction(interaction_id, session_id, question, input_state, result)
        if result.get("sales",{}).get("enabled"):
            from .commerce import CommerceLearning
            CommerceLearning(self).record_reception(interaction_id, session_id, result)
        return interaction_id

    def session_state(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT state FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row["state"]) if row else {}

    def transcript(self, session_id, before=None):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM conversation_events WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 100",
                              (session_id, int(before or 2**62))).fetchall()
        return [dict(row) for row in reversed(rows)]

    def resolve_gap(self, gap_id):
        with self.connect() as db:
            db.execute("UPDATE gaps SET resolved=1 WHERE id=?", (int(gap_id),))

    def preview(self, revision_id: int) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise ValueError("版本不存在")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def import_products(self, content: str, format: str, source: str, demo: bool = False) -> int:
        if format == "csv":
            rows = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
            for row in rows:
                row["aliases"] = [v.strip() for v in (row.get("aliases") or "").split("|") if v.strip()]
                for key in ("price", "stock", "attributes", "variants"):
                    if row.get(key):
                        try:
                            row[key] = json.loads(row[key])
                        except ValueError as exc:
                            raise ValueError("CSV 的 %s 需要合法 JSON 值" % key) from exc
                    else:
                        row.pop(key, None)
        elif format == "json":
            rows = json.loads(content)
        else:
            raise ValueError("仅支持 CSV 或 JSON")
        return self.stage("products", rows, source, demo)

    def learn_history(self, content: str, source: str, demo: bool = False) -> int:
        """Extract adjacent Q/A candidates. No automatic promotion of human claims."""
        conversations = json.loads(content)
        if not isinstance(conversations, list):
            raise ValueError("历史记录应是会话列表")
        candidates, seen = [], set()
        for conversation_index, conversation in enumerate(conversations):
            messages = conversation.get("messages", [])
            groups = []
            for index, message in enumerate(messages):
                role = message.get("role")
                role = "buyer" if role in {"buyer", "user"} else "agent" if role in {"agent", "assistant"} else "system"
                if groups and groups[-1]["role"] == role:
                    groups[-1]["content"].append(str(message.get("content", "")))
                    groups[-1]["end"] = index
                else:
                    groups.append({"role": role, "content": [str(message.get("content", ""))], "start": index, "end": index})
            for previous, current in zip(groups, groups[1:]):
                if previous["role"] != "buyer" or current["role"] != "agent":
                    continue
                question = redact("\n".join(previous["content"]))
                answer = redact("\n".join(current["content"]))
                if question and answer and question not in seen:
                    seen.add(question)
                    candidate = {"question": question, "answer": answer, "review_required": True, "source_type": "history",
                                 "history_reference": {"conversation": conversation_index, "message_start": previous["start"], "message_end": current["end"]}}
                    if isinstance(conversation.get("product_id"), str):
                        candidate["product_ids"] = [conversation["product_id"]]
                    candidate["requires_context"] = bool(re.search(r"这个|这根|这件|那个|前一个|上一款", question))
                    candidates.append(candidate)
        if not candidates:
            raise ValueError("没有找到可供审核的连续问答")
        return self.stage("faq", candidates, source, demo)

    def gap(self, question: str, product_id=None, missing=None, intents=None) -> None:
        with self.connect() as db:
            cleaned = redact(question)[:500]
            db.execute("INSERT INTO gaps(question,created_at) VALUES(?,?) ON CONFLICT(question) "
                       "DO UPDATE SET occurrences=occurrences+1", (cleaned, now()))
            if missing:
                key = hashlib.sha256(json.dumps([cleaned, product_id, sorted(missing)], ensure_ascii=False).encode()).hexdigest()
                db.execute("INSERT INTO knowledge_tasks(task_key,question,product_id,missing,intents,created_at) VALUES(?,?,?,?,?,?) "
                           "ON CONFLICT(task_key) DO UPDATE SET occurrences=occurrences+1,status='open'",
                           (key, cleaned, product_id, json.dumps(missing, ensure_ascii=False), json.dumps(intents or []), now()))

    def tasks(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM knowledge_tasks WHERE status='open' ORDER BY occurrences DESC LIMIT 100").fetchall()
        return [{**dict(row), "missing": json.loads(row["missing"]), "intents": json.loads(row["intents"])} for row in rows]

    def resolve_task(self, task_id, note="资料已补充并核对"):
        with self.connect() as db:
            db.execute("UPDATE knowledge_tasks SET status='resolved',note=? WHERE id=?", (str(note)[:500], int(task_id)))

    def gaps(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM gaps WHERE resolved=0 ORDER BY occurrences DESC LIMIT 100")]

    def readiness(self) -> dict:
        products = self.active("products")
        rows = products["payload"] if products else []
        missing = [{"product_id": p["product_id"], "fields": [k for k in ("price", "stock", "attributes")
                    if k not in p or p[k] is None or p[k] == ""]} for p in rows]
        return {"store_id": self.store_id, "products": len(rows), "demo": bool(products and products["demo"]),
                "missing": [m for m in missing if m["fields"]], "model_connected": False,
                "data_sources": {"products": "本地已确认资料" if rows else "未导入",
                                 "orders": "未连接", "logistics": "未连接"},
                "revisions": self.revisions(), "gaps": self.gaps()}
