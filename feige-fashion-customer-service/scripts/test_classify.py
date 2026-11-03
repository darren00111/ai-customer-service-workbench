# -*- coding: utf-8 -*-
"""阶段 2 验收：三类场景分类测试。

覆盖：
  组 A  尺码咨询 · 资料齐全      -> 应给出尺码建议
  组 B  资料缺失                 -> 顾客侧缺失追问 / 商品侧缺失转人工
  组 C  退换货与售后             -> 一律转人工
  组 D  售前其他（面料/库存/发货/版型）
  组 E  歧义词语境对照（起球：疑问 vs 已然）
  组 F  无法判断的兜底
  组 G  无商品卡场景             -> 顾客没点商品卡就直接问
  组 H  有无此商品（顾客只给品类）
  组 I  跨款横向比较             -> 顾客明知有多款，故意要横向比
  组 J  寒暄 / 议价优先级        -> 打招呼要能自己答；真诉求不被寒暄吞掉

验收标准（roadmap.md 阶段 2）：
  1. 三类消息分类 100% 正确
  2. 资料缺失时主动追问身高体重
  3. 尺码措辞用「更建议」，不出现「保证合身」
  4. 无商品卡时，店铺级问题照常回答；歧义时列候选而非空追问
  5. 跨款比较不排序：只摊开维度差异，不出现绝对化用语

用法：
    python test_classify.py
    python test_classify.py --show     # 额外打印完整回复草稿
"""
from __future__ import annotations

import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classify as C  # noqa: E402
import kb            # noqa: E402

# 措辞红线：出现任一即判失败（operating-policy.md）
DISALLOWED = ("保证合身", "百分百", "绝对", "一定能", "肯定能", "包退", "包赔", "无理由赔偿")

# 组 K 用：上一轮比较留下的候选（牛仔裤 vs 西装阔腿裤，材质差异明显）
PENDING_CANDS = [{"product_id": "P1002", "title": "高腰直筒水洗牛仔长裤"},
                 {"product_id": "P1009", "title": "高腰拖地阔腿西装裤"}]

CASES = [
    # ---------------- 组 A 尺码咨询 · 资料齐全 ----------------
    dict(id="A1", group="A 尺码·资料齐全", text="身高160，体重50公斤，买什么码",
         product="P1001", cat="pre_sale_size", act="reply"),
    dict(id="A2", group="A 尺码·资料齐全", text="我身高165 体重58 穿几码",
         product="P1009", cat="pre_sale_size", act="reply"),
    dict(id="A3", group="A 尺码·资料齐全", text="160cm 50kg 这款买多大",
         product="P1005", cat="pre_sale_size", act="reply"),

    # ---------------- 组 B 资料缺失 ----------------
    dict(id="B1", group="B 资料缺失·顾客侧", text="这件有我的码吗",
         product="P1001", cat="pre_sale_size", act="ask_followup",
         missing=["height", "weight"]),
    dict(id="B2", group="B 资料缺失·无商品", text="买什么码",
         product=None, cat="need_product", act="ask_followup"),
    dict(id="B3", group="B 资料缺失·商品侧", text="这个会不会起球",
         product="P1007", cat="handoff_data_gap", act="handoff"),
    dict(id="B4", group="B 资料缺失·商品侧", text="能机洗吗",
         product="P1009", cat="handoff_data_gap", act="handoff"),
    dict(id="B5", group="B 资料缺失·商品侧", text="会缩水吗",
         product="P1001", cat="handoff_data_gap", act="handoff"),

    # ---------------- 组 C 退换货与售后 ----------------
    dict(id="C1", group="C 售后·退换货", text="我要退货，尺码不合适",
         product=None, cat="handoff_after_sale", act="handoff"),
    dict(id="C2", group="C 售后·质量", text="刚收到就破了个洞",
         product=None, cat="handoff_quality", act="handoff"),
    dict(id="C3", group="C 售后·纠纷", text="再不处理我就投诉了",
         product=None, cat="handoff_dispute", act="handoff"),
    dict(id="C4", group="C 售后·要人工", text="转人工",
         product=None, cat="handoff_human_request", act="handoff"),
    dict(id="C5", group="C 售后·退换货", text="这个可以换货吗",
         product=None, cat="handoff_after_sale", act="handoff"),

    # ---------------- 组 D 售前其他 ----------------
    dict(id="D1", group="D 售前·面料", text="什么材质的",
         product="P1001", cat="pre_sale_fabric", act="reply"),
    dict(id="D2", group="D 售前·库存", text="有M码吗",
         product="P1001", cat="pre_sale_stock", act="reply"),
    dict(id="D3", group="D 售前·发货", text="多久能发货",
         product=None, cat="pre_sale_shipping", act="reply"),
    dict(id="D4", group="D 售前·版型", text="版型是宽松的吗",
         product="P1005", cat="pre_sale_fit", act="reply"),

    # ---------------- 组 E 歧义词语境对照 ----------------
    dict(id="E1", group="E 歧义·疑问", text="会不会起球",
         product="P1007", cat="handoff_data_gap", act="handoff"),
    dict(id="E2", group="E 歧义·已然", text="才穿两天就起球了",
         product="P1007", cat="handoff_quality", act="handoff"),

    # ---------------- 组 F 兜底 ----------------
    # F1 必须是不含「有卖/有没有」这类词的纯闲聊，否则会落进 R4.5 而非兜底
    dict(id="F1", group="F 兜底", text="你们家东西挺多的哈",
         product=None, cat="handoff_unknown", act="handoff"),

    # ---------------- 组 G 无商品卡（顾客没点商品卡就直接问）----------------
    # 核心问题：不依赖商品的部分要能答，依赖商品的部分要能自己找出来，
    # 找不到也不能死胡同 —— 有候选就列候选，没候选才追问。
    dict(id="G1", group="G 无商品卡·店铺级可答", text="什么时候发货",
         product=None, cat="pre_sale_shipping", act="reply"),
    dict(id="G2", group="G 无商品卡·店铺级可答", text="支持七天无理由吗",
         product=None, cat="pre_sale_policy", act="reply"),
    dict(id="G3", group="G 无商品卡·店铺级可答", text="能开发票吗",
         product=None, cat="pre_sale_policy", act="reply"),
    dict(id="G4", group="G 无商品卡·店铺级可答", text="店里现在有什么优惠",
         product=None, cat="pre_sale_promo", act="reply"),
    dict(id="G5", group="G 无商品卡·文本能定位", text="碎花的那个裙子能机洗吗",
         product=None, cat="handoff_data_gap", act="handoff"),
    dict(id="G6", group="G 无商品卡·文本能定位", text="阔腿裤多少钱",
         product=None, cat="pre_sale_price", act="reply"),
    dict(id="G7", group="G 无商品卡·歧义列候选", text="高腰裤有M码吗",
         product=None, cat="need_product", act="ask_followup",
         candidates=True),
    dict(id="G8", group="G 无商品卡·纯指代追问", text="这个有M码吗",
         product=None, cat="need_product", act="ask_followup"),
    dict(id="G9", group="G 无商品卡·售后不受影响", text="我要退货",
         product=None, cat="handoff_after_sale", act="handoff"),
    dict(id="G10", group="G 无商品卡·议价", text="能便宜点吗",
         product=None, cat="handoff_bargain", act="handoff"),
    dict(id="G11", group="G 无商品卡·上下文继承", text="有M码吗",
         product=None, context="P1009", cat="pre_sale_stock", act="reply"),
    dict(id="G12", group="G 无商品卡·上下文解歧义", text="高腰裤有M码吗",
         product=None, context="P1009", cat="pre_sale_stock", act="reply"),
    dict(id="G13", group="G 无商品卡·商品卡未入库", text="有M码吗",
         product="P9999", cat="handoff_data_gap", act="handoff"),

    # ---------------- 组 H 有无此商品（顾客只给品类，不给商品卡）----------------
    dict(id="H1", group="H 有无此商品", text="你们家有卖羽绒服吗",
         product=None, cat="pre_sale_availability", act="reply"),
    dict(id="H2", group="H 有无此商品", text="裙子呢",
         product=None, cat="need_product", act="ask_followup"),
    dict(id="H3", group="H 有无此商品", text="你们家有没有卖别的东西",
         product=None, cat="need_product", act="ask_followup"),
    # 顾客喊人：必须判成「要人工」，不能落到「意图不明」——
    # 通知人工时理由要写「顾客要求人工」，不是「意图不明」
    dict(id="H4", group="H 有无此商品", text="人呢",
         product=None, cat="handoff_human_request", act="handoff"),

    # ---------------- 组 I 跨款横向比较 ----------------
    # 顾客明知有多款、故意要横向比，不是在问「哪一款」。判成「请点选商品」
    # 是误判：他要的是结论，不是选项列表。
    # 硬约束：回复里不许出现绝对化排序用语（广告法红线 + 我们只有成分数据，
    # 没资格对顾客的手感偏好下判断）。
    dict(id="I1", group="I 跨款比较", text="你们现在哪个裤子材质是最好的",
         product=None, cat="pre_sale_compare", act="reply", candidates=True),
    dict(id="I2", group="I 跨款比较", text="这两款裤子有什么区别",
         product=None, cat="pre_sale_compare", act="reply", candidates=True),
    dict(id="I3", group="I 跨款比较", text="哪条裤子尺码全",
         product=None, cat="pre_sale_compare", act="reply", candidates=True),
    # 没命中任何具体商品，靠品类词圈范围也能比（别反问他选哪款）
    dict(id="I4", group="I 跨款比较", text="你们家哪个裙子版型好",
         product=None, cat="pre_sale_compare", act="reply", candidates=True),
    dict(id="I5", group="I 跨款比较·护栏", text="哪个颜色有货",
         product=None, cat="need_product", act="ask_followup"),
    dict(id="I6", group="I 跨款比较·护栏", text="哪个码最合适",
         product=None, cat="need_product", act="ask_followup"),
    # 有比较意图但圈不出品类范围 -> 宁可退回追问，不要全店乱比
    dict(id="I7", group="I 跨款比较·护栏", text="哪款面料更舒服一点",
         product=None, cat="need_product", act="ask_followup"),

    # ---------------- 组 J 寒暄 / 议价优先级 ----------------
    # 寒暄必须自己答掉。转人工是负体验：顾客只是打个招呼却等来
    # 「帮您转接人工」，会觉得店里没人。
    dict(id="J1", group="J 寒暄", text="你好",
         product=None, cat="pre_sale_greeting", act="reply"),
    dict(id="J2", group="J 寒暄", text="在吗",
         product=None, cat="pre_sale_greeting", act="reply"),
    dict(id="J3", group="J 寒暄", text="在不在",
         product=None, cat="pre_sale_greeting", act="reply"),
    # 护栏 1：寒暄绝不能吞掉真诉求。「你好」+ 议价仍然是议价。
    dict(id="J4", group="J 寒暄·护栏", text="你好，这个能便宜吗",
         product=None, cat="handoff_bargain", act="handoff"),
    dict(id="J5", group="J 寒暄·护栏", text="你好我要退货",
         product=None, cat="handoff_after_sale", act="handoff"),
    # 护栏 2：议价不因「没指明哪款」降级成问询 —— 让利必须人工决定，
    # 让顾客先点款只会白绕一圈，还让他以为有得谈。
    dict(id="J6", group="J 寒暄·护栏", text="你们家那个裤子还能便宜吗",
         product=None, cat="handoff_bargain", act="handoff"),
    # 护栏 3：带诉求的「在吗」不是寒暄
    dict(id="J7", group="J 寒暄·护栏", text="在吗这个有M码吗",
         product=None, cat="need_product", act="ask_followup"),

    # ---- 组 K：偏好收尾（上一轮比过，顾客这轮说侧重）----
    # 背景：比较回复结尾问「您更看重哪一点？」，顾客照着答了。
    # 没有这条分支时，这类应答会被兜底判成「意图不明」转人工——
    # 明明顺着我们的话在答，却把人踢走。
    dict(id="K1", group="K 偏好收尾", text="我需要更凉爽一点的",
         product=None, pending=PENDING_CANDS,
         cat="pre_sale_preference", act="reply"),
    dict(id="K2", group="K 偏好收尾", text="要挺括耐穿的那种",
         product=None, pending=PENDING_CANDS,
         cat="pre_sale_preference", act="reply"),
    dict(id="K3", group="K 偏好收尾", text="凉快一点的比较好",
         product=None, pending=PENDING_CANDS,
         cat="pre_sale_preference", act="reply"),
    # 护栏 1：没有上一轮候选就不能猜。「凉爽」单独出现时我们根本不知道在比哪几款。
    dict(id="K4", group="K 偏好收尾·护栏", text="我需要更凉爽一点的",
         product=None, pending=None,
         cat="handoff_unknown", act="handoff"),
    # 护栏 2：纤维词表只收完整词。「麻烦」里那个「麻」不能当成亚麻。
    dict(id="K5", group="K 偏好收尾·护栏", text="麻烦你了",
         product=None, pending=PENDING_CANDS,
         cat="handoff_unknown", act="handoff"),
    # 护栏 3：两个方向都要，说明顾客自己也没定 -> 退回列候选，别硬选也别踢走
    dict(id="K6", group="K 偏好收尾·护栏", text="又要凉快又要挺括",
         product=None, pending=PENDING_CANDS,
         cat="need_product", act="ask_followup"),
]


def check(case, d):
    """返回问题列表，空列表表示通过。"""
    errs = []
    if d["category"] != case["cat"]:
        errs.append("分类 %s != 期望 %s" % (d["category"], case["cat"]))
    if d["action"] != case["act"]:
        errs.append("动作 %s != 期望 %s" % (d["action"], case["act"]))

    if case.get("missing"):
        for m in case["missing"]:
            if m not in (d.get("missing") or []):
                errs.append("缺少 missing 项 %s" % m)

    draft = d.get("draft_reply") or ""
    if d["action"] in ("reply", "ask_followup") and not draft:
        errs.append("草稿为空")
    if d["action"] == "reply":
        for bad in DISALLOWED:
            if bad in draft:
                errs.append("措辞违规：出现「%s」" % bad)
    if d["category"] == "pre_sale_size" and d["action"] == "reply":
        if "更建议" not in draft:
            errs.append("尺码建议未使用「更建议」措辞")
    # 跨款比较：顾客可以说「最好」，我们的回复不能跟着排序。
    # 我们只有成分/克重这类客观数据，对顾客手感偏好没有判断依据。
    if d["category"] == "pre_sale_compare":
        for bad in ("最好", "最佳", "最优", "第一", "绝对"):
            if bad in draft:
                errs.append("比较回复出现绝对化排序用语「%s」" % bad)
    if d["action"] == "handoff" and not d.get("handoff_reason"):
        errs.append("转人工但未给出 handoff_reason")
    # 歧义场景必须给出候选，不能让顾客对着空追问重述一遍
    if case.get("candidates") and not (d.get("candidates") or []):
        errs.append("期望给出候选列表，但 candidates 为空")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="打印完整回复草稿")
    ap.add_argument("--store", default=kb.DEFAULT_STORE_ID)
    args = ap.parse_args()

    store, products = kb.load_kb(args.store)
    print("知识库：%s（%s），商品 %d 件\n" % (args.store, store.get("store_name"), len(products)))

    passed = 0
    rows = []
    failures = []

    last_group = None
    for case in CASES:
        if case["group"] != last_group:
            print("── %s ──" % case["group"])
            last_group = case["group"]

        d = C.classify(case["text"], store, products,
                       case.get("product"), case.get("context"),
                       case.get("pending"))
        errs = check(case, d)
        ok = not errs
        passed += ok

        mark = "✓" if ok else "✗"
        print("  %s %-3s  %-22s %-18s  %s"
              % (mark, case["id"], case["cat"], d["category"], case["text"]))
        if errs:
            for e in errs:
                print("        └─ %s" % e)
        if args.show and d.get("draft_reply"):
            print("        ↳ %s" % d["draft_reply"])

        rows.append((case, d))
        if not ok:
            failures.append((case, errs))

    total = len(CASES)
    print("\n" + "=" * 62)
    print("通过 %d / %d" % (passed, total))

    # 分组统计
    from collections import Counter
    ok_by_group = Counter()
    all_by_group = Counter()
    for case, d in rows:
        all_by_group[case["group"]] += 1
        if not check(case, d):
            ok_by_group[case["group"]] += 1
    for g in sorted(all_by_group):
        print("  %-24s %d/%d" % (g, ok_by_group[g], all_by_group[g]))

    # 关键验收点单独点名
    print("\n关键验收点：")
    b1 = next(d for c, d in rows if c["id"] == "B1")
    print("  资料缺失时主动追问身高体重 ...... %s"
          % ("✓" if b1["category"] == "pre_sale_size"
             and b1["action"] == "ask_followup" else "✗"))
    a1 = next(d for c, d in rows if c["id"] == "A1")
    print("  尺码建议措辞用「更建议」 ........ %s"
          % ("✓" if "更建议" in (a1.get("draft_reply") or "") else "✗"))
    c_all = [d for c, d in rows if c["group"].startswith("C ")]
    print("  退换货/售后 100%% 转人工 .......... %s"
          % ("✓" if all(d["action"] == "handoff" for d in c_all) else "✗"))

    # 无商品卡专项：店铺级问题不能被「找不到商品」拖累
    g_store = [d for c, d in rows if c["id"] in ("G1", "G2", "G3", "G4")]
    print("  无商品卡时店铺级问题照常回答 .... %s"
          % ("✓" if all(d["action"] == "reply" for d in g_store) else "✗"))
    g7 = next(d for c, d in rows if c["id"] == "G7")
    print("  歧义时列候选而非空追问 .......... %s"
          % ("✓" if (g7.get("candidates") or []) else "✗"))
    g_ctx = [d for c, d in rows if c["id"] in ("G11", "G12")]
    print("  会话级商品槽位可继承/解歧义 ..... %s"
          % ("✓" if all(d["action"] == "reply" for d in g_ctx) else "✗"))
    j_greet = [d for c, d in rows if c["group"] == "J 寒暄"]
    print("  寒暄自行应答、不转人工 ........ %s"
          % ("✓" if j_greet and all(d["action"] == "reply" for d in j_greet) else "✗"))

    # 动作分布 —— 直接反映「自动答复率」
    dist = Counter()
    for _, d in rows:
        dist[d["action"]] += 1
    print("\n动作分布（共 %d 例）：" % total)
    for act, label in (("reply", "可直接回复"), ("ask_followup", "追问顾客"),
                       ("handoff", "转人工")):
        n = dist[act]
        bar = "█" * int(round(n / float(total) * 30))
        print("  %-10s %2d 例 %5.1f%%  %s" % (label, n, n * 100.0 / total, bar))

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
