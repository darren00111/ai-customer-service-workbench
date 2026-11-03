# -*- coding: utf-8 -*-
"""Twelve-shop concurrency, isolation and supervisor recovery tests."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import APP_DIR, StoreRegistry, StoreSpec  # noqa: E402
from app.engine.pipeline import ReplyEngine              # noqa: E402
from app.engine.store import ConversationStore           # noqa: E402
from app.multistore import MultiStoreSupervisor          # noqa: E402
from app.notifications import HandoffOutbox              # noqa: E402
import app.engine.pipeline as pipeline_mod                # noqa: E402

passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print("  %s %s%s" % ("PASS" if condition else "FAIL", name,
                         (" -> " + str(detail)) if detail and not condition else ""))


class FakePage:
    def __init__(self, count=4, unread=True, message="在吗"):
        self.message = message
        self.rows = [
            {"i": i, "key": "conversation-%d" % i, "name": "同名买家",
             "preview": message, "unread": unread, "isCurrent": i == 0}
            for i in range(count)
        ]
        self.current = 0
        self.sent = []
        self.replied = set()

    def ensure_conversation(self, wait=0):
        return {"ok": True, "convs": self.list_convs()}

    def list_convs(self):
        return [dict(row, i=i, isCurrent=(i == self.current)) for i, row in enumerate(self.rows)]

    def click_conv(self, index):
        self.current = index
        return {"ok": True}

    def read(self):
        row = self.rows[self.current]
        messages = [{"role": "buyer", "time": "10:%02d" % self.current,
                     "content": self.message}]
        if row["key"] in self.replied:
            messages.append({"role": "agent", "time": "10:%02d" % self.current,
                             "content": "自动回复"})
        return {
            "buyerName": row["name"], "conversationKey": row["key"],
            "latestBuyerMessage": self.message, "latestBuyerTime": "10:%02d" % self.current,
            "buyerMessageCount": 1, "bubbleCount": len(messages),
            "lastRole": messages[-1]["role"], "messages": messages,
        }

    def send_text(self, text):
        key = self.rows[self.current]["key"]
        self.sent.append((key, text))
        self.replied.add(key)
        return {"ok": True}


class FakeCfg:
    def __init__(self):
        self.data = {"auto_send": True}

    def get(self, key, default=None):
        return {
            "auto_send": True, "gap_min": 0, "gap_max": 0,
            "pause_on_handoff": True,
            "conversation_switch_wait": 0, "max_conversations_per_cycle": 8,
        }.get(key, default)


class FakeAgent:
    active = 0
    peak = 0
    guard = threading.Lock()

    def __init__(self, spec):
        self.spec = spec
        self.cfg = FakeCfg()
        self.watching = False
        self.closed = False
        self.page_state = "ready"
        self.reconnects = 0
        self.total_replies = 0

    def boot(self):
        with self.guard:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
        time.sleep(0.025)
        with self.guard:
            type(self).active -= 1
        self.watching = True
        return {"ok": True, "state": "ready"}

    def status(self):
        return {"page": {"state": self.page_state}, "watching": self.watching,
                "watcher": {"counters": {"rounds": 1}},
                "conversations": {"total_replies": self.total_replies}}

    def start_watch(self):
        self.watching = True
        return True

    def stop_watch(self):
        self.watching = False
        return True

    def reconnect(self):
        self.reconnects += 1
        self.page_state = "ready"
        return {"ok": True, "state": "ready"}

    def shutdown(self):
        self.watching = False
        self.closed = True


def main():
    temp = Path(tempfile.mkdtemp(prefix="feige-multi-"))
    registry = StoreRegistry(temp / "stores.json")
    specs = [StoreSpec("shop_%03d" % i, "店铺%d" % i, 12000 + i,
                       str(temp / "profiles" / ("shop_%03d" % i))) for i in range(1, 13)]
    registry.save(specs)

    print("=== 1. 配置隔离 ===")
    loaded = registry.load()
    check("可加载 12 个店铺", len(loaded) == 12, len(loaded))
    check("12 个端口互不重复", len({s.cdp_port for s in loaded}) == 12)
    check("12 个 profile 互不重复", len({s.profile_dir for s in loaded}) == 12)
    portable = StoreSpec("portable", "便携配置", 13000, "data/profiles/portable")
    check("相对 profile 随程序目录解析",
          Path(portable.config_overrides()["profile_dir"]) ==
          (APP_DIR / "data/profiles/portable").resolve())
    try:
        registry.validate([loaded[0], StoreSpec("another", "重复端口", loaded[0].cdp_port,
                                                str(temp / "other"))])
        duplicate_rejected = False
    except ValueError:
        duplicate_rejected = True
    check("重复端口在启动前被拒绝", duplicate_rejected)

    print("\n=== 2. 12 店并发监督 ===")
    FakeAgent.peak = FakeAgent.active = 0
    supervisor = MultiStoreSupervisor(
        specs=loaded, agent_factory=FakeAgent, health_interval=0.05
    )
    result = supervisor.boot_all(max_parallel=3)
    check("12 店全部在线", result["online"] == 12, json.dumps(result, ensure_ascii=False))
    check("启动并发受到上限保护", 1 < FakeAgent.peak <= 3, FakeAgent.peak)
    victim = supervisor.agents["shop_006"]
    victim.watching = False
    time.sleep(0.45)
    check("监督器自动拉起停止的监听器", victim.watching is True)
    rebound = supervisor.agents["shop_005"]
    rebound.page_state = "wrong_site"
    rebound.watching = False
    time.sleep(0.3)
    check("页面异常后自动重新绑定并恢复监听",
          rebound.reconnects >= 1 and rebound.watching is True and
          supervisor.runtime["shop_005"]["state"] == "online")
    supervisor.stop_store("shop_007")
    time.sleep(0.3)
    check("人工停止的店铺不会被监督器误启动",
          supervisor.agents["shop_007"].watching is False)
    closed = supervisor.agents["shop_008"]
    closed.page_state = "not_connected"
    closed.watching = False
    time.sleep(0.3)
    check("人工关闭浏览器后不会被监督器重新启动",
          closed.watching is False and closed.reconnects == 0 and
          supervisor.runtime["shop_008"]["desired"] is False)
    victim.total_replies = 7
    status = supervisor.status()
    check("聚合状态返回 12 店", status["configured"] == 12 and status["online"] == 10)
    shop6 = next(row for row in status["shops"] if row["store_id"] == "shop_006")
    check("聚合状态返回持久化累计回复数", shop6.get("total_replies") == 7)
    supervisor.shutdown()
    check("关闭时清理全部店铺运行器", all(a.closed for a in supervisor.agents.values()))

    print("\n=== 3. 12 店真实编排路径压测 ===")
    original_logs = pipeline_mod.LOGS_DIR
    pipeline_mod.LOGS_DIR = temp / "logs"

    def run_shop(spec):
        page = FakePage(count=4)
        state = ConversationStore(temp / "runtime" / spec.store_id / "conversations.json", 100)
        engine = ReplyEngine(FakeCfg(), page, state, store_id="shop_001",
                             runtime_store_id=spec.store_id)
        first = engine.process_cycle()
        second = engine.process_cycle()
        return spec.store_id, first, second, page.sent, state.summary()

    try:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=12) as pool:
            rows = list(pool.map(run_shop, loaded))
        elapsed = time.perf_counter() - started
    finally:
        pipeline_mod.LOGS_DIR = original_logs

    check("12 店共处理 48 个会话", sum(r[1]["processed"] for r in rows) == 48)
    check("12 店共发送 48 条", sum(r[1]["sent"] for r in rows) == 48)
    check("第二轮全部防重复", sum(r[2]["sent"] for r in rows) == 0)
    check("客服回复增加气泡后不会重发旧买家消息",
          all(len(r[3]) == 4 for r in rows))
    check("同名买家的 4 个会话没有互相覆盖",
          all(r[4]["conversations"] == 4 for r in rows))
    check("每店状态文件独立落盘",
          len(list((temp / "runtime").glob("*/conversations.json"))) == 12)
    print("  INFO 12 店 x 4 会话两轮耗时 %.3fs" % elapsed)

    print("\n=== 4. 长会话列表轮转 ===")
    page = FakePage(count=20, unread=False)
    state = ConversationStore(temp / "rotation.json", 100)
    engine = ReplyEngine(FakeCfg(), page, state, store_id="shop_001", runtime_store_id="rotation")
    cycle_counts = [engine.process_cycle()["sent"] for _ in range(3)]
    check("单轮上限为 8 时三轮覆盖 20 个会话",
          state.summary()["conversations"] == 20, cycle_counts)

    print("\n=== 5. 售后通知统一入队 ===")
    outbox = HandoffOutbox(temp / "handoff.jsonl")
    page = FakePage(count=1, message="我要退货")
    state = ConversationStore(temp / "handoff-state.json", 100)
    engine = ReplyEngine(FakeCfg(), page, state, store_id="shop_001",
                         runtime_store_id="shop_009", event_sink=outbox.publish)
    handoff = engine.process_cycle()
    pending = outbox.list_pending()
    check("退换货消息进入统一通知队列",
          handoff["handoffs"] == 1 and len(pending) == 1)
    check("通知包含来源店铺与会话",
          pending[0]["store_id"] == "shop_009" and pending[0]["conversation_id"] == "conversation-0")
    check("高风险售后只生成草稿且绝不自动发送",
          handoff["sent"] == 0 and not page.sent and bool(pending[0].get("draft_reply")))

    print("\n=== 6. 安全自动补问 ===")
    page = FakePage(count=1, message="买什么码")
    state = ConversationStore(temp / "followup-state.json", 100)
    engine = ReplyEngine(FakeCfg(), page, state, store_id="shop_001",
                         runtime_store_id="shop_010")
    followup = engine.process_cycle()
    check("缺少商品时自动发送补问", followup["sent"] == 1 and len(page.sent) == 1)
    check("补问发送后等待客户新消息", engine.process_cycle()["sent"] == 0)

    print("\n" + "=" * 50)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    for item in failed:
        print("  失败：%s" % item)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
