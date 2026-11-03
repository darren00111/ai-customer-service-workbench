# -*- coding: utf-8 -*-
"""状态层自测：防重复回复、暂停、速率限制。

这三条都是安全底线。防重复没做好会重复打扰客户，
限速没做好会触发平台风控 —— 而这两件事都不会报错，只会静默出问题。
"""
from __future__ import annotations

import sys
import tempfile
import json
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine.store import ConversationStore, fingerprint   # noqa: E402

passed, failed = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (passed if cond else failed).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  -> " + detail) if detail and not cond else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fga-store-"))
    st = ConversationStore(tmp / "conversations.json", rate_limit_per_min=3)

    print("=== 1. 防重复回复 ===")
    ok1, fp1, why1 = st.should_reply("热力台球", "160 95斤穿什么码", 7, "10:04")
    check("新消息应当回复", ok1 is True, why1)
    st.mark_replied("热力台球", fp1, "建议 M 码", "160 95斤穿什么码")

    ok2, fp2, why2 = st.should_reply("热力台球", "160 95斤穿什么码", 7, "10:04")
    check("同一条消息不重复回", ok2 is False, why2)
    check("指纹一致", fp1 == fp2)

    reserved = st.reserve_reply("并发买家", "same-fp")
    reserved_again = st.reserve_reply("并发买家", "same-fp")
    check("发送前原子占用可拦截并发重复", reserved is True and reserved_again is False)
    st.release_reply("并发买家", "same-fp")
    check("发送失败释放后允许重试", st.reserve_reply("并发买家", "same-fp") is True)
    st.release_reply("并发买家", "same-fp")

    ok3, fp3, why3 = st.should_reply("热力台球", "那M码有货吗", 9, "10:06")
    check("客户又发新消息则重新触发", ok3 is True, why3)
    check("新消息指纹不同", fp1 != fp3)

    ok4, _, why4 = st.should_reply("热力台球", "", 9, "10:06")
    check("没有买家消息时不动作", ok4 is False, why4)

    print("\n=== 2. 转人工后暂停 ===")
    st.set_paused("热力台球", True)
    ok5, _, why5 = st.should_reply("热力台球", "在吗", 11, "10:08")
    check("已暂停的会话不再自动回", ok5 is False, why5)
    st.set_paused("热力台球", False)
    ok6, _, _ = st.should_reply("热力台球", "在吗", 11, "10:08")
    check("恢复后继续工作", ok6 is True)

    print("\n=== 3. 速率限制 ===")
    for i in range(3):
        allowed, wait = st.allow_send()
        check("第 %d 次发送允许" % (i + 1), allowed is True, "需等 %.1fs" % wait)
        st.note_send()
    allowed, wait = st.allow_send()
    check("超过上限后被拦下", allowed is False)
    check("并给出还需等待的秒数", wait > 0, "wait=%.1f" % wait)

    summary = st.summary()
    check("统计里能看到已发送条数", summary["total_replies"] == 1, str(summary["total_replies"]))
    check("统计里能看到一分钟内发送数", summary["sent_last_minute"] == 3,
          str(summary["sent_last_minute"]))

    print("\n=== 4. 落盘与重载 ===")
    st2 = ConversationStore(tmp / "conversations.json", rate_limit_per_min=3)
    check("重载后仍认得已回过的消息",
          st2.is_duplicate("热力台球", fp1) is True and st2.get("热力台球")["replies"] == 1,
          "is_dup=%s replies=%s" % (st2.is_duplicate("热力台球", fp1),
                                    st2.get("热力台球")["replies"]))
    check("重载后限速窗口仍有效", st2.allow_send()[0] is False)

    print("\n=== 5. 指纹稳定性 ===")
    a = fingerprint("A", "你好", 5, "10:00")
    b = fingerprint("A", "你好", 5, "10:00")
    c = fingerprint("B", "你好", 5, "10:00")
    check("同输入同指纹", a == b)
    check("不同会话不同指纹", a != c)

    print("\n=== 6. 平台消息 ID 与不确定发送 ===")
    stable_a = fingerprint("A", "同一条消息", 3, "10:00", "conv-a", "msg-7788")
    stable_b = fingerprint("A", "同一条消息", 1, "", "conv-a", "msg-7788")
    check("有平台消息 ID 时不受可见气泡数量变化影响", stable_a == stable_b)
    legacy_raw = "conv-old|旧买家|旧消息|4|09:30"
    legacy_fp = hashlib.md5(legacy_raw.encode("utf-8")).hexdigest()[:16]
    old = st.get("旧买家", conversation_id="conv-old")
    old["last_fp"] = legacy_fp
    check("升级后仍识别 1.0 已回复指纹",
          st.should_reply("旧买家", "旧消息", 4, "09:30",
                          conversation_id="conv-old")[0] is False)
    unknown_fp = fingerprint("待核对", "测试", 1, "10:01", "conv-unknown")
    check("不确定发送可先占用", st.reserve_reply("待核对", unknown_fp,
                                           conversation_id="conv-unknown"))
    st.mark_delivery_unknown("待核对", unknown_fp, "可能已发送",
                             conversation_id="conv-unknown")
    st_unknown = ConversationStore(tmp / "conversations.json", rate_limit_per_min=3)
    unknown_conv = st_unknown.get("待核对", conversation_id="conv-unknown")
    check("结果不明跨重启仍禁止自动重发",
          unknown_conv.get("pending_fp") == unknown_fp and
          unknown_conv.get("pending_status") == "delivery_unknown")

    print("\n=== 7. 损坏恢复 ===")
    recover_path = tmp / "recover.json"
    recover = ConversationStore(recover_path)
    recover.set_paused("保护测试", True)
    recover.set_paused("保护测试", False)  # second save creates a known-good backup
    recover_path.write_text("{damaged", encoding="utf-8")
    recovered = ConversationStore(recover_path)
    check("主文件损坏时从备份恢复", recovered.recovery.get("source") == "backup",
          json.dumps(recovered.recovery, ensure_ascii=False))
    check("损坏原件会保留副本", bool(recovered.recovery.get("error")))

    print("\n" + "=" * 46)
    print("通过 %d 项，失败 %d 项" % (len(passed), len(failed)))
    for f in failed:
        print("  失败：%s" % f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
