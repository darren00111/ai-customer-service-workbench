"""Human overrides (人工改判) of one reception decision.

An override always applies to the current conversation first: the old draft
is invalidated and confirmed conditions that were not corrected are kept.
Whether it also becomes long-term knowledge is a separate, explicit choice;
a one-off concession can never be generalised to other customers. Saving an
override never sends anything to the customer.
"""
from __future__ import annotations

import json

from .knowledge import now, redact

REASONS = {
    "misunderstood": "理解错误",
    "knowledge_missing": "知识缺失",
    "stale": "资料过期",
    "conflict": "资料冲突",
    "platform_read": "平台读取错误",
    "wording": "表达不合适",
}
SCOPES = {"session_only": "仅用于本次", "store_candidate": "生成本店知识候选", "shared_suggestion": "建议更新共用知识"}
ACTIONS = {"reply": "直接答复", "ask_followup": "补问", "handoff": "人工处理", "keep_draft": "保留草稿", "resume": "恢复自动接待"}
# Which problem card a long-term override files, by reason.
CARD_FOR_REASON = {"misunderstood": "semantic", "knowledge_missing": "knowledge_missing", "stale": "knowledge_error",
                   "conflict": "knowledge_error", "platform_read": "platform_data"}


class OverrideBook:
    def __init__(self, kb):
        self.kb = kb
        with kb.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS overrides (
                id INTEGER PRIMARY KEY, interaction_id INTEGER NOT NULL, session_id TEXT,
                conversation_id TEXT DEFAULT '', original TEXT NOT NULL, changes TEXT NOT NULL,
                reason TEXT NOT NULL, scope TEXT NOT NULL, special_offer INTEGER DEFAULT 0,
                operator TEXT NOT NULL, manual_reply TEXT DEFAULT '', feedback_id INTEGER,
                card_id INTEGER, draft_invalidated INTEGER DEFAULT 0, status TEXT DEFAULT 'recorded',
                created_at TEXT NOT NULL)""")

    @staticmethod
    def validate(changes, reason, scope, special_offer):
        if reason not in REASONS:
            raise ValueError("请选择改判原因")
        if scope not in SCOPES:
            raise ValueError("请选择作用范围")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("请至少修改意图、商品、答案或后续动作中的一项")
        allowed = {"intent", "product_id", "answer", "action"}
        if set(changes) - allowed:
            raise ValueError("只能修改意图、商品归属、事实与答案、后续动作")
        if "action" in changes and changes["action"] not in ACTIONS:
            raise ValueError("后续动作无效")
        if "answer" in changes and (not isinstance(changes["answer"], str) or not 1 <= len(changes["answer"].strip()) <= 2000):
            raise ValueError("正确答案需为1–2000字")
        if "intent" in changes and (not isinstance(changes["intent"], str) or not changes["intent"].strip()):
            raise ValueError("意图无效")
        if "product_id" in changes and changes["product_id"] is not None and not isinstance(changes["product_id"], str):
            raise ValueError("商品标识无效")
        if special_offer and scope != "session_only":
            raise ValueError("一次特殊优惠或承诺只作用于原会话或订单，不能写成本店政策")

    def record(self, interaction_id, changes, reason, scope="session_only", operator="店主",
               special_offer=False, conversation_id=""):
        self.validate(changes, reason, scope, bool(special_offer))
        operator = (str(operator or "店主").strip() or "店主")[:60]
        with self.kb.connect() as db:
            row = db.execute("SELECT * FROM interactions WHERE id=?", (int(interaction_id),)).fetchone()
        if not row:
            raise ValueError("本店未找到该次接待记录")
        result = json.loads(row["result"])
        if "product_id" in changes and changes["product_id"]:
            products = self.kb.active("products")
            if changes["product_id"] not in {p["product_id"] for p in (products["payload"] if products else [])}:
                raise ValueError("商品必须来自本店已生效资料")
        original = {"intents": result.get("intents", []), "action": result.get("action"),
                    "product_id": result.get("product_id"), "reply": redact(result.get("draft_reply", ""))[:2000]}
        clean = {k: (redact(v.strip()) if isinstance(v, str) and k == "answer" else v) for k, v in changes.items()}
        feedback_id = card_id = None
        if scope == "store_candidate":
            from .learning import LearningLoop
            from .gapcards import GapBoard
            expected = clean.get("action", result.get("action") or "reply")
            expected = expected if expected in {"reply", "ask_followup", "handoff"} else "reply"
            if clean.get("answer"):
                feedback_id = LearningLoop(self.kb).record(row["id"], clean["answer"], expected)
            card_type = CARD_FOR_REASON.get(reason)
            if card_type:
                card_id = GapBoard(self.kb).file(card_type, row["question"], clean.get("product_id") or result.get("product_id") or "",
                                                 session_id=row["session_id"], interaction_id=row["id"],
                                                 reply=result.get("draft_reply", ""), operator=operator, source="override")
        with self.kb.connect() as db:
            cursor = db.execute(
                "INSERT INTO overrides(interaction_id,session_id,conversation_id,original,changes,reason,scope,special_offer,operator,"
                "manual_reply,feedback_id,card_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["session_id"], str(conversation_id or "")[:200], json.dumps(original, ensure_ascii=False),
                 json.dumps(clean, ensure_ascii=False), reason, scope, int(bool(special_offer)), operator,
                 clean.get("answer", ""), feedback_id, card_id,
                 "suggested" if scope == "shared_suggestion" else "recorded", now()))
            override_id = cursor.lastrowid
        return {"override_id": override_id, "feedback_id": feedback_id, "card_id": card_id,
                "session_id": row["session_id"], "original": original, "changes": clean, "sent": False}

    def mark_invalidated(self, override_id):
        with self.kb.connect() as db:
            db.execute("UPDATE overrides SET draft_invalidated=1 WHERE id=?", (int(override_id),))

    @staticmethod
    def apply_to_state(state, changes, product_titles=None):
        """Correct only what the merchant changed; keep the other confirmed conditions."""
        state = dict(state or {})
        if "product_id" in changes:
            if changes["product_id"] != state.get("product_id"):
                state.pop("variant", None)
            if changes["product_id"]:
                state["product_id"] = changes["product_id"]
                history = list(state.get("product_history") or [])
                if not history or history[-1] != changes["product_id"]:
                    history.append(changes["product_id"])
                state["product_history"] = history[-8:]
            else:
                state.pop("product_id", None)
            # A merchant's product choice beats the card that was on screen.
            ref = state.get("platform_product_reference")
            if ref:
                state["platform_reference_overridden"] = ref.get("message_id")
        if "intent" in changes:
            state["pending_intents"] = [changes["intent"]]
        if changes.get("action") == "handoff":
            state["human_override"] = "handoff"
        elif changes.get("action") == "resume":
            state.pop("human_override", None)
        return state

    def list(self, session_id=None, limit=100):
        with self.kb.connect() as db:
            if session_id:
                rows = db.execute("SELECT * FROM overrides WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, int(limit))).fetchall()
            else:
                rows = db.execute("SELECT * FROM overrides ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        return [{**dict(r), "original": json.loads(r["original"]), "changes": json.loads(r["changes"]),
                 "reason_label": REASONS[r["reason"]], "scope_label": SCOPES[r["scope"]]} for r in rows]
