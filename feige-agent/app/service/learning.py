"""Human corrections become reviewable knowledge and repeatable regression cases."""
from __future__ import annotations

import json
import time
import re
from datetime import date
from .knowledge import now, redact


class RevisionView:
    """Read a proposed revision without temporarily changing the live knowledge."""
    def __init__(self, kb, revision):
        self.kb, self.revision, self.store_id = kb, revision, kb.store_id

    def active(self, kind):
        return self.revision if kind == self.revision["kind"] else self.kb.active(kind)

    def revisions(self):
        return [dict(row, active=int(row["id"] == self.revision["id"]) if row["kind"] == self.revision["kind"] else row["active"])
                for row in self.kb.revisions()]


class LearningLoop:
    def __init__(self, kb):
        self.kb = kb

    def interactions(self, limit=30):
        with self.kb.connect() as db:
            rows = db.execute("SELECT id,session_id,question,result,created_at FROM interactions ORDER BY id DESC LIMIT ?", (min(int(limit), 100),)).fetchall()
        return [{**dict(row), "result": json.loads(row["result"])} for row in rows]

    def record(self, interaction_id, correction, expected_action="reply"):
        if not isinstance(correction, str) or not 1 <= len(correction.strip()) <= 2000:
            raise ValueError("请填写1–2000字的正确答复或应补问内容")
        if expected_action not in {"reply", "ask_followup", "handoff"}:
            raise ValueError("期望处理方式无效")
        with self.kb.connect() as db:
            row = db.execute("SELECT id FROM interactions WHERE id=?", (int(interaction_id),)).fetchone()
            if not row:
                raise ValueError("本店未找到该次接待记录")
            cursor = db.execute("INSERT INTO feedback(interaction_id,correction,expected_action,created_at) VALUES(?,?,?,?)",
                                (row["id"], redact(correction.strip()), expected_action, now()))
            return cursor.lastrowid

    def list(self):
        with self.kb.connect() as db:
            rows = db.execute("SELECT f.*,i.question FROM feedback f JOIN interactions i ON i.id=f.interaction_id ORDER BY f.id DESC LIMIT 100").fetchall()
        return [{**dict(row), "evaluation": json.loads(row["evaluation"] or "{}")} for row in rows]

    def retire(self, feedback_id):
        with self.kb.connect() as db:
            db.execute("UPDATE feedback SET status='retired' WHERE id=?", (int(feedback_id),))

    def propose(self, feedback_id, demo=False):
        with self.kb.connect() as db:
            row = db.execute("SELECT f.*,i.question,i.result FROM feedback f JOIN interactions i ON i.id=f.interaction_id WHERE f.id=?", (int(feedback_id),)).fetchone()
        if not row:
            raise ValueError("纠错记录不存在")
        if row['revision_id']:
            previous=self.kb.preview(row['revision_id'])
            current=self.kb.active('faq')
            if previous['activated_at'] or previous['base_revision']==(current['id'] if current else 0):
                return previous
        result = json.loads(row["result"])
        unsupported_details = (result.get('action')=='ask_followup' and not result.get('evidence') and
                               set(result.get('intents',[]))<={'unknown','faq','details'})
        if row["expected_action"] != "reply" or (not unsupported_details and any(i not in {"unknown", "faq"} for i in result.get("intents", []))):
            raise ValueError("这条纠错应更新商品或政策资料、分流规则；已保留为回归案例，不用FAQ覆盖业务事实")
        item = {"question": redact(row["question"]), "answer": row["correction"], "review_required": True,
                "source_type": "merchant_correction", "feedback_id": row["id"]}
        if result.get("product_id"):
            item["product_ids"] = [result["product_id"]]
        if re.search(r'这个|这根|这件|那个|前一个|上一款',row['question']):
            item['requires_context']=True
        revision = self.kb.stage_incremental("faq", [item], "商家纠错 #%s" % row["id"], demo)
        with self.kb.connect() as db:
            db.execute("UPDATE feedback SET revision_id=?,evaluation=NULL,status='pending' WHERE id=?", (revision, row["id"]))
        return self.kb.preview(revision)

    def evaluate(self, service):
        with self.kb.connect() as db:
            rows = db.execute("SELECT f.*,i.question,i.input_state,i.result FROM feedback f JOIN interactions i ON i.id=f.interaction_id WHERE f.status!='retired' ORDER BY f.id").fetchall()
        cases = []
        for row in rows:
            state = json.loads(row["input_state"])
            if row['evaluation_state']:
                state = json.loads(row['evaluation_state'])
            from .dialogue import ReceptionService
            original = json.loads(row["result"])
            if original.get("context_expired"):
                state = {"after_sale_open": True} if state.get("after_sale_open") else {}
            elif state.get("last_activity"):
                # Replay the original active context, not the wall-clock age of
                # the regression case saved weeks earlier.
                state["last_activity"] = time.time()
            as_of = date.fromisoformat(original["reference_date"]) if original.get("reference_date") else service.today()
            case_service = ReceptionService(service.source, service.knowledge, service.interpreter, as_of=as_of)
            result = case_service.respond(row["question"], state, track_gaps=False)
            passed = not result.get('model_error') and result["action"] == row["expected_action"] and row["correction"] in result["draft_reply"]
            cases.append({"feedback_id": row["id"], "question": redact(row["question"]), "passed": passed,
                          "expected_action": row["expected_action"], "actual_action": result["action"],
                          "expected_contains": row["correction"],
                          "product_id": result.get("product_id"),
                          "actual_answer": redact(result["draft_reply"])})
        return {"passed": bool(cases) and all(c["passed"] for c in cases), "cases": cases,
                "mode": "model_regression" if service.interpreter else "offline_regression", "total": len(cases)}

    def evaluate_revision(self, revision, factory):
        proposed = self.kb.preview(int(revision))
        report = self.evaluate(factory(RevisionView(self.kb, proposed)))
        report["baseline"] = {r["kind"]: r["id"] for r in self.kb.revisions() if r["active"] and r["kind"] != proposed["kind"]}
        with self.kb.connect() as db:
            db.execute("UPDATE feedback SET evaluation=? WHERE revision_id=?", (json.dumps(report, ensure_ascii=False), int(revision)))
        return report

    def export_cases(self):
        with self.kb.connect() as db:
            rows = db.execute("SELECT f.id,f.correction,f.expected_action,i.question,i.input_state,i.result FROM feedback f "
                              "JOIN interactions i ON i.id=f.interaction_id WHERE f.status!='retired' ORDER BY f.id").fetchall()
        def clean(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value
        return {"schema_version": 1, "store_id": self.kb.store_id, "kind": "merchant_regression_cases",
                "cases": [{"case_id": row["id"], "question": redact(row["question"]),
                           "state": clean(json.loads(row["input_state"])), "expected_action": row["expected_action"],
                           "expected_contains": row["correction"], "reference_date": json.loads(row["result"]).get("reference_date")}
                          for row in rows]}
