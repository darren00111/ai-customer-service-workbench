"""Incremental synchronization of explicitly registered local merchant exports."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path


class SyncManager:
    def __init__(self, knowledge, demo=False):
        self.kb, self.demo = knowledge, demo

    def register(self, path, kind="products", mode="merge", auto_publish=False, interval=60, trusted_outcomes=False):
        if type(trusted_outcomes) is not bool:
            raise ValueError('成交来源确认状态无效')
        file = Path(path)
        if not file.is_absolute() or not file.is_file() or file.suffix.lower() not in {".json", ".csv"}:
            raise ValueError("请选择存在的本地 JSON 或 CSV 文件")
        if kind not in {"products", "business", "faq", "history", "outcomes"} or mode not in {"merge", "replace"}:
            raise ValueError("同步资料类型或更新方式无效")
        if kind != "products" and file.suffix.lower() == ".csv":
            raise ValueError("只有商品资料支持 CSV 同步")
        if auto_publish and kind != "products":
            raise ValueError("政策、问答和历史候选必须核对后启用")
        interval = max(10, min(int(interval), 86400))
        with self.kb.connect() as db:
            db.execute("INSERT INTO sync_sources(path,kind,format,mode,auto_publish,interval_seconds) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(path,kind) DO UPDATE SET mode=excluded.mode,auto_publish=excluded.auto_publish,"
                       "interval_seconds=excluded.interval_seconds,enabled=1,next_poll=0",
                       (str(file.resolve()), kind, file.suffix[1:].lower(), mode, int(auto_publish), interval))
            db.execute("UPDATE sync_sources SET digest=CASE WHEN trusted_outcomes<>? THEN '' ELSE digest END,trusted_outcomes=? WHERE path=? AND kind=?",
                       (int(trusted_outcomes),int(trusted_outcomes and kind=='outcomes'),str(file.resolve()),kind))
        return self.list()

    def list(self):
        with self.kb.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sync_sources ORDER BY id")]

    def enable(self, source_id, enabled):
        with self.kb.connect() as db:
            db.execute("UPDATE sync_sources SET enabled=?,next_poll=0 WHERE id=?", (int(bool(enabled)), int(source_id)))

    def poll(self, force=False):
        reports = []
        for row in self.list():
            if not row["enabled"]:
                continue
            timestamp = time.time()
            with self.kb.connect() as db:
                # Cross-window and engine invocations share a durable polling lease.
                changed = db.execute("UPDATE sync_sources SET next_poll=?,lease_until=? WHERE id=? AND enabled=1 AND lease_until<=? AND (next_poll<=? OR ?=1)",
                                     (timestamp + row["interval_seconds"], timestamp + 30, row["id"], timestamp, timestamp, int(force))).rowcount
                if changed:
                    row = dict(db.execute("SELECT * FROM sync_sources WHERE id=?", (row["id"],)).fetchone())
            if not changed:
                continue
            try:
                file = Path(row["path"])
                if file.stat().st_size > 5_000_000:
                    raise ValueError("同步文件超过5MB，请拆分")
                raw = file.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest == row["digest"]:
                    with self.kb.connect() as db:
                        db.execute("UPDATE sync_sources SET last_error='' WHERE id=?", (row["id"],))
                    reports.append({"source_id": row["id"], "status": "unchanged"})
                    continue
                content = raw.decode("utf-8-sig")
                if row['kind']=='outcomes':
                    from .commerce import CommerceLearning
                    with self.kb.connect() as db:
                        current=db.execute('SELECT enabled,trusted_outcomes FROM sync_sources WHERE id=?',(row['id'],)).fetchone()
                    if not current['enabled']:
                        reports.append({'source_id':row['id'],'status':'paused'});continue
                    summary=CommerceLearning(self.kb).ingest_export(json.loads(content),origin='merchant_export',trusted=bool(current['trusted_outcomes']))
                    with self.kb.connect() as db:
                        db.execute("UPDATE sync_sources SET digest=?,last_error='' WHERE id=?",(digest,row['id']))
                    reports.append({'source_id':row['id'],'status':'outcomes_imported',**summary})
                    continue
                if row["kind"] == "history":
                    revision = self.kb.learn_history(content, "历史文件同步", self.demo)
                    if row["mode"] == "merge":
                        revision = self.kb.stage_incremental("faq", self.kb.preview(revision)["payload"], "历史候选增量同步", self.demo)
                elif row["format"] == "csv":
                    staged = self.kb.import_products(content, "csv", "商品文件同步", self.demo)
                    revision = self.kb.stage_incremental("products", self.kb.preview(staged)["payload"], "商品文件增量同步", self.demo) if row["mode"] == "merge" else staged
                else:
                    value = json.loads(content)
                    if row["mode"] == "merge":
                        revision = self.kb.stage_incremental(row["kind"], value, "本地文件增量同步", self.demo)
                    else:
                        revision = self.kb.stage(row["kind"], value, "本地文件完整同步", self.demo)
                with self.kb.connect() as db:
                    settings = db.execute("SELECT enabled,auto_publish FROM sync_sources WHERE id=?", (row["id"],)).fetchone()
                if not settings["enabled"]:
                    reports.append({"source_id": row["id"], "status": "paused", "revision_id": revision})
                    continue
                if settings["auto_publish"]:
                    self.kb.activate(revision, allow_demo=self.demo)
                with self.kb.connect() as db:
                    db.execute("UPDATE sync_sources SET digest=?,last_revision=?,last_error='' WHERE id=?",
                               (digest, revision, row["id"]))
                reports.append({"source_id": row["id"], "revision_id": revision,
                                "status": "published" if settings["auto_publish"] else "staged"})
            except Exception as exc:
                message = str(exc) if isinstance(exc, (ValueError, UnicodeError, OSError)) else "同步未完成，请检查文件"
                with self.kb.connect() as db:
                    db.execute("UPDATE sync_sources SET last_error=? WHERE id=?", (message[:300], row["id"]))
                reports.append({"source_id": row["id"], "status": "error", "error": message})
            finally:
                with self.kb.connect() as db:
                    db.execute("UPDATE sync_sources SET lease_until=0 WHERE id=?", (row["id"],))
        return reports


class SyncWorker:
    """Keep registered exports current while the workbench window is open."""
    def __init__(self, workbench):
        self.workbench = workbench
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def run():
            while not self.stop_event.wait(5):
                try:
                    for store in self.workbench.stores():
                        self.workbench.sync(store["store_id"]).poll()
                        self.workbench.learning_refresh(store['store_id'])
                except Exception:
                    # Each registered source retains its visible last_error.
                    continue
        self.thread = threading.Thread(target=run, name="merchant-export-sync", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
