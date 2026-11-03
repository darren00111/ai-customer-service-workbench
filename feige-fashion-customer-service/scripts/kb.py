# -*- coding: utf-8 -*-
"""知识库加载 —— 读 data/stores/<store_id>/{store.yaml, products/*.yaml}。

优先级：yaml 为主（人工可读、便于商家核对）。
若运行环境没有 pyyaml，会退化尝试同名 .json，仍然失败则明确报错。
"""
from __future__ import annotations

import io
import json
import os

try:
    import yaml
    _HAS_YAML = True
except ImportError:      # pragma: no cover - 环境相关
    yaml = None
    _HAS_YAML = False


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STORES_DIR = os.path.join(ROOT, "data", "stores")

DEFAULT_STORE_ID = "shop_001"


class KnowledgeBaseError(RuntimeError):
    pass


def _load_structured(path: str):
    """读一个结构化文件，按扩展名选解析器。"""
    if path.endswith(".json"):
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    if not _HAS_YAML:
        raise KnowledgeBaseError(
            "读取 %s 需要 pyyaml；请先安装：pip install pyyaml" % path
        )
    with io.open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def store_dir(store_id: str = DEFAULT_STORE_ID) -> str:
    return os.path.join(STORES_DIR, store_id)


def load_store(store_id: str = DEFAULT_STORE_ID) -> dict:
    d = store_dir(store_id)
    merchant_path = os.path.join(d, "merchant.yaml")
    if os.path.exists(merchant_path):
        from merchant import read_manifest
        data = read_manifest(merchant_path)
        return {"store_id": store_id, "store_name": data.get("name") or store_id,
                "decision_engine": "standard", "platform": data["platform"].get("type", "feige"), "business": data["business"],
                "automation": data["automation"]}
    for name in ("store.yaml", "store.yml", "store.json"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            data = _load_structured(p) or {}
            data.setdefault("store_id", store_id)
            return data
    raise KnowledgeBaseError("找不到店铺配置：%s/store.yaml" % d)


def load_products(store_id: str = DEFAULT_STORE_ID) -> list:
    root = store_dir(store_id)
    merchant_path = os.path.join(root, "merchant.yaml")
    inline = []
    if os.path.exists(merchant_path):
        from merchant import read_manifest
        inline = read_manifest(merchant_path).get("catalog", {}).get("products", [])
    d = os.path.join(root, "products")
    if not os.path.isdir(d) and not os.path.exists(merchant_path):
        raise KnowledgeBaseError("找不到商品目录：%s" % d)

    out = list(inline)
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.lower().endswith((".yaml", ".yml", ".json")):
            continue
        doc = _load_structured(os.path.join(d, fn))
        if isinstance(doc, dict) and doc.get("product_id"):
            out.append(doc)
    ids = [str(p["product_id"]) for p in out]
    if len(ids) != len(set(ids)):
        raise KnowledgeBaseError("店铺 %s 存在重复的 product_id" % store_id)
    return out


def load_kb(store_id: str = DEFAULT_STORE_ID):
    """一次加载店铺配置 + 全部商品。"""
    return load_store(store_id), load_products(store_id)


# ---------------------------------------------------------------------------
# 商品匹配
# ---------------------------------------------------------------------------
# 标题里的通用词，参与匹配会误伤（比如每件衣服都含"女"，不能靠它定位）
GENERIC_WORDS = (
    "女装", "男装", "女士", "男士", "新款", "同款", "正品", "包邮",
    "春秋", "夏季", "冬季", "秋冬", "春夏", "四季",
    "长袖", "短袖", "无袖",
)


def _longest_common_substring(title: str, text: str):
    """最长公共子串。返回 (长度, 子串)。标题短，暴力即可。"""
    best, best_s = 0, ""
    n = len(title)
    for i in range(n):
        for j in range(i + 2, n + 1):
            sub = title[i:j]
            if sub in text and (j - i) > best:
                best, best_s = j - i, sub
    return best, best_s


# 只由这些字构成的匹配等于没匹配。「裤」「裙」「衣」这类字太泛，
# 单靠它定位商品必然误伤——一家店里可能同时有 5 条裤子。
WEAK_CHARS = set("裤裙衣装款个这那件套的")

MIN_SCORE = 2          # 低于此分视为没匹配上
MAX_CANDIDATES = 5     # 歧义时最多列几个候选给顾客选


def _is_weak(sub: str) -> bool:
    return bool(sub) and all(ch in WEAK_CHARS for ch in sub)


# 品类词：顾客说「裤」就不可能是在问「裙」。用它在候选里做一致性过滤，
# 否则「高腰裤有M码吗」会把「高腰A字半身裙」也当成候选列出去。
KIND_TOKENS = ("羽绒服", "连衣裙", "半身裙", "阔腿裤", "牛仔裤", "西装裤",
               "外套", "T恤", "衬衫", "卫衣", "开衫", "毛衣", "裤", "裙")


def _filter_by_kind(text: str, scored: list) -> list:
    """若消息里带品类词，只保留标题含该品类词的商品（过滤后为空则不过滤）。"""
    for k in KIND_TOKENS:
        if k in text:
            kept = [(s, p) for s, p in scored if k in (p.get("title") or "")]
            if kept:
                return kept
    return scored


def _match_by_kind_only(text: str, products: list):
    """兜底：消息里只有品类词（「裙子呢」「有卖羽绒服吗」）。

    这类消息靠标题子串匹配必然落空——「裙子」跟任何标题的公共子串都只有
    「裙」这一个泛字，会被 _is_weak 判废。但顾客的意思很清楚，就是问
    「你们家这类东西」。此时把该品类下的商品列出来给他点，远比转人工好。
    """
    for k in KIND_TOKENS:
        if k in text:
            hits = [p for p in products if k in (p.get("title") or "")]
            if not hits:
                continue
            if len(hits) == 1:
                return hits[0], []
            return None, hits[:MAX_CANDIDATES]
    return None, []


def _match_by_text(text: str, products: list):
    """消息文本 → 商品。返回 (唯一命中 | None, 并列候选列表)。"""
    scored = []
    for p in products:
        title = p.get("title") or ""
        cleaned = title
        for w in GENERIC_WORDS:
            cleaned = cleaned.replace(w, " ")
        score, sub = _longest_common_substring(cleaned, text)
        if _is_weak(sub):
            score = 0
        if p.get("product_id") and p["product_id"].lower() in text.lower():
            score = max(score, 6)
        if score >= MIN_SCORE:
            scored.append((score, p))

    if not scored:
        # 子串全废，但可能只是顾客说得极简（只给了品类）
        return _match_by_kind_only(text, products)

    scored = _filter_by_kind(text, scored)
    scored.sort(key=lambda x: -x[0])
    top_score = scored[0][0]
    tied = [p for s, p in scored if s == top_score]
    if len(tied) == 1:
        return tied[0], []
    # 并列最高分 —— 不猜，全部作为候选交给顾客确认
    return None, tied[:MAX_CANDIDATES]


def resolve_product(text: str, products: list,
                    product_card: str | None = None,
                    context_product: str | None = None):
    """定位顾客问的是哪个商品。多级优先，任一级命中即返回。

    优先级（高 → 低）：
      P1 product_card    飞鸽会话上挂的商品卡。**最可靠**——顾客从商品详情页
                         点「客服」进来时飞鸽会自动带上，完全不依赖顾客措辞。
      P2 消息文本        顾客自己说了商品名或特征（「高腰裤」「碎花的裙子」）。
      P3 context_product 会话级商品槽位：本会话里之前已经确认过的那个商品。
                         顾客说「这个有 M 码吗」时，「这个」指的就是它。

    返回 (product, matched_by, candidates)
      matched_by : card / card_title / card_miss / text / context /
                   ambiguous / no_text / None
      candidates : 仅在 ambiguous 时非空，供回复里列给顾客点选
    """
    def _by_ref(ref, tag):
        r = (ref or "").strip()
        if not r:
            return None
        for p in products:
            if r == p.get("product_id") or r == p.get("sku_code"):
                return p, tag
        for p in products:
            if r in (p.get("title") or ""):
                return p, tag + "_title"
        return None

    # ---- P1 商品卡 ----
    if product_card:
        hit = _by_ref(product_card, "card")
        if hit:
            return hit[0], hit[1], []
        # 卡上的商品不在知识库里 —— 不瞎猜，交给调用方转人工
        return None, "card_miss", []

    if not text:
        return None, "no_text", []

    # ---- P2 消息文本 ----
    prod, cands = _match_by_text(text, products)
    if prod:
        return prod, "text", []

    # P2 出歧义时，若上下文槽位正好落在候选里 —— 那它就是答案
    if cands and context_product:
        hit = _by_ref(context_product, "context")
        if hit and any(hit[0]["product_id"] == c["product_id"] for c in cands):
            return hit[0], "context", []
    if cands:
        return None, "ambiguous", cands

    # ---- P3 会话级商品槽位 ----
    if context_product:
        hit = _by_ref(context_product, "context")
        if hit:
            return hit[0], hit[1], []

    return None, None, []


def match_product(text: str, products: list, product_hint: str | None = None):
    """兼容旧签名：只取 (product, matched_by)。"""
    p, by, _ = resolve_product(text, products, product_card=product_hint)
    return p, by


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def sold_sizes(product: dict) -> set:
    """该商品所有颜色下都售罄的尺码。"""
    variants = product.get("variants") or []
    if not variants:
        return set()
    all_sizes = set()
    for v in variants:
        all_sizes |= set((v.get("sizes") or {}).keys())
    return {s for s in all_sizes
            if all(int((v.get("sizes") or {}).get(s) or 0) <= 0 for v in variants)}


def available_sizes(product: dict) -> list:
    """至少有一种颜色有货的尺码，按尺码表顺序。"""
    order = [r["size"] for r in (product.get("size_chart") or {}).get("rows") or []]
    sold = sold_sizes(product)
    if order:
        return [s for s in order if s not in sold]
    sizes = set()
    for v in product.get("variants") or []:
        sizes |= {s for s, q in (v.get("sizes") or {}).items() if int(q or 0) > 0}
    return sorted(sizes)


def all_sizes(product: dict) -> list:
    order = [r["size"] for r in (product.get("size_chart") or {}).get("rows") or []]
    if order:
        return order
    sizes = set()
    for v in product.get("variants") or []:
        sizes |= set((v.get("sizes") or {}).keys())
    return sorted(sizes)
