# -*- coding: utf-8 -*-
"""Durable, process-safe handoff notification outbox."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class HandoffOutbox:
    """Serialize alerts from every shop before a desktop transport sends them.

    Personal WeChat is a single foreground UI, so ten shop workers must never drive
    it independently. This outbox gives the future desktop notifier one ordered stream.
    """

    _locks = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path | str):
        self.path = Path(path)
        key=os.path.normcase(str(self.path.resolve()))
        with self._locks_guard:
            self._lock = self._locks.setdefault(key,threading.RLock())
        self._ids: set[str] = set()
        self._load_ids()

    def _load_ids(self) -> None:
        self._ids.clear()
        if not self.path.exists():
            return
        for line in self.path.read_bytes().splitlines():
            try:
                row = json.loads(line)
                if row.get("event_id"):
                    self._ids.add(row["event_id"])
            except Exception:
                # A damaged tail must not hide valid records before or after it.
                continue

    @contextmanager
    def _locked(self):
        """Serialize append and replace across all instances and processes."""
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self._lock, self.path.with_suffix(self.path.suffix+'.lock').open('a+b') as handle:
            if os.name=='nt':
                import msvcrt
                handle.seek(0,2)
                if not handle.tell():handle.write(b'0');handle.flush()
                handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
            try:yield
            finally:
                if os.name=='nt':
                    handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    @staticmethod
    def event_id(event: dict) -> str:
        raw = "|".join(str(event.get(k) or "") for k in
                       ("store_id", "conversation_id", "fingerprint", "category"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def publish(self, event: dict) -> dict:
        row = dict(event)
        row["event_id"] = row.get("event_id") or self.event_id(row)
        row.setdefault("created_at", time.time())
        row.setdefault("status", "pending")
        with self._locked():
            self._load_ids()
            if row["event_id"] in self._ids:
                return {"queued": False, "duplicate": True, "event_id": row["event_id"]}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            needs_newline=False
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open('rb') as previous:
                    previous.seek(-1,os.SEEK_END);needs_newline=previous.read(1)!=b'\n'
            with self.path.open("a", encoding="utf-8") as stream:
                if needs_newline:stream.write('\n')
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._ids.add(row["event_id"])
        return {"queued": True, "duplicate": False, "event_id": row["event_id"]}

    def list_pending(self, limit: int = 200) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        with self._locked():
            for line in self.path.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("status") == "pending":
                    if not row.get("draft_reply"):
                        row["draft_reply"] = self._fallback_draft(row)
                    rows.append(row)
        return rows[-max(1, int(limit)):]

    @staticmethod
    def _fallback_draft(event: dict) -> str:
        """Give older queued events a usable draft after upgrading."""
        category = event.get("category") or ""
        if category == "handoff_quality":
            return "亲，很抱歉给您带来不便。麻烦您发送一下订单编号和问题位置的清晰照片，我帮您核实。"
        if category == "handoff_after_sale":
            return "亲，收到您的售后需求了。麻烦您发送一下订单编号，并说明需要退货、换货还是退款，我帮您核实处理。"
        if category == "handoff_dispute":
            return "亲，您的反馈我已经收到，给您带来不好的体验很抱歉。我先结合订单核实具体情况，请稍等一下。"
        if category == "handoff_human_request":
            return "您好，人工客服已收到您的消息，请问具体需要帮您处理什么问题呢？"
        return "亲，我看到您的消息了。为了准确处理，麻烦您再说明一下具体商品和需要解决的问题。"

    def get_pending(self, event_id: str) -> dict | None:
        if not event_id:
            return None
        return next((row for row in self.list_pending(limit=10000)
                     if row.get("event_id") == event_id), None)

    def mark_done(self, event_id: str) -> bool:
        """Mark one alert handled while preserving the append-only event fields."""
        if not event_id or not self.path.exists():
            return False
        changed, output_lines = False, []
        with self._locked():
            for line in self.path.read_bytes().splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    # Preserve damaged/forward-compatible records byte-for-byte so
                    # handling one task can never erase unrelated runtime data.
                    output_lines.append(line)
                    continue
                if row.get("event_id") == event_id and row.get("status") == "pending":
                    row["status"] = "done"
                    row["handled_at"] = time.time()
                    changed = True
                output_lines.append(json.dumps(row, ensure_ascii=False).encode('utf-8'))
            if changed:
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                backup = self.path.with_suffix(self.path.suffix + ".bak")
                with tmp.open("wb") as stream:
                    stream.write(b"\n".join(output_lines) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                shutil.copy2(self.path, backup)
                os.replace(tmp, self.path)
        return changed
