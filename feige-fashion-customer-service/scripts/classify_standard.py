# -*- coding: utf-8 -*-
"""Category-neutral, evidence-gated decisions for merchant.yaml shops."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

from merchant import DEFAULT_HANDOFF, DEFAULT_INTENTS, DEFAULT_SYNONYMS, PRODUCT_SOURCES

REGIONS = ("黑龙江", "内蒙古", "新疆", "宁夏", "西藏", "广西", "广东", "湖南", "湖北",
           "河南", "河北", "山东", "山西", "江苏", "浙江", "安徽", "福建", "江西", "四川",
           "贵州", "云南", "陕西", "甘肃", "青海", "辽宁", "吉林", "海南", "台湾",
           "北京", "上海", "天津", "重庆", "香港", "澳门")


def _result(text, category, action, reason, **extra):
    return {"text": text, "category": category, "action": action,
            "reason": reason, "confidence": "high" if action == "reply" else "low",
            "draft_reply": "", **extra}


def _normalize(text, synonyms=None):
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    normalized = re.sub(r"\s+", "", normalized)
    for phrase, replacement in sorted((synonyms or {}).items(), key=lambda item: -len(item[0])):
        normalized = normalized.replace(phrase.lower(), replacement.lower())
    return normalized


def _product(text, products, product_hint=None, context_product=None, synonyms=None):
    """Explicit name wins; a card or recent confirmed product is a fallback."""
    scored = []
    for product in products:
        names = [product.get("product_id", ""), product.get("sku_code", ""),
                 product.get("title", "")] + (product.get("aliases") or [])
        hits = [len(str(name)) for name in names
                if len(str(name)) >= 2 and _normalize(name) in text]
        if hits:
            scored.append((max(hits), product))
    scored.sort(key=lambda item: -item[0])
    if scored:
        return scored[0][1] if len(scored) == 1 or scored[0][0] > scored[1][0] else None
    for ref in (product_hint, context_product):
        if not ref:
            continue
        candidates = [product for product in products if any(
            str(name) and _normalize(name) in _normalize(ref, synonyms)
            for name in (product.get("product_id"), product.get("sku_code"),
                         product.get("title"), *(product.get("aliases") or []))
        )]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _fact(source, business, product, region=None):
    if source == "product.price":
        return product.get("price") if product else None
    if source == "product.stock":
        return product.get("stock") if product else None
    if source == "product.attributes":
        return product.get("attributes") if product else None
    if source.startswith("policies."):
        return (business.get("policies") or {}).get(source.split(".", 1)[1])
    if source == "shipping.delivery_estimate" and region:
        by_region = (business.get("shipping") or {}).get("delivery_by_region") or {}
        if by_region:
            return by_region.get(region)
    section, key = source.split(".", 1)
    return (business.get(section) or {}).get(key)


def _approved_fact(value):
    """Fact objects may carry provenance and an expiry; expired facts are unusable."""
    evidence = {}
    if isinstance(value, dict) and "value" in value:
        evidence = {key: value.get(key) for key in ("source", "updated_at", "expires_at")
                    if value.get(key)}
        expires = value.get("expires_at")
        if expires:
            try:
                if len(str(expires)) == 10:
                    expired = date.today() > date.fromisoformat(str(expires))
                else:
                    expired = datetime.now().astimezone() > datetime.fromisoformat(
                        str(expires).replace("Z", "+00:00")
                    ).astimezone()
            except ValueError:
                return None, evidence, "失效时间格式无效"
            if expired:
                return None, evidence, "资料已过期"
        value = value.get("value")
    if value is None or value == "" or value == [] or value == {}:
        return None, evidence, "资料缺失"
    return value, evidence, ""


def _format_fact(source, fact, product, region=None):
    if source == "product.stock":
        if product.get("status") in ("off_shelf", "discontinued"):
            return "%s目前已下架，是否重新销售请以商品页面为准。" % product["title"]
        if isinstance(fact, (int, float)) and fact <= 0:
            return "%s目前显示无货，后续补货请以商品页面为准。" % product["title"]
        return "%s资料显示库存%s件，实际可购买数量以商品页为准。" % (product["title"], fact)
    if source == "product.price":
        return "%s资料显示标价%s元，实际成交价以商品页为准。" % (product["title"], fact)
    if source == "product.attributes":
        if isinstance(fact, dict):
            fact = "；".join("%s：%s" % (key, value) for key, value in fact.items())
        return "%s的资料：%s。" % (product["title"], fact)
    if source == "shipping.delivery_estimate":
        return "%s预计%s，具体以物流实际揽收和运输情况为准。" % (
            ("发往%s" % region) if region else "物流", fact)
    if source == "shipping.origin":
        return "资料显示从%s发货，实际发货仓以订单页面为准。" % fact
    if source == "shipping.lead_time":
        return "资料显示%s，具体以商品页面和订单状态为准。" % fact
    if isinstance(fact, list):
        return "、".join(str(item) for item in fact)
    return str(fact)


def _intent_rules(business):
    rules = {name: dict(rule) for name, rule in DEFAULT_INTENTS.items()}
    for name, override in (business.get("intents") or {}).items():
        rules[name] = dict(rules.get(name, {}), **override)
    return rules


def classify(text: str, store: dict, products: list, product_hint=None,
             context_product=None, pending_candidates=None, pending_intent=None,
             handoff_pending=False, _selected_intent=None) -> dict:
    del pending_candidates
    raw = (text or "").strip()
    business = store.get("business") or {}
    synonyms = dict(DEFAULT_SYNONYMS, **(business.get("synonyms") or {}))
    normalized = _normalize(raw, synonyms)
    handoff = business.get("handoff") or {}
    for word in dict.fromkeys(DEFAULT_HANDOFF + handoff.get("keywords", [])):
        if word and _normalize(word) in normalized:
            return _result(raw, "handoff_policy", "handoff",
                           "命中商家配置的人工处理范围：%s" % word,
                           handoff_reason="需要人工处理")
    if re.search(r"(?:买|要|拿)\s*[\d一二三四五六七八九十百千万两]+\s*(?:件|个|套).*(?:便宜|优惠|折扣)", normalized):
        return _result(raw, "handoff_bulk_quote", "handoff", "批量特殊报价需要商家确认",
                       handoff_reason="特殊报价")

    rules = _intent_rules(business)
    matched = [(name, rule, max((len(word) for word in rule.get("keywords", [])
                                 if word and _normalize(word) in normalized), default=0))
               for name, rule in rules.items()]
    matched = [(name, rule, score) for name, rule, score in matched if score]
    if "发货" in normalized and any(word in normalized for word in ("从", "在", "哪", "发货地")):
        matched.append(("shipping_origin", rules["shipping_origin"], 20))
    if not matched and pending_intent in rules:
        matched.append((pending_intent, rules[pending_intent], 1))
    if not matched:
        return _result(raw, "handoff_unknown", "handoff", "未识别出可自动回答的意图",
                       handoff_reason="意图不明")
    matched.sort(key=lambda item: -item[2])
    names = list(dict.fromkeys(item[0] for item in matched))
    if _selected_intent:
        if _selected_intent not in names:
            return _result(raw, "handoff_unknown", "handoff", "指定意图未命中",
                           handoff_reason="意图不明")
        matched = [item for item in matched if item[0] == _selected_intent]
    elif len(names) > 1:
        if len(names) > 3:
            return _result(raw, "need_clarification", "ask_followup", "一次包含过多问题",
                           draft_reply="您问到了几个不同的问题，请先告诉我最想确认哪一项？")
        decisions = [classify(raw, store, products, product_hint, context_product,
                              pending_intent=pending_intent, handoff_pending=handoff_pending,
                              _selected_intent=name) for name in names]
        blocked = next((item for item in decisions if item["action"] == "handoff"), None)
        if blocked:
            return blocked
        followups = [item for item in decisions if item["action"] == "ask_followup"]
        if len(followups) > 1:
            return _result(raw, "need_clarification", "ask_followup", "需要补充多个条件",
                           draft_reply="请先提供商品名称或商品卡片，以及收货地区，我帮您逐项核对。")
        replies = [item for item in decisions if item["action"] == "reply"]
        draft = "\n".join(item["draft_reply"] for item in replies + followups)
        if followups:
            return dict(followups[0], draft_reply=draft, evidence=[
                item.get("evidence") for item in replies], multi_intents=names)
        return _result(raw, "pre_sale_multi", "reply", "多个意图均已核对资料",
                       evidence=[item.get("evidence") for item in replies],
                       multi_intents=names, draft_reply=draft)
    name, rule, _ = matched[0]
    if handoff_pending and not pending_intent and name in ("delivery_estimate", "public_promotion"):
        independent = ("快递", "物流", "包裹", "商品", "运费") if name == "delivery_estimate" else (
            "商品", "优惠券", "活动价", "折扣")
        if not any(word in normalized for word in independent):
            return _result(raw, "handoff_context", "handoff",
                           "人工待办尚未处理，当前问法可能延续上一问题",
                           handoff_reason="需结合上一问题判断")
    source = rule.get("source")
    product = _product(normalized, products, product_hint, context_product, synonyms)
    if source in PRODUCT_SOURCES and not product:
        return _result(raw, "need_product", "ask_followup", "需要确认商品",
                       confidence="medium", missing=["product"], pending_intent=name,
                       draft_reply="请提供商品名称或发送商品卡片，我帮您核对。")
    if product and (product.get("sample_data") or product.get("source") == "sample"):
        return _result(raw, "handoff_data_gap", "handoff", "商品资料为样例数据",
                       handoff_reason="商品资料未经核实")
    if name == "inventory" and product.get("status") in ("off_shelf", "discontinued"):
        return _result(raw, "pre_sale_inventory", "reply", "商品状态已明确标注为下架",
                       product_id=product["product_id"], product_title=product["title"],
                       evidence=["product.status"],
                       draft_reply="%s目前已下架，是否重新销售请以商品页面为准。" % product["title"])
    region = next((place for place in REGIONS if place in normalized), None)
    if name == "delivery_estimate" and not region:
        regions = (business.get("shipping") or {}).get("delivery_by_region") or {}
        if regions:
            return _result(raw, "pre_sale_delivery_estimate", "ask_followup",
                           "需要确认收货地区", confidence="medium", missing=["destination"],
                           pending_intent=name, draft_reply="请问收货地是哪个省或直辖市？我帮您查预计时效。")

    if name == "public_promotion" and not rule.get("answer"):
        fact = _fact(source, business, product)
        fact, metadata, _ = _approved_fact(fact)
        note = ("当前公开优惠：%s。" % fact) if fact else "当前优惠以商品页面和平台活动为准。"
        return _result(raw, "pre_sale_public_promotion", "reply", "仅说明公开活动",
                       evidence=[source, metadata], draft_reply=note + "客服无法承诺额外让利。")

    if rule.get("answer"):
        answer = rule["answer"]
        evidence = ["business.intents.%s.answer" % name]
    else:
        raw_fact = _fact(source, business, product, region)
        fact, metadata, error = _approved_fact(raw_fact)
        if error:
            return _result(raw, "handoff_data_gap", "handoff",
                           "%s：%s" % (error, source), missing=[source],
                           handoff_reason="资料缺失或过期")
        answer = _format_fact(source, fact, product, region)
        evidence = [source, metadata]
    return _result(raw, "pre_sale_%s" % name, "reply", "已核对商家配置资料",
                   product_id=product.get("product_id") if product else None,
                   product_title=product.get("title") if product else None,
                   evidence=evidence, draft_reply=answer)
