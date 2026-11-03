# -*- coding: utf-8 -*-
"""Versioned, merchant-owned configuration for the standard product engine."""
from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - covered by the application dependency
    yaml = None


SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_PLATFORM_URL = "https://im.jinritemai.com/pc_seller_v2/main/workspace"
DEFAULT_HANDOFF = ["退款", "退货", "换货", "售后", "投诉", "赔偿", "转人工", "人工客服",
                   "再便宜", "单独优惠", "私下优惠", "特殊折扣"]
DEFAULT_SYNONYMS = {
    "退钱": "退款", "咋付": "怎么付款", "几钱": "多少钱", "啥价": "多少钱",
    "啥时候发": "什么时候发", "哪个快递": "什么快递", "几天能到": "几天到",
}
DEFAULT_INTENTS = {
    "inventory": {"keywords": ["有货", "库存", "现货", "缺货", "还有吗", "还卖吗", "还有货吗"], "source": "product.stock"},
    "price": {"keywords": ["多少钱", "什么价", "价格", "售价"], "source": "product.price"},
    "shipping_time": {"keywords": ["什么时候发", "多久发", "发货时间", "今天发", "明天发", "可以发货吗"], "source": "shipping.lead_time"},
    "shipping_origin": {"keywords": ["哪里发货", "哪儿发货", "发货地", "从哪发", "哪里寄"], "source": "shipping.origin"},
    "shipping_carrier": {"keywords": ["什么快递", "哪家快递", "发什么快递"], "source": "shipping.carriers"},
    "delivery_estimate": {"keywords": ["多久到", "几天到", "什么时候到", "到货时间"], "source": "shipping.delivery_estimate"},
    "public_promotion": {"keywords": ["优惠", "优惠券", "折扣", "能便宜", "活动价"], "source": "promotions.note"},
    "payment_methods": {"keywords": ["怎么付款", "如何支付", "支付方式", "能用什么支付"], "source": "payment.methods"},
    "order_flow": {"keywords": ["怎么下单", "如何下单", "购买流程", "怎么买"], "source": "orders.flow"},
    "product_details": {"keywords": ["规格", "材质", "参数", "尺寸", "适用"], "source": "product.attributes"},
}
PRODUCT_SOURCES = {"product.stock", "product.price", "product.attributes"}
SUPPORTED_SOURCES = PRODUCT_SOURCES | {
    "shipping.lead_time", "shipping.origin", "shipping.carriers",
    "shipping.delivery_estimate", "promotions.note", "payment.methods", "orders.flow",
}


def supported_source(source: str) -> bool:
    return source in SUPPORTED_SOURCES or (
        isinstance(source, str) and source.startswith("policies.")
        and len(source.split(".", 1)[1]) > 0
    )


class MerchantConfigError(ValueError):
    pass


def read_manifest(path: Path | str) -> dict:
    path = Path(path)
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        if yaml is None:
            raise MerchantConfigError("读取 merchant.yaml 需要安装 pyyaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_manifest(data, path.parent.name)
    return data


def validate_manifest(data: dict, directory_id: str | None = None) -> None:
    if not isinstance(data, dict):
        raise MerchantConfigError("merchant 配置必须是对象")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise MerchantConfigError("不支持的 merchant 配置版本")
    merchant_id = data.get("store_id")
    if not isinstance(merchant_id, str) or len(merchant_id) > 64 or not ID_RE.fullmatch(merchant_id):
        raise MerchantConfigError("store_id 只能包含字母、数字、下划线和短横线")
    if directory_id and merchant_id != directory_id:
        raise MerchantConfigError("目录名必须与 store_id 一致")
    for section in ("platform", "automation", "business"):
        if not isinstance(data.get(section), dict):
            raise MerchantConfigError("缺少 %s 配置对象" % section)
    platform = data["platform"]
    platform_type = platform.get("type", "feige")
    if platform_type != "feige":
        raise MerchantConfigError("社区版仅支持抖店飞鸽")
    owner = data.get("merchant_id", "local")
    if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", owner):
        raise MerchantConfigError("merchant_id 只能包含1–64位字母、数字、下划线和短横线")
    if platform_type != "feige" and any(data["automation"].get(key) for key in ("auto_send", "auto_start")):
        raise MerchantConfigError("该平台消息连接尚未接入，不能开启自动发送或自动启动")
    try:
        port = int(platform.get("cdp_port", 9222))
    except (TypeError, ValueError) as exc:
        raise MerchantConfigError("cdp_port 必须是有效端口") from exc
    if not 1024 <= port <= 65535:
        raise MerchantConfigError("cdp_port 必须位于 1024–65535")
    automation = data["automation"]
    for key in ("enabled", "auto_send", "auto_start"):
        if key in automation and not isinstance(automation[key], bool):
            raise MerchantConfigError("automation.%s 必须是 true 或 false" % key)
    for key in ("poll_interval", "full_scan_interval", "max_replies_per_minute"):
        if key in automation:
            try:
                valid = float(automation[key]) > 0
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise MerchantConfigError("automation.%s 必须大于 0" % key)
    business = data["business"]
    reception = business.get("reception", {})
    if not isinstance(reception, dict) or not isinstance(reception.get("enabled", False), bool):
        raise MerchantConfigError("business.reception.enabled 必须是 true 或 false")
    if not isinstance(business.get("category", ""), str):
        raise MerchantConfigError("business.category 必须是文字")
    if not isinstance(business.get("handoff", {}), dict):
        raise MerchantConfigError("business.handoff 必须是对象")
    if not isinstance(business.get("handoff", {}).get("keywords", DEFAULT_HANDOFF), list):
        raise MerchantConfigError("business.handoff.keywords 必须是列表")
    if not isinstance(business.get("policies", {}), dict):
        raise MerchantConfigError("business.policies 必须是对象")
    synonyms = business.get("synonyms", {})
    if not isinstance(synonyms, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in synonyms.items()
    ):
        raise MerchantConfigError("business.synonyms 必须是非空词语到标准词的映射")
    shipping = business.get("shipping", {})
    if not isinstance(shipping, dict) or not isinstance(shipping.get("delivery_by_region", {}), dict):
        raise MerchantConfigError("business.shipping.delivery_by_region 必须是地区到时效的映射")
    intents = business.get("intents", {})
    if not isinstance(intents, dict):
        raise MerchantConfigError("business.intents 必须是对象")
    for name, rule in intents.items():
        if not isinstance(rule, dict) or not isinstance(rule.get("keywords"), list):
            raise MerchantConfigError("意图 %s 缺少 keywords 列表" % name)
        if not supported_source(rule.get("source")) and not rule.get("answer"):
            raise MerchantConfigError("意图 %s 的数据源不受支持，或缺少固定答复" % name)
    catalog = data.get("catalog", {})
    if not isinstance(catalog, dict) or not isinstance(catalog.get("products", []), list):
        raise MerchantConfigError("catalog.products 必须是列表")
    for product in catalog.get("products", []):
        if not isinstance(product, dict) or not product.get("product_id") or not product.get("title"):
            raise MerchantConfigError("每个商品都需要 product_id 和 title")


def default_manifest(store_id: str, name: str, port: int = 9222, platform_type="feige", merchant_id="local") -> dict:
    data = {
        "schema_version": SCHEMA_VERSION,
        "store_id": store_id,
        "name": name,
        "merchant_id": merchant_id,
        "platform": {"type": platform_type, "cdp_port": port,
                     "workspace_url": DEFAULT_PLATFORM_URL if platform_type == "feige" else "",
                     "profile_dir": "data/profiles/%s" % store_id, "browser_path": ""},
        "automation": {"enabled": True, "auto_send": False, "auto_start": False,
                       "poll_interval": 3.0, "full_scan_interval": 15.0,
                       "max_replies_per_minute": 20},
        "business": {"category": "", "handoff": {"keywords": list(DEFAULT_HANDOFF)},
                     "shipping": {"lead_time": "", "origin": "", "carriers": [],
                                  "delivery_estimate": "", "delivery_by_region": {}},
                     "orders": {"flow": ""}, "payment": {"methods": []},
                     "promotions": {"note": ""}, "policies": {}, "intents": {},
                     "synonyms": {}},
        "catalog": {"products": []},
    }
    validate_manifest(data)
    return data


def onboarding_errors(data: dict, products: list[dict]) -> list[str]:
    """Readiness check; blank facts are valid while building a merchant account."""
    errors = []
    business = data["business"]
    if not business.get("category"):
        errors.append("请填写 business.category")
    if not products:
        errors.append("请导入至少一件真实商品")
    if any(p.get("sample_data") or p.get("source") == "sample" for p in products):
        errors.append("商品中包含样例数据，不得用于自动发送")
    if data["automation"].get("auto_send") and errors:
        errors.append("自动发送已开启，但接入资料尚未准备好")
    return errors
