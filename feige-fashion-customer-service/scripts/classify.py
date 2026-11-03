# -*- coding: utf-8 -*-
"""决策逻辑 —— 把 operating-policy.md 的规则变成可执行判断。

输入：顾客最新消息（+ 可选的飞鸽商品卡 / 会话级商品槽位）
输出：分类 / 动作 / 依据 / 回复草稿

设计立场（不可动摇）：
    保守优先 —— 宁可转人工，不可乱答。

规则优先级（先命中先返回，顺序即优先级）：
    R1  明确要人工        -> handoff_human_request
    R2a 具名政策咨询      -> pre_sale_policy      （可答，不需要商品）
    R2  售后 / 质量 / 纠纷 -> handoff_after_sale / handoff_quality / handoff_dispute
    R3  库存 / 颜色        -> pre_sale_stock       （全码缺货 -> handoff_stock_out）
    R3.5 价格 / 优惠 / 议价 -> pre_sale_price / pre_sale_promo / handoff_bargain
    R4  尺码               -> pre_sale_size
    R5  面料               -> pre_sale_fabric
    R6  版型               -> pre_sale_fit
    R7  发货 / 物流        -> pre_sale_shipping    （可答，不需要商品）
    R8  兜底               -> handoff_unknown

动作（action）：
    reply        可直接回复
    ask_followup 追问顾客一个关键信息（顾客侧信息缺失 / 商品定位不到）
    handoff      转人工（商品侧资料缺失 / 售后 / 纠纷 / 无法判断）

不依赖商品的分类（顾客不点商品卡也能答）：
    pre_sale_policy / pre_sale_shipping / pre_sale_promo
    handoff_human_request / handoff_after_sale / handoff_quality / handoff_dispute

用法：
    python classify.py --text "身高160，体重50公斤，买什么码" --product P1009
    python classify.py --text "有M码吗" --context P1001
    python classify.py --text "我要退货" --json
    python classify.py --batch cases.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb  # noqa: E402

# ===========================================================================
# 一、词表与规则
# ===========================================================================

# --- R1 明确要人工 -----------------------------------------------------------
HUMAN_WORDS = (
    "人工服务", "人工客服", "转人工", "找人工", "要人工", "人工", "真人客服",
    "找个人",
    # 顾客喊人 = 要人工，不是「意图不明」。实测里「人呢」被判成 unknown，
    # 分类错不要紧，但通知人工时写的理由就误导人了。
    #
    # 这里只收**带催促/不耐**的喊法。「在吗」「客服在吗」不收——那是寒暄，
    # 回一句「在的」才是最优解；转人工反而是多一次无谓介入，顾客还得多等。
    "人呢", "客服呢", "有人没", "没人吗", "怎么没人", "说话",
)

# --- R1g 寒暄 ----------------------------------------------------------------
# 「你好」「在吗」必须能自己答掉。转人工是负体验：顾客只是打个招呼，
# 却等来「帮您转接人工」，会觉得店里没人。
#
# 这个分支**放在所有强规则之后**（R8 之前），因为「你好，我要退货」里的
# 「你好」绝不能把售后吞掉——那才是真的会出事的误判。
GREET_WORDS = ("你好", "您好", "你好呀", "你好啊", "您好呀", "哈喽", "哈啰",
               "嗨", "在吗", "在么", "在不在", "有人在吗", "客服在吗",
               "有客服吗", "老板在吗", "早上好", "中午好", "下午好",
               "晚上好", "hello", "hi", "哈罗", "在么在么")
GREET_MAX_LEN = 14      # 去标点后超过这个长度，就不算「整条都是寒暄」

# --- R2 售后 / 质量 / 纠纷 ---------------------------------------------------
# (a) 无条件转人工：交易与纠纷类
AFTER_SALE_WORDS = (
    "退货", "换货", "退款", "退钱", "退运费", "退差价", "拒收", "补发",
    "少件", "错发", "漏发", "多发", "发错", "缺件", "退单", "取消订单",
)
DISPUTE_WORDS = (
    "投诉", "差评", "举报", "赔偿", "平台介入", "小二介入", "客服介入",
    "工商", "消协", "12315", "315", "曝光", "律师", "起诉", "法院", "维权",
)
SERVICE_WORDS = ("售后", "维修", "返修", "质保", "保修")

# (b) 质量词：需结合语境判断是「售前咨询」还是「售后抱怨」
QUALITY_WORDS = (
    "起球", "掉色", "褪色", "缩水", "变形", "开线", "脱线", "破洞", "破损",
    "坏了", "烂了", "破了", "有洞", "裂了", "开胶", "掉钻", "少扣",
    "污渍", "脏", "发霉", "异味", "刺鼻", "过敏", "受伤",
    "划伤", "掉毛", "掉绒", "勾丝", "做工差", "质量差", "质量问题",
)

# 疑问标记：出现即判定为「在问」，不是「发生了」
INQUIRY_MARKERS = (
    "吗", "嘛", "么", "会不会", "是否", "能不能", "可不可以", "怎么样",
    "怎样", "如何", "?", "？", "多不多",
)
# 已然标记：出现即判定为「已经发生」
PAST_MARKERS = (
    "了", "已经", "才穿", "刚收到", "收到就", "买回来", "穿了一次", "穿了两次",
    "洗过", "洗了", "两天就", "三天就", "昨天", "上次", "这次",
)

# (c) 性能承诺类：必须资料明确支持，否则转人工
PERFORMANCE_WORDS = (
    "起球", "掉色", "褪色", "缩水", "变形", "掉毛", "掉绒", "勾丝",
    "透光", "不透", "静电", "防皱", "免烫", "抗皱",
)

# --- R3 库存 / 颜色 ----------------------------------------------------------
STOCK_WORDS = (
    "有货", "现货", "库存", "缺货", "补货", "售罄", "下架", "断码",
    "什么颜色", "哪些颜色", "几个颜色", "有几种颜色",
)
CODE_RE = re.compile(r"[A-Za-z0-9]{1,3}\s*码")

# --- R4 尺码 -----------------------------------------------------------------
SIZE_WORDS = (
    "尺码", "码数", "码子", "买什么码", "买多大", "穿几码", "选几码", "多大码",
    "哪个码", "什么码", "号型", "偏大", "偏小", "卡码", "尺码表", "身高",
    "体重", "胸围", "腰围", "臀围", "肩宽", "衣长", "裤长", "裙长", "腿长",
)

# --- R4.5 有无此商品 ---------------------------------------------------------
# 顾客只问「你们家有卖羽绒服吗」——已经认出商品却答不上来是最亏的，
# 这类必须能答。放在尺码/库存之后，免得把「有没有 M 码」吞进来。
AVAIL_WORDS = (
    "有卖", "有没有卖", "卖不卖", "有没有这款", "有这款", "有没有", "有吗",
    "在售", "还在卖", "还有这个", "有没有货",
)

# --- R5 面料 -----------------------------------------------------------------
FABRIC_WORDS = (
    "面料", "材质", "成分", "什么料", "哪种料", "克重", "弹力", "弹性",
    "透气", "闷不闷", "保暖", "里料", "内衬", "填充", "洗护", "怎么洗",
    "能机洗", "水洗", "熨烫", "厚不厚", "薄不薄", "多厚",
)

# --- R6 版型 -----------------------------------------------------------------
FIT_WORDS = (
    "版型", "宽松", "修身", "显瘦", "显胖", "廓形", "垂坠", "挺括",
    "上身", "偏瘦", "肥大", "收腰",
)

# --- R7 发货 -----------------------------------------------------------------
SHIPPING_WORDS = (
    "多久", "几天", "发货", "快递", "包邮", "运费", "什么时候发", "物流",
    "什么时候能到", "顺丰",
)

# --- R2a 店铺政策（具名政策咨询，可答，不需要商品） --------------------------
# 只收「明确在问政策」的说法。带退/换字的泛问（「这个可以换货吗」）仍走售后转人工，
# 因为无法区分是问政策还是在说具体订单。
POLICY_WORDS = (
    "七天无理由", "7天无理由", "无理由退换", "退换货政策", "退换政策",
    "售后政策", "退换规则", "发票", "开票", "正品", "假一赔", "保障",
)
# 第一人称行动词：出现即说明顾客在说自己的订单，不能当政策咨询答
ACTION_MARKERS = ("我要", "我想", "帮我", "给我", "申请", "要退", "想退", "要换", "想换")

# --- R3.5 价格 / 优惠 --------------------------------------------------------
PRICE_WORDS = (
    "多少钱", "价格", "价位", "售价", "到手价", "原价", "现价",
    "几块钱", "多少块", "卖多少", "什么价", "贵不贵", "降价",
)
PROMO_WORDS = (
    "优惠", "活动", "打折", "折扣", "优惠券", "领券", "满减", "促销", "有没有券",
)
# 议价类：涉及让利，一律转人工（私下降价是事故）
BARGAIN_WORDS = (
    "便宜点", "便宜些", "便宜一点", "能便宜", "少点", "少一点",
    "让点", "让一点", "抹零", "最低多少", "底价", "给个价",
)

# --- R0 跨款横向比较 ----------------------------------------------------------
# 「你们现在哪个裤子材质最好」不是在问「哪一款」，而是明知有多款、故意要
# 横向比。把它当成「请点选商品」是误判：顾客要的是结论，不是选项列表。
#
# 但**不能输出「X 最好」**。两个原因，都不是洁癖：
#   1) 绝对化用语在广告法上是明确红线（「国家级」「最佳」「第一」同档）；
#   2) 我们只有成分/克重这类客观数据，没有顾客手感偏好，本来就没资格排序。
# 正确姿势：按维度把客观差异摊开 + 给出「在意什么就选什么」的映射 + 问侧重。
# 注意：这里刻意**不收裸「哪个」**。「哪个颜色有货」「哪个码最合适」里的
# 「哪个」修饰的是属性不是商品，属于单款问题；只有带商品量词的
# （哪款 / 哪条 / 哪件 / 哪种）才是在问「几款里挑哪个」。
SELECT_WORDS = ("哪款", "哪一款", "哪一条", "哪条", "哪一种", "哪种",
                "哪一件", "哪件", "哪一个", "推荐哪", "买哪", "选哪", "要哪")
SUPER_WORDS = ("最好", "最舒服", "最凉快", "最凉", "最保暖", "最暖", "最耐",
               "最厚", "最薄", "最显瘦", "最划算", "最强", "最结实", "最百搭")
COMPARE_WORDS = ("哪个更", "哪个好", "对比", "比较一下", "有什么区别",
                 "有啥区别", "差别在哪", "不一样在哪", "哪个值得", "哪个推荐")

# 状态文案
STATUS_TEXT = {
    "on_sale": "在售",
    "sold_out": "已售罄",
    "pre_sale": "预售中",
    "off_shelf": "已下架",
}


# ===========================================================================
# 二、文本处理
# ===========================================================================
def norm(s: str) -> str:
    """归一化：全角转半角、去空白。保留中文标点（问号对判定有意义）。"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", "", s)


def has_any(t: str, words) -> str | None:
    for w in words:
        if w in t:
            return w
    return None


def is_past_tense(t: str) -> bool:
    """判断质量词是在描述「已发生」还是「在询问」。

    疑问标记优先：'会不会起球' 是问，'起球了' 是抱怨。
    """
    if any(m in t for m in INQUIRY_MARKERS):
        return False
    return any(m in t for m in PAST_MARKERS)


def hit_stock(t: str) -> bool:
    if any(w in t for w in STOCK_WORDS):
        return True
    # 「有 M 码吗」「还有 27 码么」——需要同时出现尺码码点与 有/还有
    if CODE_RE.search(t) and re.search(r"(有|还有|剩|没)", t):
        return True
    if re.search(r"还有[^，。?？]{0,6}(吗|么)", t):
        return True
    return False


def hit_size(t: str) -> bool:
    if any(w in t for w in SIZE_WORDS):
        return True
    # 「码」字本身是很强的尺码信号（排除密码/验证码等别义）
    if "码" in t and not any(x in t for x in
                             ("密码", "验证码", "二维码", "条形码", "扫码", "号码", "筹码")):
        return True
    if re.search(r"\d{2,3}\s*(cm|厘米|公分|kg|公斤|千克|斤)", t):
        return True
    return False


def parse_body(t: str):
    """从文本里抽身高(cm) 与体重(kg)。返回 (height, weight)，缺为 None。"""
    height = weight = None

    m = re.search(r"身高\s*[:：]?\s*(\d{2,3})", t)
    if m:
        height = int(m.group(1))

    # 「斤」必须先判，否则 100 斤会被当成 100 公斤
    m = re.search(r"体重\s*[:：]?\s*(\d{2,3})\s*(?:市斤|斤)", t)
    if m:
        weight = round(int(m.group(1)) / 2.0, 1)
    else:
        m = re.search(r"体重\s*[:：]?\s*(\d{2,3})", t)
        if m:
            weight = int(m.group(1))

    for m in re.finditer(r"(\d{2,3})\s*(?:cm|厘米|公分)", t):
        v = int(m.group(1))
        if height is None and 130 <= v <= 210:
            height = v
    for m in re.finditer(r"(\d{2,3})\s*(?:kg|公斤|千克)", t):
        v = int(m.group(1))
        if weight is None and 30 <= v <= 150:
            weight = v
    for m in re.finditer(r"(\d{2,3})\s*斤", t):
        v = int(m.group(1))
        if weight is None and 60 <= v <= 300:
            weight = round(v / 2.0, 1)

    # 裸数字兜底：身高区间 140~210，体重区间 35~130，两者不重叠
    if height is None or weight is None:
        for v in (int(x) for x in re.findall(r"\d{2,3}", t)):
            if height is None and 140 <= v <= 210:
                height = v
            elif weight is None and 35 <= v <= 130:
                weight = v
    return height, weight


# ===========================================================================
# 三、尺码推荐
# ===========================================================================
W_HEIGHT, W_WEIGHT = 1.0, 1.0
CRITICAL_GAP = 3.0          # 最优与次优距离差小于此值 -> 判为「临界码」


def recommend_size(height, weight, guide):
    """按中心点距离匹配通用尺码对照表。返回 dict 或 None。"""
    rows = (guide or {}).get("table") or []
    if not rows or height is None or weight is None:
        return None
    scored = []
    for r in rows:
        d = abs(height - float(r.get("height", 0))) * W_HEIGHT \
            + abs(weight - float(r.get("weight", 0))) * W_WEIGHT
        scored.append((d, r.get("label")))
    scored.sort(key=lambda x: x[0])
    best_d, best = scored[0]
    second_d, second = (scored[1] if len(scored) > 1 else (999.0, None))
    return {
        "size": best,
        "backup": second,
        "critical": (second_d - best_d) < CRITICAL_GAP,
        "distance": round(best_d, 2),
    }


_LETTER_ORDER = ("XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL")


def align_size(label: str, product: dict):
    """把通用对照的尺码标签对齐到该商品实际有的尺码。

    返回 (最终尺码 | None, 说明 | None)
    对齐不了时返回 (None, 原因) —— 绝不硬套。
    """
    sizes = kb.all_sizes(product)
    if not sizes:
        return None, "该商品没有尺码表"
    if label in sizes:
        return label, None

    if label in _LETTER_ORDER and all(s in _LETTER_ORDER for s in sizes):
        idx = _LETTER_ORDER.index(label)
        best = min(sizes, key=lambda s: abs(_LETTER_ORDER.index(s) - idx))
        return best, "该商品没有 %s 码，已按最接近的 %s 码给出" % (label, best)

    # 数字码体系（牛仔裤 26-30 等）无法从通用对照直接换算
    return None, "该商品用的是另一套尺码体系，需按围度对照"


# ===========================================================================
# 四、回复草稿
# ===========================================================================
def fmt_size_row(product: dict, size: str) -> str:
    sc = product.get("size_chart") or {}
    unit = sc.get("unit") or "cm"
    cat = product.get("category") or ""
    if "裙" in cat:
        length_label = "裙长"
    elif "裤" in cat:
        length_label = "裤长"
    else:
        length_label = "衣长"
    label_map = {
        "bust": "胸围", "waist": "腰围", "hip": "臀围",
        "length": length_label, "shoulder": "肩宽", "sleeve": "袖长",
    }
    for row in sc.get("rows") or []:
        if row.get("size") == size:
            parts = []
            for k in ("bust", "waist", "hip", "length", "shoulder", "sleeve"):
                v = row.get(k)
                if v:
                    parts.append("%s %s%s" % (label_map[k], v, unit))
            return "、".join(parts)
    return ""


def basis_note(product: dict) -> str:
    """尺码口径说明。必须写明是成衣尺寸还是净尺寸，否则尺码建议就是错的。"""
    basis = (product.get("size_chart") or {}).get("basis") or ""
    if "净" in basis:
        return "这款的尺码表是净尺寸，也就是按身体围度对照的"
    if "成衣" in basis:
        return "这款的尺码表是成衣尺寸，也就是衣服平铺量的，穿着会留出活动余量"
    return ""


def reply_size(product, height, weight, guide, store):
    """尺码咨询的回复草稿。措辞只用「更建议」「可以参考」，禁止「保证合身」。"""
    title = product.get("title") or "这款"
    rec = recommend_size(height, weight, guide)

    if rec is None:
        # 有商品但没有通用对照表 -> 给出尺码表数据 + 让顾客自行对照
        sizes = kb.all_sizes(product)
        head = "亲，这款%s目前有这些尺码：%s。" % (title, "、".join(sizes))
        note = basis_note(product)
        if note:
            head += note + "。"
        return head + "您可以对照一下商品尺码表，如果方便告诉我身高体重，我也可以帮您参考一下～"

    label, note = align_size(rec["size"], product)
    if label is None:
        sizes = kb.all_sizes(product)
        return (
            "亲，这款%s用的是%s码（%s），和常见的 S/M/L 不太一样，"
            "更建议按腰围对照：可选尺码 %s。方便的话告诉我您的腰围，我帮您参考一下～"
            % (title, "数字", note, "、".join(sizes))
        )

    parts = ["亲，按您身高 %scm、体重 %skg，这款%s更建议选【%s 码】。"
             % (height, weight, title, label)]
    row = fmt_size_row(product, label)
    if row:
        parts.append("%s 码的尺码参考：%s。" % (label, row))
    if note:
        parts.append(note + "。")
    if rec["critical"] and rec.get("backup"):
        parts.append("另外 %s 码也比较接近，如果您喜欢宽松一点可以看看 %s 码～"
                     % (rec["backup"], rec["backup"]))
    if basis_note(product):
        parts.append(basis_note(product) + "。")
    parts.append("有任何不确定随时问我～")
    return "".join(parts)


def reply_fabric(product, store):
    m = product.get("material") or {}
    comp = m.get("composition") or []
    if not comp:
        return None
    comp_s = "、".join("%s %s%%" % (c.get("fiber"), c.get("percent"))
                       for c in comp if c.get("fiber"))
    bits = ["亲，这款%s的面料是%s" % (product.get("title"), comp_s)]
    if m.get("elasticity"):
        bits.append("，%s" % m["elasticity"])
    if m.get("thickness"):
        bits.append("，厚薄属%s" % m["thickness"])
    if m.get("weight_gsm"):
        bits.append("，克重约 %sg/㎡" % m["weight_gsm"])
    tail = "。"
    if m.get("lining"):
        tail += "里料为%s。" % m["lining"]
    bits.append(tail)
    return "".join(bits)


def reply_stock(product, store, text=""):
    title = product.get("title")
    status = STATUS_TEXT.get(product.get("status"), product.get("status"))
    sizes = kb.all_sizes(product)
    avail = kb.available_sizes(product)
    sold = kb.sold_sizes(product)
    variants = product.get("variants") or []
    colors = [v.get("color") for v in variants if v.get("color")]

    # 顾客点名了具体尺码/颜色 -> 直接给结论，不要糊一坨清单
    asked_sizes = []
    for m in re.finditer(r"([A-Za-z0-9]{1,3})\s*码", text or ""):
        s = m.group(1).upper()
        if s in sizes and s not in asked_sizes:
            asked_sizes.append(s)
    asked_colors = [c for c in colors if c and c in (text or "")]

    parts = ["亲，这款%s目前状态是「%s」。" % (title, status)]

    if asked_sizes:
        for s in asked_sizes:
            detail = []
            for v in variants:
                q = (v.get("sizes") or {}).get(s)
                if q is None:
                    continue
                c = v.get("color") or ""
                if asked_colors and c not in asked_colors:
                    continue
                detail.append("%s%s" % (c, "有货" if int(q) > 0 else "已缺货"))
            if detail:
                parts.append("%s 码：%s。" % (s, "、".join(detail)))
            else:
                parts.append("%s 码目前没有。" % s)
        return "".join(parts)

    if not avail:
        # 不要一边说「可选颜色」一边说「全码缺货」——那是自相矛盾的
        parts.append("目前所有尺码都没有现货，具体补货时间建议联系人工核实。")
        return "".join(parts)

    if colors:
        parts.append("可选颜色：%s。" % "、".join(colors))
    parts.append("有货尺码：%s。" % "、".join(avail))
    if sold:
        parts.append("其中 %s 码目前已缺货。" % "、".join(sorted(sold)))
    if product.get("status") == "pre_sale":
        parts.append("这款是预售，下单后按商品页标注的时间发货。")
    return "".join(parts)


def reply_fit(product, store):
    f = product.get("fit") or {}
    bits = []
    seen = set()
    for key, tpl in (("fit_type", "版型%s"),
                     ("silhouette", "廓形%s"),
                     ("waistline", "腰位%s")):
        v = f.get(key)
        if v and v not in seen:
            bits.append(tpl % v)
            seen.add(v)
    if not bits:
        return None
    s = "亲，这款%s是%s的。" % (product.get("title"), "、".join(bits))
    if f.get("notes"):
        s += f["notes"] + "。"
    return s


def reply_shipping(store):
    sh = store.get("shipping") or {}
    bits = []
    if sh.get("lead_time"):
        bits.append(sh["lead_time"])
    if sh.get("carriers"):
        bits.append("默认%s" % "／".join(sh["carriers"]))
    if sh.get("free_shipping_note"):
        bits.append(sh["free_shipping_note"])
    s = "亲，" + "，".join(bits) + "。" if bits else "亲，发货时效这边帮您确认一下～"
    if sh.get("pre_sale_note"):
        s += sh["pre_sale_note"] + "。"
    return s


def reply_price(product, store, text=""):
    """价格咨询。只答商品售价 + 店铺公开优惠；议价不走这里。"""
    parts = []
    if product and product.get("price_cny"):
        parts.append("亲，这款%s售价 %s 元。" % (product["title"], product["price_cny"]))
    promo = store.get("promotions") or {}
    if promo.get("note") and (not product or has_any(text, PROMO_WORDS)):
        parts.append(promo["note"] + "。")
    if not parts:
        return None
    parts.append("最终以商品页显示的到手价为准～")
    return "".join(parts)


def reply_availability(product, store):
    """顾客问「你们家有卖 X 吗」。已经认出商品，就明确答「有」并给关键信息。"""
    title = product.get("title")
    price = product.get("price_cny")
    avail = kb.available_sizes(product)
    status = STATUS_TEXT.get(product.get("status"), product.get("status"))

    parts = ["亲，有的，我们家有这款「%s」。" % title]
    if price:
        parts.append("售价 %s 元。" % price)
    if avail:
        parts.append("目前有货尺码：%s。" % "、".join(avail))
    else:
        parts.append("这个款目前暂时没有现货，补货时间我帮您确认一下哈～")
    if status and status not in ("在售", "正常"):
        parts.append("（当前状态：%s）" % status)
    return "".join(parts)


def reply_policy(store):
    pol = store.get("policies") or {}
    parts = [pol[k] for k in ("return_policy", "invoice", "authenticity")
             if pol.get(k)]
    if not parts:
        return None
    return "亲，" + "".join(parts)


# ===========================================================================
# 五、主决策
# ===========================================================================
# ---------------------------------------------------------------------------
# R0 跨款横向比较
# ---------------------------------------------------------------------------
# 维度优先级：顾客说了哪个维度就比哪个维度，都没说就兜底到面料。
COMPARE_DIMS = (
    ("fabric", FABRIC_WORDS),
    ("fit", FIT_WORDS),
    ("size", SIZE_WORDS),
    ("price", PRICE_WORDS),
    ("stock", STOCK_WORDS),
)
DIM_LABEL = {"fabric": "面料", "fit": "版型", "size": "尺码",
             "price": "价格", "stock": "库存颜色"}

# 维度 → 「在意什么就选什么」的客观映射。
# 一律不出现「最 / 第一 / 最佳」，只做属性到需求的对应——排序是主观的，
# 我们的资料只支持描述，不支持评价。
DIM_GUIDE = {
    "fabric": "想更挺括耐穿，就选成分里棉占比高的那款；"
              "想更垂顺轻凉，就看粘胶/聚酯占比高的那款",
    "fit": "想利落一点选直筒/常规版型，想遮腿型、显腿长选阔腿/宽松版型",
    "size": "两款尺码体系不一样，按您平时的码对照上面的表选更稳",
    "price": "两款售价列在上面了，预算优先就选低的那款",
    "stock": "在意颜色的话，挑可选颜色更全、有您想要颜色的那款更稳妥",
}


def compare_dim(t: str):
    for name, words in COMPARE_DIMS:
        if has_any(t, words):
            return name
    return "fabric"


# 「哪 + 量词」的通用写法。刻意不做成裸「哪」，否则「哪天发货」也会命中。
WHICH_RE = re.compile(r"哪(?:个|款|一条|条|一件|件|一种|种|一个)")


def _which_kind(t: str) -> bool:
    """「哪X + 品类词」= 在某个品类内部横向挑。

    这是跨款比较的**充分形态**：「哪条裤子尺码全」问的是裤子之间比，
    跟「哪个码最合适」（问码本身）是两回事。品类词缺失时（「这几款哪个显瘦」）
    就不知道比什么，宁可退回追问。
    """
    if not WHICH_RE.search(t):
        return False
    return has_any(t, kb.KIND_TOKENS) is not None


def _compare_pool(t: str, products: list) -> list:
    """没命中具体商品时，按消息里的品类词圈出可比较的范围。

    「哪款面料更舒服」不含任何商品特征词，P2 必然落空；但「面料」这类
    维度词不构成品类，圈不出范围——那就返回空，交回追问，不要全店乱比。
    """
    for k in kb.KIND_TOKENS:
        if k in t:
            hits = [p for p in products if k in (p.get("title") or "")]
            if 1 < len(hits) <= kb.MAX_CANDIDATES:
                return hits
            return []
    return []


def is_compare(t: str) -> bool:
    """是不是「跨款横向比较」，而不是「没说清是哪款」。

    三道护栏，都是为了不误伤正常咨询：
      1) 明确在问尺码的不算。「哪个码」「M码有货吗」带「哪个」但显然是
         单款尺码问题；只有「哪条裤子尺码全」这种带品类词的选择问法才算比较。
      2) 裸「哪个」不算比较词——「哪个颜色有货」问的是属性，不是商品。
      3) 顾客点了具体商品名时 P2 已命中，根本走不到比较分支。
    """
    if has_any(t, SIZE_WORDS) and not (has_any(t, FABRIC_WORDS)
                                       or has_any(t, FIT_WORDS)
                                       or _which_kind(t)):
        return False
    if has_any(t, SUPER_WORDS):
        return True
    if has_any(t, SELECT_WORDS) or has_any(t, COMPARE_WORDS):
        return True
    return _which_kind(t)


def _compare_point(product, dim):
    """抽某商品在该维度上的一句客观要点；缺资料返回 None。"""
    if dim == "fabric":
        m = product.get("material") or {}
        comp = m.get("composition") or []
        s = "、".join("%s %s%%" % (c.get("fiber"), c.get("percent"))
                      for c in comp if c.get("fiber"))
        if not s:
            return None
        extra = []
        if m.get("weight_gsm"):
            extra.append("克重约 %sg/㎡" % m["weight_gsm"])
        if m.get("elasticity"):
            extra.append(m["elasticity"])
        if m.get("thickness"):
            extra.append("厚薄属%s" % m["thickness"])
        return s + ("，" + "，".join(extra) if extra else "")
    if dim == "fit":
        f = product.get("fit") or {}
        bits = [str(b) for b in (f.get("waistline"), f.get("silhouette"),
                                 f.get("fit_type")) if b]
        if not bits:
            return None
        s = "".join(bits)
        if f.get("notes"):
            s += "（%s）" % f["notes"]
        return s
    if dim == "size":
        sizes = kb.all_sizes(product)
        if not sizes:
            return None
        avail = kb.available_sizes(product)
        return "尺码 %s，目前有货：%s" % (
            "/".join(sizes), "、".join(avail) if avail else "无")
    if dim == "price":
        p = product.get("price_cny")
        return ("售价 ¥%s" % p) if p else None
    if dim == "stock":
        st = STATUS_TEXT.get(product.get("status"), product.get("status"))
        colors = [v.get("color") for v in (product.get("variants") or [])
                  if v.get("color")]
        return "状态「%s」%s" % (st, ("，可选颜色 " + "、".join(colors)) if colors else "")
    return None


def reply_compare(cands, store, text):
    """跨款横向比较。任何一款在该维度上缺资料 → 返回 None，交回原逻辑降级。"""
    if not cands or len(cands) < 2:
        return None
    dim = compare_dim(text)
    lines = []
    for p in cands:
        pt = _compare_point(p, dim)
        if not pt:
            return None         # 资料不全就不比，免得凭空编
        lines.append("· %s —— %s" % (p.get("title"), pt))
    if len(lines) < 2:
        return None
    return (
        "亲，这几款在%s上确实不一样，没法一概而论，主要看您更在意什么：\n%s\n\n"
        "%s。\n您更看重哪一点？我按您的侧重帮您定款～"
        % (DIM_LABEL[dim], "\n".join(lines), DIM_GUIDE[dim])
    )


# --- R7.5 偏好收尾：顾客回答了上一轮比较的追问 -------------------------------
# 为什么必须有：比较回复的结尾是「您更看重哪一点？」。顾客照着答了
# 「我需要更凉爽一点的」，没有这个分支就会被兜底判成「意图不明」转人工——
# 明明顺着我们的话在答，却把人踢走，是最典型的答非所问事故。
PREF_COOL = ("凉爽", "凉快", "透气", "轻薄", "轻凉", "垂顺", "不闷", "凉感", "夏天")
PREF_STIFF = ("挺括", "耐穿", "厚实", "有型", "硬挺", "耐磨", "不易皱", "抗皱")

# 纤维归类只写完整词，不写单字：「麻」会误伤「麻烦」这种高频客套话
COOL_FIBERS = ("粘胶", "天丝", "莱赛尔", "莫代尔", "亚麻", "苎麻", "铜氨", "桑蚕丝")
STIFF_FIBERS = ("棉", "涤", "聚酯", "锦纶", "尼龙", "羊毛", "醋酸")

# 两款分差小于这个值就说明在这件事上没实质区别，别硬选
PREF_MIN_GAP = 8.0

PREF_LABEL = {"cool": "更凉爽、更垂顺", "stiff": "更挺括、更耐穿"}
PREF_OTHER = {"cool": "更挺括耐穿", "stiff": "更凉爽垂顺"}


def preference_direction(t: str):
    """顾客说的是哪个偏好方向。两边都提到就返回 None，交回原逻辑。"""
    c, s = has_any(t, PREF_COOL), has_any(t, PREF_STIFF)
    if c and not s:
        return "cool"
    if s and not c:
        return "stiff"
    return None


def _pref_score(product, want: str):
    """按成分 + 克重给一款打分。资料不全返回 None——不猜。"""
    m = product.get("material") or {}
    comp = m.get("composition") or []
    if not comp:
        return None
    cool = stiff = 0.0
    for c in comp:
        fib = str(c.get("fiber") or "")
        try:
            pct = float(c.get("percent") or 0)
        except Exception:
            continue
        if any(f in fib for f in COOL_FIBERS):
            cool += pct
        elif any(f in fib for f in STIFF_FIBERS):
            stiff += pct
    score = (cool - stiff) if want == "cool" else (stiff - cool)
    gsm = m.get("weight_gsm")
    if gsm:
        try:
            # 克重是第二信号：成分相同时，轻的更凉、重的更挺
            score += ((300 - float(gsm)) / 10.0 if want == "cool"
                      else (float(gsm) - 300) / 10.0)
        except Exception:
            pass
    return score


def reply_preference(cands, store, text):
    """上一轮比过、这轮顾客说了偏好 -> 给出定款建议。"""
    if not cands or len(cands) < 2:
        return None
    want = preference_direction(text)
    if not want:
        return None
    scored = []
    for p in cands:
        s = _pref_score(p, want)
        if s is None:
            return None          # 资料不全就不定款，免得凭空编
        scored.append((s, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    top, second = scored[0], scored[1]
    if abs(top[0] - second[0]) < PREF_MIN_GAP:
        return None              # 没实质区别，交回原逻辑
    pick, other = top[1], second[1]
    point = _compare_point(pick, "fabric") or ""
    return (
        "亲，按您说的想%s，这两款里我更建议【%s】：\n%s。\n\n"
        "另一款【%s】走的是%s的路子，不是您这个诉求的首选。\n"
        "要看它的尺码或现货颜色，我接着帮您查～"
        % (PREF_LABEL[want], pick.get("title"), point,
           other.get("title"), PREF_OTHER[want])
    )


def is_pure_greeting(t: str):
    """整条消息就是寒暄时返回命中的词，否则 None。

    「整条都是」这个约束是关键：「你好，这个能便宜吗」不能被当成寒暄——
    只回一句问候、把议价诉求丢掉，是会挨投诉的实际事故。
    """
    s = re.sub(r"[，。,.!！?？~～、…\s]+", "", t or "")
    if not s or len(s) > GREET_MAX_LEN:
        return None
    for w in GREET_WORDS:
        if s == w:
            return w
        if w.isascii():
            continue        # 英文寒暄只认整条完全相等，免得 hi 误伤别的词
        if w in s and len(s) - len(w) <= 2:
            return w
    return None


def reply_greeting(store):
    """寒暄应答：确认在线 + 给几个开口方向。不堆信息、不长篇自我介绍。"""
    msg = store.get("greeting_message")
    if msg:
        return msg
    return ("亲，在的～您看中哪款了呢？\n"
            "关于尺码、面料、版型、现货颜色、发货时间，都可以直接问我。\n"
            "要是还没定款，告诉我身高体重和想要的款式，我帮您推荐～")


def _decision(text, category, action, reason, **kw):
    d = {
        "text": text,
        "category": category,
        "action": action,
        "reason": reason,
        "confidence": kw.get("confidence", "high"),
        "product_id": None,
        "product_title": None,
        "matched_by": None,
        "missing": [],
        "evidence": [],
        "draft_reply": "",
        "handoff_reason": None,
    }
    d.update(kw)
    return d


def _classify_core(text: str, store: dict, products: list,
                   product_hint=None, context_product=None,
                   pending_candidates=None) -> dict:
    raw = text or ""
    t = norm(raw)
    if not t:
        return _decision(raw, "empty", "handoff", "空消息，不处理",
                         confidence="low", handoff_reason="消息为空")

    # ---- 商品定位（尽早做，后面各分支都要用）----
    # 三级优先：飞鸽商品卡 > 消息文本 > 会话级商品槽位
    product, matched_by, candidates = kb.resolve_product(
        t, products, product_card=product_hint, context_product=context_product)

    # ---- R1 明确要人工 ----
    w = has_any(t, HUMAN_WORDS)
    if w:
        return _decision(raw, "handoff_human_request", "handoff",
                         "顾客明确请求人工客服（命中「%s」）" % w,
                         confidence="high",
                         handoff_reason="顾客要求人工")

    # ---- R2a 具名政策咨询（可答，不需要商品）----
    # 必须排在售后之前：否则「支持七天无理由吗」会被「退货」类词抓走转人工。
    # 但只放行纯政策问法，带第一人称行动词的（「我要用七天无理由退货」）仍是售后。
    w = has_any(t, POLICY_WORDS)
    if w and not has_any(t, ACTION_MARKERS):
        draft = reply_policy(store)
        if draft:
            d = _decision(raw, "pre_sale_policy", "reply",
                          "店铺政策咨询（命中「%s」），店铺规则可支持" % w,
                          product_id=product["product_id"] if product else None,
                          product_title=product.get("title") if product else None,
                          matched_by=matched_by,
                          evidence=["store.policies"])
            d["draft_reply"] = draft
            return d

    # ---- R2 售后 / 质量 / 纠纷 ----
    w = has_any(t, DISPUTE_WORDS)
    if w:
        return _decision(raw, "handoff_dispute", "handoff",
                         "命中纠纷类词「%s」，按规则必须转人工" % w,
                         handoff_reason="投诉/纠纷类（%s）" % w)

    w = has_any(t, AFTER_SALE_WORDS)
    if w:
        return _decision(raw, "handoff_after_sale", "handoff",
                         "命中售后类词「%s」，按规则必须转人工" % w,
                         handoff_reason="退换货/退款类（%s）" % w)

    w = has_any(t, SERVICE_WORDS)
    if w:
        return _decision(raw, "handoff_after_sale", "handoff",
                         "命中售后类词「%s」，按规则必须转人工" % w,
                         handoff_reason="售后维修类（%s）" % w)

    w = has_any(t, QUALITY_WORDS)
    if w:
        if is_past_tense(t):
            return _decision(raw, "handoff_quality", "handoff",
                             "质量问题已发生（命中「%s」且为已然语气），必须转人工" % w,
                             handoff_reason="质量问题（%s）" % w)
        # 疑问语气 -> 落到售前咨询（R5 面料），但性能承诺类另作保守处理

    # ---- 议价：无条件转人工，且必须排在 ambiguous 之前 ----
    # 顾客说「还能便宜吗」无论指哪款都要转人工——私下降价是事故。
    # 如果让 ambiguous 先把它变成「请点选商品」，顾客选完款照样得转人工，
    # 白绕一圈，还让顾客误以为有得谈。
    w = has_any(t, BARGAIN_WORDS)
    if w:
        return _decision(raw, "handoff_bargain", "handoff",
                         "命中议价类词「%s」，让利需人工决定，必须转人工" % w,
                         product_id=product["product_id"] if product else None,
                         product_title=product.get("title") if product else None,
                         matched_by=matched_by,
                         handoff_reason="议价/让利请求（%s）" % w)

    # ---- 「定位不到商品」的统一出口 ----
    def need_product(reason_tail="", cands=None):
        cands = cands or []
        if cands:
            # 有候选就别让顾客重述一遍 —— 列出候选让他点，转化率高得多
            body = "亲，您问的应该是下面这几款里的哪一款呢？"
            for i, c in enumerate(cands, 1):
                body += "\n%s. %s" % (i, c.get("title"))
            body += "\n回复序号或名字都可以，我马上帮您查～"
            return _decision(
                raw, "need_product", "ask_followup",
                "消息特征同时命中 %d 款商品，列出候选让顾客点选" % len(cands) + reason_tail,
                confidence="medium", missing=["product"],
                candidates=[{"product_id": c["product_id"], "title": c.get("title")}
                            for c in cands],
                draft_reply=body,
            )
        return _decision(
            raw, "need_product", "ask_followup",
            "无法确认顾客问的是哪款商品" + reason_tail,
            confidence="medium",
            missing=["product"],
            draft_reply=store.get("product_followup_message", ""),
        )

    # ---- R0 跨款横向比较（必须排在 ambiguous 之前）----
    # 排前面是因为「哪个裤子材质最好」同时命中多款商品，会被 ambiguous 顺手
    # 吞成「请点选商品」；但顾客本意是要横向比较，不是要我们反问他选哪款。
    #
    # 触发条件是「没有唯一确定的商品」而不是「命中多款」：顾客问
    # 「哪款面料更舒服」时消息里没有任何商品特征词，P2 完全落空，
    # 但意图仍然是跨款比较——此时按品类词圈范围，而不是反问他选哪款。
    if not product and matched_by != "card_miss" and is_compare(t):
        pool = list(candidates) if candidates else _compare_pool(t, products)
        if len(pool) >= 2:
            hit = (has_any(t, SUPER_WORDS) or has_any(t, SELECT_WORDS)
                   or has_any(t, COMPARE_WORDS) or "跨款选择")
            draft = reply_compare(pool, store, t)
            if draft:
                return _decision(
                    raw, "pre_sale_compare", "reply",
                    "顾客在跨款比较（命中「%s」），按%s维度给出客观差异，不做优劣排序"
                    % (hit, DIM_LABEL[compare_dim(t)]),
                    confidence="medium",
                    matched_by="compare",
                    evidence=["products.material"],
                    candidates=[{"product_id": c["product_id"], "title": c.get("title")}
                                for c in pool],
                    draft_reply=draft,
                )

    # ---- R7.5 偏好收尾（必须排在 ambiguous 之前）----
    # 排前面的理由：偏好应答常带「那种 / 这款」这类选择词，会被 ambiguous
    # 顺手吞成「请点选商品」。但顾客是在回答我们上一轮的追问，不是在挑款。
    # 触发条件很窄：上一轮留下了候选，且这轮说清了单一偏好方向。
    if pending_candidates:
        ids = {c.get("product_id") for c in pending_candidates if c.get("product_id")}
        cands = [p for p in products if p.get("product_id") in ids]
        if len(cands) >= 2:
            want = preference_direction(t)
            draft = reply_preference(cands, store, t)
            if draft and want:
                pick = max(cands, key=lambda p: (_pref_score(p, want) or -999))
                return _decision(
                    raw, "pre_sale_preference", "reply",
                    "顾客回答了上一轮比较的追问（侧重%s），按成分与克重定款"
                    % PREF_LABEL[want],
                    confidence="medium",
                    matched_by="preference",
                    product_id=pick.get("product_id"),
                    product_title=pick.get("title"),
                    evidence=["products.material"],
                    draft_reply=draft,
                )

    if matched_by == "ambiguous":
        return need_product("（消息特征同时命中多款商品）", cands=candidates)

    # 商品卡指向的商品不在知识库 -> 顾客问的是一件我们没资料的商品，
    # 和「不知道问哪款」是两回事：不能追问顾客，只能转人工
    if matched_by == "card_miss":
        return _decision(raw, "handoff_data_gap", "handoff",
                         "飞鸽商品卡指向的商品不在知识库中，无资料可依，转人工",
                         confidence="low", missing=["product_in_kb"],
                         handoff_reason="商品卡商品未入库")

    # ---- R3 库存 / 颜色 ----
    if hit_stock(t):
        if not product:
            return need_product("（顾客在问库存/颜色，但未识别到商品）")
        if product.get("status") == "off_shelf":
            return _decision(raw, "handoff_after_sale", "handoff",
                             "顾客咨询的商品已下架，避免误导，转人工",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             handoff_reason="商品已下架")
        # 全码无货：自动回「缺货」到此为止，顾客接着必然问补货/能否预定，
        # 这些答不了。直接转人工，人工还能推替代款。
        if not kb.available_sizes(product):
            return _decision(raw, "handoff_stock_out", "handoff",
                             "该商品所有尺码均无货，自动回复帮不上，转人工跟进",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             handoff_reason="全码缺货")
        d = _decision(raw, "pre_sale_stock", "reply",
                      "库存/颜色咨询，资料可支持",
                      product_id=product["product_id"],
                      product_title=product.get("title"),
                      matched_by=matched_by)
        d["draft_reply"] = reply_stock(product, store, t)
        d["evidence"] = ["variants", "product.status"]
        return d

    # ---- R3.5 价格 / 优惠（议价已在前面无条件拦截，不在这里重复）----
    w = has_any(t, PRICE_WORDS) or has_any(t, PROMO_WORDS)
    if w:
        promo = store.get("promotions") or {}
        if not product:
            # 只问店铺级活动（「店里有什么优惠」）→ 不需要商品也能答
            if has_any(t, PROMO_WORDS) and promo.get("note"):
                d = _decision(raw, "pre_sale_promo", "reply",
                              "店铺优惠咨询（命中「%s」），店铺规则可支持" % w,
                              evidence=["store.promotions"])
                d["draft_reply"] = "亲，%s。最终以商品页显示的到手价为准～" % promo["note"]
                return d
            return need_product("（顾客在问价格，但未识别到商品）")
        draft = reply_price(product, store, t)
        if not draft:
            return _decision(raw, "handoff_data_gap", "handoff",
                             "顾客问价格，但商品资料未提供售价",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             missing=["price_cny"],
                             handoff_reason="商品资料缺失：售价")
        d = _decision(raw, "pre_sale_price", "reply", "价格咨询，资料可支持",
                      product_id=product["product_id"],
                      product_title=product.get("title"),
                      matched_by=matched_by, evidence=["price_cny"])
        d["draft_reply"] = draft
        return d

    # ---- R4 尺码 ----
    if hit_size(t):
        if not product:
            return need_product("（顾客在问尺码，但未识别到商品）")
        if product.get("status") == "off_shelf":
            return _decision(raw, "handoff_after_sale", "handoff",
                             "顾客咨询的商品已下架，避免误导，转人工",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by, handoff_reason="商品已下架")

        sc = product.get("size_chart") or {}
        if not sc.get("rows"):
            return _decision(raw, "handoff_data_gap", "handoff",
                             "该商品缺少尺码表，无法给出尺码建议",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             missing=["size_chart"],
                             handoff_reason="商品资料缺失：尺码表")

        height, weight = parse_body(t)
        if height is None or weight is None:
            miss = []
            if height is None:
                miss.append("height")
            if weight is None:
                miss.append("weight")
            return _decision(raw, "pre_sale_size", "ask_followup",
                             "尺码咨询，但顾客未提供%s，需先追问"
                             % "、".join("身高" if m == "height" else "体重" for m in miss),
                             confidence="medium",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             missing=miss,
                             evidence=["size_chart.rows 存在"],
                             draft_reply=store.get("size_followup_message", ""))

        d = _decision(raw, "pre_sale_size", "reply",
                      "尺码咨询，顾客已提供身高体重，商品有尺码表",
                      product_id=product["product_id"],
                      product_title=product.get("title"),
                      matched_by=matched_by,
                      evidence=["size_chart.basis=%s" % sc.get("basis")])
        d["draft_reply"] = reply_size(product, height, weight,
                                      store.get("size_guide"), store)
        d["parsed"] = {"height": height, "weight": weight}
        return d

    # ---- R4.5 有无此商品 ----
    if has_any(t, AVAIL_WORDS) and not hit_size(t) and not hit_stock(t):
        if not product:
            return need_product("（顾客在问有没有某类商品，但未识别到具体商品）")
        if product.get("status") == "off_shelf":
            return _decision(raw, "handoff_after_sale", "handoff",
                             "顾客问的商品已下架，避免误导，转人工",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by, handoff_reason="商品已下架")
        d = _decision(raw, "pre_sale_availability", "reply",
                      "顾客问有没有这款商品，已定位到商品",
                      product_id=product["product_id"],
                      product_title=product.get("title"),
                      matched_by=matched_by)
        d["draft_reply"] = reply_availability(product, store)
        d["evidence"] = ["title", "price_cny", "variants"]
        return d

    # ---- R5 面料 ----
    if has_any(t, FABRIC_WORDS) or has_any(t, PERFORMANCE_WORDS):
        if not product:
            return need_product("（顾客在问面料/材质，但未识别到商品）")

        # 性能承诺类：必须资料明确支持，否则转人工（不编）
        pw = has_any(t, PERFORMANCE_WORDS)
        if pw:
            return _decision(raw, "handoff_data_gap", "handoff",
                             "「%s」属性能承诺类问题，当前商品资料未包含该项说明，"
                             "按规则不承诺、转人工核实" % pw,
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             missing=["material.%s" % pw],
                             handoff_reason="商品资料缺失：%s 性能说明" % pw)

        m = product.get("material") or {}
        # 洗护类问题需要 care 字段
        if any(k in t for k in ("洗", "熨", "养护", "保养", "机洗")):
            if not m.get("care"):
                return _decision(raw, "handoff_data_gap", "handoff",
                                 "顾客问洗护方式，但商品资料未提供洗护说明，不编造",
                                 product_id=product["product_id"],
                                 product_title=product.get("title"),
                                 matched_by=matched_by,
                                 missing=["material.care"],
                                 handoff_reason="商品资料缺失：洗护说明")

        draft = reply_fabric(product, store)
        if not draft:
            return _decision(raw, "handoff_data_gap", "handoff",
                             "顾客问面料，但商品资料缺少面料成分",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             missing=["material.composition"],
                             handoff_reason="商品资料缺失：面料成分")
        d = _decision(raw, "pre_sale_fabric", "reply",
                      "面料咨询，资料可支持",
                      product_id=product["product_id"],
                      product_title=product.get("title"),
                      matched_by=matched_by,
                      evidence=["material.composition"])
        d["draft_reply"] = draft
        return d

    # ---- R6 版型 ----
    if has_any(t, FIT_WORDS):
        if not product:
            return need_product("（顾客在问版型，但未识别到商品）")
        draft = reply_fit(product, store)
        if not draft:
            return _decision(raw, "handoff_data_gap", "handoff",
                             "顾客问版型，但商品资料缺少版型说明",
                             product_id=product["product_id"],
                             product_title=product.get("title"),
                             matched_by=matched_by,
                             missing=["fit"],
                             handoff_reason="商品资料缺失：版型")
        d = _decision(raw, "pre_sale_fit", "reply", "版型咨询，资料可支持",
                      product_id=product["product_id"],
                      product_title=product.get("title"),
                      matched_by=matched_by, evidence=["fit"])
        d["draft_reply"] = draft
        return d

    # ---- R7 发货 ----
    if has_any(t, SHIPPING_WORDS):
        d = _decision(raw, "pre_sale_shipping", "reply",
                      "售前发货/物流咨询，店铺规则可支持",
                      product_id=product["product_id"] if product else None,
                      product_title=product.get("title") if product else None,
                      matched_by=matched_by,
                      evidence=["store.shipping"])
        d["draft_reply"] = reply_shipping(store)
        return d

    # ---- R1g 寒暄（放最后，避免吞掉真实诉求）----
    w = is_pure_greeting(t)
    if w:
        d = _decision(raw, "pre_sale_greeting", "reply",
                      "顾客寒暄（命中「%s」），回问候并给出开口方向" % w,
                      matched_by=matched_by)
        d["draft_reply"] = reply_greeting(store)
        return d

    # ---- R8 兜底：不认识的意图，一律转人工 ----
    return _decision(raw, "handoff_unknown", "handoff",
                     "无法归类为可答的售前问题，按保守立场转人工",
                     confidence="low",
                     product_id=product["product_id"] if product else None,
                     product_title=product.get("title") if product else None,
                     matched_by=matched_by,
                     handoff_reason="意图不明")


_GENERIC_HANDOFF_CATS = ("handoff_data_gap", "handoff_unknown",
                         "handoff_stock_out", "handoff_bargain")


def classify(text: str, store: dict, products: list,
             product_hint=None, context_product=None,
             pending_candidates=None) -> dict:
    """对外接口：在核心判定外统一补齐 handoff 占位回复。

    product_hint       : 飞鸽会话上挂的商品卡（最可靠来源）
    context_product    : 会话级商品槽位，本会话上一轮已确认的商品
    pending_candidates : 上一轮比较留下的候选，顾客本轮回答偏好时用来定款
    """
    d = _classify_core(text, store, products, product_hint, context_product,
                       pending_candidates)
    if d["action"] == "handoff" and not d.get("draft_reply"):
        # operating-policy.md：占位回复必须简短，且不得承诺处理结果。
        # 售后类要说「结合订单核实」；资料/意图类说「帮您确认」更贴切。
        if d["category"] in _GENERIC_HANDOFF_CATS:
            msg = store.get("handoff_message_generic") or store.get("handoff_message", "")
        else:
            msg = store.get("handoff_message", "")
        d["draft_reply"] = msg
    return d


# ===========================================================================
# 六、CLI
# ===========================================================================
def render(d: dict) -> str:
    icon = {"reply": "✅", "ask_followup": "❓", "handoff": "🔔"}.get(d["action"], "•")
    lines = []
    lines.append("%s [%s] %s" % (icon, d["action"], d["category"]))
    lines.append("   顾客说 : %s" % d["text"])
    if d.get("product_title"):
        lines.append("   商品   : %s (%s, 匹配方式=%s)"
                     % (d["product_title"], d["product_id"], d.get("matched_by")))
    lines.append("   依据   : %s" % d["reason"])
    if d.get("missing"):
        lines.append("   缺失   : %s" % "、".join(d["missing"]))
    if d.get("handoff_reason"):
        lines.append("   转人工 : %s" % d["handoff_reason"])
    if d.get("draft_reply"):
        lines.append("   草稿   : %s" % d["draft_reply"])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="客服决策逻辑（阶段 2）")
    ap.add_argument("--text", help="顾客最新一条消息")
    ap.add_argument("--product", default=None,
                    help="飞鸽会话上的商品卡：product_id / sku_code / 标题片段")
    ap.add_argument("--context", default=None,
                    help="会话级商品槽位：本会话上一轮已确认的商品（商品卡和文本都无果时兜底）")
    ap.add_argument("--store", default=kb.DEFAULT_STORE_ID)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--batch", help="批量：JSON 文件，元素为 {text, product, context}")
    args = ap.parse_args()

    store, products = kb.load_kb(args.store)

    if args.batch:
        with io.open(args.batch, "r", encoding="utf-8") as f:
            cases = json.load(f)
        out = [classify(c.get("text", ""), store, products,
                        c.get("product"), c.get("context"))
               for c in cases]
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            for d in out:
                print(render(d))
                print()
        return 0

    if not args.text:
        ap.error("需要 --text 或 --batch")

    d = classify(args.text, store, products, args.product, args.context)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print(render(d))
    return 0 if d["action"] != "handoff" else 0


if __name__ == "__main__":
    raise SystemExit(main())
