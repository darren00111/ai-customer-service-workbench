# -*- coding: utf-8 -*-
"""编排层：读页面 -> 判定 -> 发送。

判定逻辑不在这里重写，直接复用已经过 57 个用例回归验证的
feige-fashion-customer-service/scripts/classify.py，保证规则只有一份真相。
"""
from __future__ import annotations

import json
import random
import re
import sys
import threading
import time
from contextlib import nullcontext
from datetime import datetime

from ..config import LOGS_DIR, SCRIPTS_DIR, STORES_KB_DIR, RECEPTION_DIR
from ..service import KnowledgeStore, ReceptionService
from ..service.data import SnapshotDataSource
from ..service.sync import SyncManager

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import classify as CL          # noqa: E402
import classify_standard as STANDARD  # noqa: E402
import kb as KB                # noqa: E402

KB.STORES_DIR = str(STORES_KB_DIR)

__all__ = ["ReplyEngine"]


class ReplyEngine:
    # 会话已结束的系统提示。最后一条是它，就说明这个会话已经关闭：
    # 客户收不到消息，页面也不再渲染输入框。此时若还去回最后那条买家消息，
    # 就是往一个已经空掉的会话里自言自语。
    CLOSED_PAT = re.compile(r"用户超时未回复|系统关闭会话|会话已关闭|会话已结束")
    AUTO_FOLLOWUP_CATEGORIES = {"need_product", "pre_sale_size"}

    def __init__(self, cfg, page, state, store_id: str = "shop_001",
                 runtime_store_id: str | None = None, event_sink=None, interpreter=None, interpreter_factory=None):
        self.cfg = cfg
        self.page = page
        self.state = state
        self.store_id = store_id
        self.runtime_store_id = runtime_store_id or store_id
        self.event_sink = event_sink
        self.interpreter = interpreter
        self._interpreter_factory = interpreter_factory
        self.store: dict = {}
        self.products: list = []
        self._scan_cursor = 0
        self._sidebar_revision: int | None = None
        self._cycle_lock = threading.RLock()
        self.reload_kb()

    # ---------- 知识库 ----------

    def reload_kb(self) -> dict:
        self.store, self.products = KB.load_kb(self.store_id)
        if self.store.get("platform", "feige") != "feige":
            raise ValueError("此平台尚未接入消息，不能使用飞鸽发送流程")
        if self.store.get("business", {}).get("reception", {}).get("enabled"):
            if self.runtime_store_id != self.store_id:
                raise ValueError("新版接待要求每个店铺使用自己的知识库")
            active = KnowledgeStore(RECEPTION_DIR / "shops", self.store_id).active("products")
            if active:
                self.products = active["payload"]
        if self.store.get("decision_engine") == "standard" and self.cfg.get("auto_send"):
            from merchant import onboarding_errors, read_manifest
            manifest = read_manifest(STORES_KB_DIR / self.store_id / "merchant.yaml")
            errors = onboarding_errors(manifest, self.products)
            if errors:
                raise ValueError("店铺 %s 尚不能自动发送：%s" % (self.store_id, "；".join(errors)))
        return {"store": self.store.get("store_name") or self.store_id,
                "products": len(self.products)}

    def _reception(self, sync=True) -> ReceptionService:
        knowledge = KnowledgeStore(RECEPTION_DIR / "shops", self.store_id)
        if sync:
            SyncManager(knowledge).poll()
        products = knowledge.active("products")
        business = knowledge.active("business")
        source = SnapshotDataSource(
            self.store_id, business["payload"] if business else self.store.get("business", {}),
            products["payload"] if products else self.products,
            demo=bool((products and products["demo"]) or (business and business["demo"])),
            provenance={"products": products or {}, "business": business or {}},
        )
        interpreter = self.interpreter
        if interpreter is None and self._interpreter_factory is not None:
            interpreter = self._interpreter_factory()
        return ReceptionService(source, knowledge, interpreter)

    @staticmethod
    def _human_draft(decision: dict) -> str:
        """Build a useful, editable draft without inventing order facts."""
        category = decision.get("category") or ""
        product = decision.get("product_title") or ""
        if category == "handoff_human_request":
            return "您好，人工客服已收到您的消息，请问具体需要帮您处理什么问题呢？"
        if category == "handoff_dispute":
            return "亲，您的反馈我已经收到，给您带来不好的体验很抱歉。我先结合订单核实具体情况，请稍等一下。"
        if category == "handoff_after_sale":
            return "亲，收到您的售后需求了。麻烦您发送一下订单编号，并说明需要退货、换货还是退款，我帮您核实处理。"
        if category == "handoff_quality":
            return "亲，很抱歉给您带来不便。麻烦您发送一下订单编号和问题位置的清晰照片，并说明商品是否已经穿着或洗涤，我帮您核实。"
        if category == "handoff_bargain":
            return "亲，我帮您核实一下当前可用的优惠，最终价格以商品页面和平台活动显示为准。"
        if category == "handoff_stock_out":
            return "亲，这款目前暂时缺货，我帮您确认补货情况或看看是否有合适的替代款。"
        if category == "handoff_data_gap":
            subject = "“%s”" % product if product else "您咨询的商品信息"
            return "亲，%s我还需要进一步核实，确认清楚后再回复您，避免给您错误信息。" % subject
        return "亲，我看到您的消息了。为了准确处理，麻烦您再说明一下具体商品和需要解决的问题。"

    # ---------- 日志 ----------

    def _log(self, record: dict) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        record.setdefault("store_id", self.runtime_store_id)
        line = json.dumps(record, ensure_ascii=False)
        shop_logs = LOGS_DIR / self.runtime_store_id
        shop_logs.mkdir(parents=True, exist_ok=True)
        with (shop_logs / ("%s.jsonl" % day)).open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---------- 会话定位 ----------

    def ensure_live(self) -> dict:
        """让页面停在一个「还活着」的会话上，必要时自己切过去。

        为什么必须做：客户新进线时会话出现在「当前会话」页签，而页面往往还停在
        上一个已被系统关闭的会话上。不主动切，监听会一直读旧会话，
        表现出来就是「客户明明发了消息，这边毫无反应」——这个坑当天已经撞过一次。

        已在活会话上时不做任何切换，避免无谓地打断人工操作。
        """
        snap = self.page.read()
        buyer = (snap.get("buyerName") or "").strip()
        msgs = snap.get("messages") or []
        closed = bool(msgs) and msgs[-1].get("role") == "system" and \
            bool(self.CLOSED_PAT.search(msgs[-1].get("content") or ""))
        if buyer and not closed:
            return {"ok": True, "switched": False, "buyer": buyer}

        self.page.click_tab("当前会话")
        time.sleep(0.7)
        convs = self.page.list_convs()
        if not convs:
            return {"ok": False, "reason": "当前会话页签为空"}
        # 未读优先；没有未读就取列表第一条（飞鸽按最近活跃排序）
        idx = next((c["i"] for c in convs if c.get("unread")), 0)
        self.page.click_conv(idx)
        time.sleep(0.7)
        return {"ok": True, "switched": True, "buyer": convs[idx].get("name")}

    # ---------- 主流程 ----------

    def process_once(self, dry_run: bool = False, ensure_live: bool = True) -> dict:
        """跑一轮：读到新买家消息就判定并回复，否则什么都不做。"""
        lease = self.page.control("reply-once") if hasattr(self.page, "control") else nullcontext()
        with lease:
            return self._process_once_unlocked(dry_run=dry_run, ensure_live=ensure_live)

    def _process_once_unlocked(self, dry_run: bool = False,
                               ensure_live: bool = True) -> dict:
        started = time.time()
        result = {
            "at": datetime.now().strftime("%H:%M:%S"),
            "acted": False,
            "sent": False,
            "dry_run": dry_run,
            "store_id": self.runtime_store_id,
        }

        # 先确认停在活着的会话上。找不到活跃会话就到此为止：
        # 继续读只会读到上一个已关闭会话的残留内容（切换页签并不会清空右侧内容区），
        # 拿这份数据判定毫无意义。
        if ensure_live:
            live = self.ensure_live()
            if not live.get("ok"):
                result["buyer"] = live.get("buyer") or ""
                result["skip"] = "没有活跃会话：%s" % live.get("reason", "")
                return result

        snap = self.page.read()
        buyer = (snap.get("buyerName") or "").strip()
        conversation_id = (snap.get("conversationKey") or buyer).strip()
        latest = (snap.get("latestBuyerMessage") or "").strip()
        bubbles = int(snap.get("bubbleCount") or 0)
        msgs = snap.get("messages") or []
        last_role = snap.get("lastRole") or (msgs[-1].get("role", "") if msgs else "")
        buyer_count = int(snap.get("buyerMessageCount") or
                          sum(1 for msg in msgs if msg.get("role") == "buyer"))
        last_time = snap.get("latestBuyerTime") or next(
            (msg.get("time", "") for msg in reversed(msgs) if msg.get("role") == "buyer"), ""
        )
        message_id = (snap.get("latestBuyerKey") or "").strip()

        result.update({"buyer": buyer, "conversation_id": conversation_id,
                       "latest": latest, "bubbles": bubbles})

        if not buyer:
            result["skip"] = "当前没有打开的会话"
            return result

        # The newest visible bubble must be from the buyer.  Our own successful
        # send leaves latestBuyerMessage unchanged, so without this gate the old
        # customer message can be picked up again on the next polling cycle.
        if last_role != "buyer":
            result["skip"] = "最后一条不是买家消息"
            return result

        # 会话已关闭就不要回。顺序很关键：只有「最后一条」是关闭提示才算数，
        # 客户在关闭后又发来新消息时，最后一条就是买家消息，照常回复。
        if msgs:
            last = msgs[-1]
            if last.get("role") == "system" and self.CLOSED_PAT.search(last.get("content") or ""):
                result["closed"] = True
                result["skip"] = "会话已结束，不向已关闭的会话发送"
                return result

        ok, fp, why = self.state.should_reply(
            buyer, latest, buyer_count, last_time, conversation_id=conversation_id,
            message_id=message_id or None,
        )
        result["fingerprint"] = fp
        if not ok:
            result["skip"] = why
            return result

        conv = self.state.get(buyer, conversation_id=conversation_id)
        import copy
        dialogue_before = copy.deepcopy(conv.get("dialogue") or {})
        # 商品定位：已确认的会话商品 + 最近买家发送的商品卡/商品名称。
        context_product = None
        if conv.get("last_product_id"):
            context_product = conv["last_product_id"]

        standard = self.store.get("decision_engine") == "standard"
        classifier = STANDARD if standard else CL
        product_hint = None
        if standard:
            for message in reversed(msgs[:-1]):
                if message.get("role") != "buyer":
                    continue
                content = message.get("content") or ""
                if STANDARD._product(content, self.products):
                    product_hint = content
                    break
        classify_args = {"product_hint": product_hint, "context_product": context_product,
                         "pending_candidates": conv.get("pending_candidates") or None}
        if standard:
            classify_args["pending_intent"] = conv.get("pending_intent") or None
            classify_args["handoff_pending"] = bool(conv.get("handoff_fps"))
        reception_enabled = bool(self.store.get("business", {}).get("reception", {}).get("enabled"))
        if reception_enabled:
            # Merge adjacent, as-yet-unanswered buyer bubbles; retain original
            # role-tagged messages for the interpreter. This avoids losing the
            # product name when a customer sends the question in a second bubble.
            batch = []
            for message in reversed(msgs):
                if message.get("role") != "buyer":
                    break
                if message.get("content") == conv.get("last_msg"):
                    break
                batch.insert(0, message.get("content") or "")
                if len(batch) >= 8:
                    break
            query = "\n".join(batch)[-8000:] if batch else latest
            decision = self._reception(sync=not dry_run).respond(query, conv.get("dialogue"), messages=msgs,
                                                  live=True, track_gaps=not dry_run, session_id=conversation_id)
        else:
            decision = classifier.classify(latest, self.store, self.products, **classify_args)
        draft = (decision.get("draft_reply") or "").strip()
        if decision.get("action") == "handoff":
            draft = self._human_draft(decision)
        result.update({
            "category": decision.get("category"),
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "confidence": decision.get("confidence"),
            "product": decision.get("product_title") or decision.get("product_id"),
            "handoff_reason": decision.get("handoff_reason"),
            "draft": draft,
        })
        if reception_enabled:
            result.update({key: decision.get(key) for key in (
                "mode", "model_connected", "model_status", "model_metrics", "evidence", "missing", "automatic_resolution", "can_send", "intents")})
        if dry_run:
            result["skip"] = "试运行：只判定未发送"
            return result
        if not self.cfg.get("auto_send"):
            result["skip"] = "自动发送已关闭，仅生成草稿"
            return result

        if decision.get("action") == "handoff" and self.event_sink:
            try:
                result["notification"] = self.event_sink({
                    "store_id": self.runtime_store_id,
                    "conversation_id": conversation_id,
                    "buyer": buyer,
                    "fingerprint": fp,
                    "category": decision.get("category"),
                    "reason": decision.get("handoff_reason") or decision.get("reason"),
                    "summary": latest[:200],
                    "draft_reply": draft,
                    "confidence": decision.get("confidence") or "low",
                    "missing": decision.get("missing") or [],
                    "product": decision.get("product_title") or decision.get("product_id"),
                    "context": [
                        {"role": m.get("role"), "time": m.get("time"),
                         "content": (m.get("content") or "")[:300]}
                        for m in msgs[-10:]
                    ],
                })
            except Exception as exc:  # noqa: BLE001
                # Notification failure must be visible, but must not crash all replies.
                result["notification"] = {"queued": False, "error": str(exc)}

        # Standard shops deduplicate the risky message, while later independent
        # information questions may still be answered.  If the outbox is down,
        # retain the legacy pause so the human task cannot silently disappear.
        if decision.get("action") == "handoff":
            notification = result.get("notification") or {}
            if standard and (notification.get("queued") or notification.get("duplicate")):
                self.state.record_handoff(buyer, fp, latest, conversation_id=conversation_id)
                result["skip"] = "该问题已生成待办，后续独立信息咨询仍可自动处理"
            else:
                self.state.set_paused(buyer, True, conversation_id=conversation_id)
                result["paused"] = True
                result["skip"] = "已生成草稿，等待人工确认"
            if reception_enabled and result.get("notification", {}).get("queued"):
                dialogue = decision["state"]
                # The handoff acknowledgement above is a draft, not a sent bubble.
                dialogue["messages"] = dialogue["messages"][:-1]
                self.state.set_dialogue(buyer, dialogue, conversation_id)
                try:
                    KnowledgeStore(RECEPTION_DIR / "shops", self.store_id).save_turn(conversation_id, decision["state"]["last_turn"]["buyer"]["content"], dialogue_before, decision, include_agent=False)
                except Exception:
                    result["archive_error"] = "人工待办已记录，对话归档未完成"
            self._log(result)
            return result

        if reception_enabled and not decision.get("can_send"):
            result["skip"] = ("生成草稿期间资料已更新，将重新查询" if decision.get("stale_knowledge") else
                              decision.get("model_status") or "新版接待尚未启用模型或正在使用模拟资料，仅供预览")
            self._log(result)
            return result

        # Automatic follow-up is deliberately narrow: only missing product or
        # body measurements may be asked without a human decision.
        if decision.get("action") == "ask_followup" and (
                (not standard and decision.get("category") not in self.AUTO_FOLLOWUP_CATEGORIES) or
                decision.get("confidence") not in ("medium", "high")):
            result["skip"] = "补问置信度不足，等待人工处理"
            self.state.set_paused(buyer, True, conversation_id=conversation_id)
            result["paused"] = True
            self._log(result)
            return result

        if not draft:
            result["skip"] = "判定结果没有可发的草稿（需人工）"
            if decision.get("action") == "handoff" and self.cfg.get("pause_on_handoff"):
                self.state.set_paused(buyer, True, conversation_id=conversation_id)
                result["paused"] = True
            self._log(result)
            return result

        # 限速：超出单店铺每分钟上限就排队，不硬发
        allowed, wait = self.state.allow_send()
        if not allowed:
            result["skip"] = "触发速率上限，需等待 %.0f 秒" % wait
            return result

        if not self.state.reserve_reply(buyer, fp, conversation_id=conversation_id):
            result["skip"] = "这条消息已经回过或正在处理"
            return result

        # 平台风控：发送前加随机停顿，避免节奏过于规律被判定为机器人
        lo, hi = float(self.cfg.get("gap_min", 0.8)), float(self.cfg.get("gap_max", 2.2))
        if hi > lo:
            time.sleep(random.uniform(lo, hi))

        try:
            if reception_enabled:
                fresh = self.page.read()
                fresh_msgs = fresh.get("messages") or []
                unchanged = (
                    (fresh.get("conversationKey") or fresh.get("buyerName")) == conversation_id and
                    fresh.get("buyerName") == buyer and
                    fresh.get("latestBuyerMessage") == latest and
                    (fresh.get("lastRole") or (fresh_msgs[-1].get("role") if fresh_msgs else "")) == "buyer" and
                    ((fresh.get("latestBuyerKey") == message_id) if message_id else
                     int(fresh.get("buyerMessageCount") or sum(m.get("role") == "buyer" for m in fresh_msgs)) == buyer_count)
                )
                knowledge = KnowledgeStore(RECEPTION_DIR / "shops", self.store_id)
                current_versions = {kind: row["id"] for kind in ("products", "business", "faq") if (row := knowledge.active(kind))}
                if current_versions != decision.get("knowledge_versions", {}):
                    self.state.release_reply(buyer, fp, conversation_id=conversation_id)
                    result["skip"] = "生成草稿期间店铺资料已更新，将重新查询"
                    self._log(result)
                    return result
                if not unchanged or not self.cfg.get("auto_send"):
                    self.state.release_reply(buyer, fp, conversation_id=conversation_id)
                    result["skip"] = "生成草稿期间会话已变化或自动发送已关闭，将按最新消息重新处理"
                    self._log(result)
                    return result
            sent = self.page.send_text(draft)
        except Exception:
            self.state.release_reply(buyer, fp, conversation_id=conversation_id)
            raise
        result["sent"] = bool(sent.get("ok"))
        result["send_detail"] = sent
        if sent.get("ok"):
            self.state.note_send()
            self.state.mark_replied(
                buyer, fp, draft, latest,
                candidates=decision.get("candidates") or [],
                handoff=(decision.get("action") == "handoff"),
                conversation_id=conversation_id,
                product_id=decision.get("product_id") if standard else None,
                pending_intent=decision.get("pending_intent") if standard else None,
            )
            if reception_enabled:
                self.state.set_dialogue(buyer, decision["state"], conversation_id)
                decision['delivery_channel']='live_confirmed'
                try:
                    KnowledgeStore(RECEPTION_DIR / "shops", self.store_id).save_turn(conversation_id, decision["state"]["last_turn"]["buyer"]["content"], dialogue_before, decision)
                    from ..service.commerce import CommerceLearning
                    CommerceLearning(KnowledgeStore(RECEPTION_DIR / 'shops',self.store_id)).refresh()
                except Exception:
                    result["archive_error"] = "回复已确认发送，对话归档未完成"
            if decision.get("action") == "handoff" and self.cfg.get("pause_on_handoff"):
                self.state.set_paused(buyer, True, conversation_id=conversation_id)
                result["paused"] = True
            result["elapsed_ms"] = int((time.time() - started) * 1000)
        elif sent.get("safe_to_retry"):
            self.state.release_reply(buyer, fp, conversation_id=conversation_id)
            result["skip"] = "发送失败：%s" % sent.get("reason")
        else:
            self.state.mark_delivery_unknown(
                buyer, fp, draft, conversation_id=conversation_id
            )
            result["delivery_unknown"] = True
            result["skip"] = "发送结果无法确认，已停止自动重试，请人工核对会话"
        self._log(result)
        return result

    def wait_for_activity(self, timeout: float = 4.0) -> bool | None:
        """DOM-side change signal; None means the page adapter needs polling fallback."""
        if not hasattr(self.page, "wait_for_sidebar_change"):
            return None
        event = self.page.wait_for_sidebar_change(self._sidebar_revision, timeout)
        self._sidebar_revision = event.get("revision", self._sidebar_revision)
        return bool(event.get("changed"))

    def process_cycle(self, dry_run: bool = False, full_scan: bool = True) -> dict:
        """Scan several conversations, prioritising unread ones.

        Only one scan runs per shop.  The page lease is acquired for the initial
        sidebar snapshot and each conversation separately, so a manual reply can
        take control between conversations.  Opening by key re-locates the row at
        click time because Feige can reorder the sidebar.
        """
        with self._cycle_lock:
            return self._process_cycle_unlocked(dry_run=dry_run, full_scan=full_scan)

    def _process_cycle_unlocked(self, dry_run: bool = False, full_scan: bool = True) -> dict:
        limit = max(1, int(self.cfg.get("max_conversations_per_cycle", 12)))
        wait = max(0.0, float(self.cfg.get("conversation_switch_wait", 0.7)))
        # 自动监听只处理「当前会话」。不能退到「最近联系」补扫旧记录，
        # 否则首次启动会把历史买家消息当成新消息补发。
        lease = self.page.control("background-list") if hasattr(self.page, "control") else nullcontext()
        with lease:
            tabs = self.page.tabs() if hasattr(self.page, "tabs") else []
            already_current = any("当前会话" in (tab.get("name") or "") and tab.get("selected")
                                  for tab in tabs)
            switched = ({"ok": True, "switched": False} if already_current else
                        self.page.click_tab("当前会话") if hasattr(self.page, "click_tab") else
                        {"ok": False})
            if switched.get("ok"):
                if not already_current and wait:
                    time.sleep(wait)
                convs = self.page.list_convs()
            else:
                found = self.page.ensure_conversation(wait=wait)
                convs = found.get("convs") or []
        if not convs:
            return {"store_id": self.runtime_store_id, "processed": 0, "sent": 0,
                    "results": [], "skip": "没有可扫描的会话"}

        unread = [c for c in convs if c.get("unread")]
        rest = [c for c in convs if not c.get("unread")] if full_scan else []
        # 未读永远优先；剩余槽位在普通会话中轮转，避免列表超过单轮上限后
        # 永远只检查顶部同一批会话。
        take_unread = unread[:limit]
        remaining = limit - len(take_unread)
        rotated = []
        if rest and remaining > 0:
            offset = self._scan_cursor % len(rest)
            ring = rest[offset:] + rest[:offset]
            rotated = ring[:remaining]
            self._scan_cursor = (offset + len(rotated)) % len(rest)
        ordered = take_unread + rotated
        results, seen = [], set()
        for wanted in ordered:
            ident = wanted.get("key") or wanted.get("name") or str(wanted.get("i"))
            if ident in seen:
                continue
            seen.add(ident)
            lease = self.page.control("background-conversation") if hasattr(self.page, "control") else nullcontext()
            with lease:
                if hasattr(self.page, "open_conv"):
                    opened = self.page.open_conv(wanted.get("key") or "", wanted.get("name") or "")
                else:
                    current = self.page.list_convs()
                    target = next((c for c in current if c.get("key") and c.get("key") == wanted.get("key")), None)
                    if target is None and str(wanted.get("key") or "").startswith("fallback:"):
                        by_name = [c for c in current if c.get("name") == wanted.get("name")]
                        target = by_name[0] if len(by_name) == 1 else None
                    opened = self.page.click_conv(int(target["i"])) if target else {"ok": False}
                if not opened.get("ok"):
                    results.append({"store_id": self.runtime_store_id, "sent": False,
                                    "skip": "会话列表刷新后无法安全定位", "conversation_id": ident})
                    continue
                if opened.get("switched", True) and wait:
                    time.sleep(wait)
                results.append(self._process_once_unlocked(dry_run=dry_run, ensure_live=False))

        return {
            "store_id": self.runtime_store_id,
            "processed": len(results),
            "sent": sum(1 for r in results if r.get("sent")),
            "handoffs": sum(1 for r in results if r.get("action") == "handoff"),
            "results": results,
        }

    # ---------- 辅助 ----------

    def peek(self) -> dict:
        """只看一眼当前会话状态，不做任何判定与发送。"""
        try:
            snap = self.page.read()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        msgs = snap.get("messages") or []
        closed = bool(msgs) and msgs[-1].get("role") == "system" and \
            bool(self.CLOSED_PAT.search(msgs[-1].get("content") or ""))
        return {
            "ok": True,
            "buyer": snap.get("buyerName") or "",
            "bubbles": snap.get("bubbleCount") or 0,
            "latest": snap.get("latestBuyerMessage") or "",
            "last_role": snap.get("lastRole") or "",
            "closed": closed,
            "input": snap.get("input") or {},
            "messages": msgs[-12:],
        }
