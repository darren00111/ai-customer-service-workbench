"""Canonical shop fields and evidence lookup after semantic interpretation."""
import re

PROVINCES = ("黑龙江", "内蒙古", "新疆", "宁夏", "西藏", "广西", "广东", "湖南", "湖北", "河南", "河北", "山东", "山西", "江苏", "浙江", "安徽", "福建", "江西", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "辽宁", "吉林", "海南", "北京", "上海", "天津", "重庆", "香港", "澳门", "台湾")
CITIES = {"深圳":"广东", "广州":"广东", "长沙":"湖南", "杭州":"浙江", "成都":"四川"}
ATTRIBUTE_GROUPS = {
    "供电方式": ("供电", "电源", "接啥电", "接什么电", "充电方式"),
    "材质": ("材质", "材料", "木头", "料子", "碳纤维"),
    "震手": ("震手", "手震", "震动"), "掉色": ("掉色", "褪色"),
    "长度": ("长度", "多长"), "尺寸": ("尺寸", "尺码", "胸围"),
    "起球": ("起球",), "硬度": ("硬度", "软硬"), "重量": ("重量", "多重"),
    "耐用性": ("耐用", "耐打", "寿命"),
}


def canonical_region(value, shipping=None):
    if not value:
        return value
    text = str(value).strip()
    for region in PROVINCES:
        if text.startswith(region):
            return region
    aliases = {**CITIES, **(shipping or {}).get("city_regions", {})}
    for city, region in aliases.items():
        if text in {city, city+"市"}:
            return canonical_region(region)
    return text


def attributes_asked(text):
    return [key for key, aliases in ATTRIBUTE_GROUPS.items() if any(word in text for word in aliases)]


def attribute_key(requested, attributes):
    if requested in attributes:
        return requested
    canonical = next((key for key, aliases in ATTRIBUTE_GROUPS.items() if requested in aliases), requested)
    group = ATTRIBUTE_GROUPS.get(canonical, ())
    for key in attributes:
        if key == canonical or key in group:
            return key
    # An electrical fact stored in a generic specification remains the source;
    # no derived voltage, wattage or unsupported feature is manufactured.
    if canonical == "供电方式":
        for key, raw in attributes.items():
            value = raw.get("value") if isinstance(raw, dict) else raw
            if isinstance(value, str) and re.search(r"USB|供电|电池|电源|充电|\d+V", value, re.I):
                return key
    return None


def recommendation_scope(products, understanding, state):
    scope = understanding.recommendation_ids
    kind = understanding.product_type or state.get("product_type", "")
    if kind:
        matches = [p for p in products if kind in {p.get("product_type"), p.get("category")} or
                   kind in p["title"] or any(kind == alias for alias in p.get("aliases", []))]
        if matches:
            return matches
        if not scope:
            return []
    if scope:
        return [p for p in products if p["product_id"] in scope]
    # Legacy offline recommendations can use an explicit product family in the
    # current product. Without one, ask instead of including unrelated items.
    selected = next((p for p in products if p["product_id"] == state.get("product_id")), None)
    if selected and selected.get("product_type"):
        return [p for p in products if p.get("product_type") == selected["product_type"]]
    return products if len(products) <= 2 else []
