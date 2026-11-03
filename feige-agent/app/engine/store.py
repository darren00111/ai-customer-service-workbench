# -*- coding: utf-8 -*-
"""会话状态：防重复回复、限速、待确认候选。

防重复是安全底线 —— 重复回复比不回复更伤客户体验，而且平台会判定为异常。
判据只使用买家侧消息序号，不使用总气泡数。客服自己发送消息会增加总气泡数，
若把它放进指纹，同一条买家消息会在每次发送后被错误地识别为新消息。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections import deque
from pathlib import Path

__all__ = ["ConversationStore"]


def fingerprint(buyer: str, latest: str, buyer_message_count: int, last_time: str = "",
                conversation_id: str | None = None, message_id: str | None = None) -> str:
    # Prefer a platform message id when the DOM exposes one.  Visible bubble counts
    # change when Feige virtualises the history and are only a compatibility fallback.
    identity = "id:%s" % message_id if message_id else "fallback:%s|%d|%s" % (
        latest, buyer_message_count, last_time
    )
    raw = "%s|%s|%s" % (conversation_id or buyer, buyer, identity)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _legacy_fingerprint(buyer: str, latest: str, buyer_message_count: int,
                        last_time: str = "", conversation_id: str | None = None) -> str:
    """Version 1.0 fingerprint retained only for seamless upgrade deduplication."""
    raw = "%s|%s|%s|%d|%s" % (
        conversation_id or buyer, buyer, latest, buyer_message_count, last_time
    )
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


class ConversationStore:
    def __init__(self, path: Path | str, rate_limit_per_min: int = 20):
        self.path = Path(path)
        self.rate_limit = max(1, int(rate_limit_per_min))
        self._lock = threading.RLock()
        self.data = {"schema_version": 2, "conversations": {}, "sent_ts": []}
        self.recovery: dict = {"source": "new", "error": ""}
        self.load()

    # ---------- 持久化 ----------

    def load(self) -> None:
        candidates = [self.path, self.path.with_suffix(self.path.suffix + ".bak")]
        errors = []
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                got = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(got, dict):
                    self.data["conversations"] = got.get("conversations", {}) or {}
                    self.data["sent_ts"] = got.get("sent_ts", []) or []
                    self.data["schema_version"] = int(got.get("schema_version") or 1)
                    self.recovery = {"source": "primary" if candidate == self.path else "backup",
                                     "error": "; ".join(errors)}
                    return
                errors.append("%s: root is not an object" % candidate.name)
            except Exception as exc:
                errors.append("%s: %s" % (candidate.name, exc))
        if errors:
            self.recovery = {"source": "empty_after_corruption", "error": "; ".join(errors)}
            if self.path.exists():
                damaged = self.path.with_name(
                    "%s.corrupt-%d" % (self.path.name, int(time.time()))
                )
                try:
                    shutil.copy2(self.path, damaged)
                    self.recovery["corrupt_copy"] = str(damaged)
                except Exception:
                    pass

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.data["schema_version"] = 2
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            payload = json.dumps(self.data, ensure_ascii=False, indent=1)
            with tmp.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                try:
                    # Only rotate a file that can still be parsed; never overwrite a
                    # good backup with a damaged primary.
                    json.loads(self.path.read_text(encoding="utf-8"))
                    shutil.copy2(self.path, backup)
                except Exception:
                    pass
            os.replace(tmp, self.path)

    # ---------- 会话 ----------

    def get(self, buyer: str, conversation_id: str | None = None) -> dict:
        with self._lock:
            key = conversation_id or buyer
            conv = self.data["conversations"].setdefault(
                key,
                 {"last_fp": "", "pending_fp": "", "pending_at": 0,
                  "pending_status": "", "pending_reply": "",
                  "last_reply": "", "last_reply_at": 0, "last_msg": "",
                 "replies": 0, "paused": False, "pending_candidates": [],
                 "pending_intent": "", "last_product_id": "", "handoff_fps": [],
                 "handled_fps": [],
                 "handoff": False, "first_seen": time.time(), "buyer": buyer},
            )
            conv["buyer"] = buyer
            return conv

    def is_duplicate(self, buyer: str, fp: str, conversation_id: str | None = None) -> bool:
        return self.get(buyer, conversation_id).get("last_fp") == fp

    def should_reply(self, buyer: str, latest: str, buyer_message_count: int, last_time: str = "",
                     conversation_id: str | None = None,
                     message_id: str | None = None) -> tuple[bool, str, str]:
        """返回 (是否该回, 指纹, 原因)。"""
        conv = self.get(buyer, conversation_id)
        if not latest.strip():
            return False, "", "无买家消息"
        if conv.get("paused"):
            return False, "", "该会话已暂停自动回复（此前转人工）"
        if conv.get("pending_status") == "delivery_unknown":
            return False, "", "发送结果尚未核实，暂停该会话后续自动发送"
        fp = fingerprint(buyer, latest, buyer_message_count, last_time,
                         conversation_id, message_id)
        legacy_fp = _legacy_fingerprint(
            buyer, latest, buyer_message_count, last_time, conversation_id
        )
        if conv.get("last_fp") in (fp, legacy_fp):
            return False, fp, "这条消息已经回过"
        human_fps = conv.get("handoff_fps", []) + conv.get("handled_fps", [])
        if fp in human_fps or legacy_fp in human_fps:
            return False, fp, "这条消息已进入人工待办"
        if conv.get("pending_fp") in (fp, legacy_fp):
            return False, fp, "这条消息正在处理"
        return True, fp, "新消息"

    def reserve_reply(self, buyer: str, fp: str,
                      conversation_id: str | None = None) -> bool:
        """Atomically claim a buyer message before any delay or send action."""
        with self._lock:
            conv = self.get(buyer, conversation_id)
            if conv.get("last_fp") == fp or conv.get("pending_fp") == fp:
                return False
            conv["pending_fp"] = fp
            conv["pending_at"] = time.time()
            conv["pending_status"] = "reserved"
            conv["pending_reply"] = ""
            self.save()
            return True

    def mark_delivery_unknown(self, buyer: str, fp: str, reply: str,
                              conversation_id: str | None = None) -> None:
        """Keep the claim durable when a click happened but delivery is uncertain.

        Retrying automatically could duplicate a customer-visible message.  The
        pending item is intentionally surfaced for reconciliation instead.
        """
        with self._lock:
            conv = self.get(buyer, conversation_id)
            if conv.get("pending_fp") == fp:
                conv["pending_status"] = "delivery_unknown"
                conv["pending_reply"] = reply[:300]
                conv["pending_at"] = time.time()
                self.save()

    def release_reply(self, buyer: str, fp: str,
                      conversation_id: str | None = None) -> None:
        with self._lock:
            conv = self.get(buyer, conversation_id)
            if conv.get("pending_fp") == fp:
                conv["pending_fp"] = ""
                conv["pending_at"] = 0
                conv["pending_status"] = ""
                conv["pending_reply"] = ""
                self.save()

    def mark_replied(self, buyer: str, fp: str, reply: str, latest: str,
                     candidates: list | None = None, handoff: bool = False,
                     conversation_id: str | None = None,
                     product_id: str | None = None,
                     pending_intent: str | None = None) -> None:
        with self._lock:
            conv = self.get(buyer, conversation_id)
            conv["last_fp"] = fp
            conv["pending_fp"] = ""
            conv["pending_at"] = 0
            conv["pending_status"] = ""
            conv["pending_reply"] = ""
            conv["last_reply"] = reply[:300]
            conv["last_reply_at"] = time.time()
            conv["last_msg"] = latest[:200]
            conv["replies"] = int(conv.get("replies", 0)) + 1
            conv["pending_candidates"] = candidates or []
            conv["pending_intent"] = pending_intent or ""
            if product_id:
                conv["last_product_id"] = product_id
            if handoff:
                conv["handoff"] = True
            self.save()

    def record_handoff(self, buyer: str, fp: str, latest: str,
                       conversation_id: str | None = None) -> None:
        """Deduplicate one risky message without blocking later independent questions."""
        with self._lock:
            conv = self.get(buyer, conversation_id)
            fps = conv.setdefault("handoff_fps", [])
            if fp not in fps:
                fps.append(fp)
            conv["last_msg"] = latest[:200]
            conv["pending_intent"] = ""
            self.save()

    def set_dialogue(self, buyer: str, dialogue: dict, conversation_id: str | None = None) -> None:
        """Persist only after a confirmed send or a successfully queued handoff."""
        import copy
        with self._lock:
            self.get(buyer, conversation_id)["dialogue"] = copy.deepcopy(dialogue)
            self.save()

    def set_paused(self, buyer: str, paused: bool, conversation_id: str | None = None) -> None:
        with self._lock:
            self.get(buyer, conversation_id)["paused"] = bool(paused)
            self.save()

    def mark_handled(self, buyer: str, fp: str, latest: str, reply: str = "",
                     conversation_id: str | None = None) -> None:
        """Close a human task without counting it as an automatic reply."""
        with self._lock:
            conv = self.get(buyer, conversation_id)
            latest_is_current = not conv.get("last_fp") or conv.get("last_fp") == fp
            if latest_is_current:
                conv["last_fp"] = fp
            conv["handoff_fps"] = [item for item in conv.get("handoff_fps", []) if item != fp]
            handled = conv.setdefault("handled_fps", [])
            if fp not in handled:
                handled.append(fp)
                del handled[:-50]
            if conv.get("pending_fp") == fp:
                conv["pending_fp"] = ""
                conv["pending_at"] = 0
                conv["pending_status"] = ""
                conv["pending_reply"] = ""
            if latest_is_current:
                conv["last_msg"] = latest[:200]
            if reply and latest_is_current:
                conv["last_reply"] = reply[:300]
                conv["last_reply_at"] = time.time()
            conv["paused"] = False
            conv["handoff"] = False
            if conv.get("dialogue"):
                conv["dialogue"].pop("after_sale_open", None)
                if conv["dialogue"].get("summary"):
                    conv["dialogue"]["summary"]["after_sale_open"] = False
            self.save()

    # ---------- 限速 ----------

    def allow_send(self) -> tuple[bool, float]:
        """返回 (是否允许, 还需等待秒数)。按滑动一分钟窗口计。"""
        with self._lock:
            now = time.time()
            ts = deque(t for t in self.data.get("sent_ts", []) if now - t < 60)
            self.data["sent_ts"] = list(ts)
            if len(ts) >= self.rate_limit:
                oldest = ts[0]
                return False, max(0.0, 60 - (now - oldest))
            return True, 0.0

    def note_send(self) -> None:
        with self._lock:
            now = time.time()
            ts = [t for t in self.data.get("sent_ts", []) if now - t < 60]
            ts.append(now)
            self.data["sent_ts"] = ts
            self.save()

    def summary(self) -> dict:
        with self._lock:
            convs = self.data["conversations"]
            now = time.time()
            return {
                "conversations": len(convs),
                "paused": sum(1 for c in convs.values() if c.get("paused")),
                "total_replies": sum(int(c.get("replies", 0)) for c in convs.values()),
                "sent_last_minute": sum(1 for t in self.data.get("sent_ts", []) if now - t < 60),
                "rate_limit": self.rate_limit,
                "pending": sum(1 for c in convs.values() if c.get("pending_fp")),
                "delivery_unknown": sum(
                    1 for c in convs.values() if c.get("pending_status") == "delivery_unknown"
                ),
                "handoff_pending": sum(len(c.get("handoff_fps", [])) for c in convs.values()),
                "recovery": dict(self.recovery),
                "items": [
                    {"conversation_id": k, "buyer": v.get("buyer") or k,
                     "replies": int(v.get("replies", 0)),
                     "paused": bool(v.get("paused")), "last_msg": v.get("last_msg", ""),
                     "last_reply": v.get("last_reply", ""),
                     "last_reply_at": v.get("last_reply_at", 0),
                     "pending_status": v.get("pending_status", ""),
                     "pending_at": v.get("pending_at", 0)}
                    for k, v in sorted(convs.items(), key=lambda kv: -kv[1].get("last_reply_at", 0))
                ],
            }
