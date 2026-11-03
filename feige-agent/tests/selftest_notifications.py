# -*- coding: utf-8 -*-
"""Concurrent durable handoff outbox test."""
from __future__ import annotations

import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.notifications import HandoffOutbox  # noqa: E402


def main():
    temp = Path(tempfile.mkdtemp(prefix="feige-alerts-"))
    outbox = HandoffOutbox(temp / "notifications.jsonl")

    def publish(shop):
        results = []
        for index in range(20):
            event = {
                "store_id": "shop_%03d" % shop,
                "conversation_id": "conv-%d" % index,
                "fingerprint": "fp-%d" % index,
                "category": "handoff_after_sale",
                "buyer": "买家", "summary": "我要退货", "draft_reply": "请提供订单号",
            }
            results.append(outbox.publish(event))
            results.append(outbox.publish(event))
        return results

    with ThreadPoolExecutor(max_workers=10) as pool:
        rows = list(pool.map(publish, range(1, 11)))
    queued = sum(1 for batch in rows for row in batch if row.get("queued"))
    duplicated = sum(1 for batch in rows for row in batch if row.get("duplicate"))
    pending = outbox.list_pending(limit=500)
    retrievable = outbox.get_pending(pending[0]["event_id"]) is not None
    with outbox.path.open("a", encoding="utf-8") as stream:
        stream.write("{damaged-tail\n")
    marked = outbox.mark_done(pending[0]["event_id"])
    rewritten = outbox.path.read_text(encoding="utf-8")
    checks = [
        ("10 店并发写入 200 个唯一通知", queued == 200),
        ("重复通知全部被拦截", duplicated == 200),
        ("落盘后可读取 200 个待发送通知", len(pending) == 200),
        ("通知带有店铺和会话身份", all(x.get("store_id") and x.get("conversation_id") for x in pending)),
        ("人工草稿完整落盘", all(x.get("draft_reply") == "请提供订单号" for x in pending)),
        ("可以按事件读取待处理草稿", retrievable),
        ("处理待办时保留无法解析的原始行", marked and "{damaged-tail" in rewritten),
        ("重写待办前保留备份", outbox.path.with_suffix(".jsonl.bak").exists()),
    ]
    failed = []
    for name, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", name))
        if not ok:
            failed.append(name)
    print("通过 %d 项，失败 %d 项" % (len(checks) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
