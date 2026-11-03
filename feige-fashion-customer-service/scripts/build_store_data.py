# -*- coding: utf-8 -*-
"""把示例商品 JSON 转成店铺知识库 YAML。

用途：一次性构建工具。把
    data/samples/clothing_products_sample.json
转成
    data/stores/shop_001/products/p1001.yaml ... p1010.yaml

⚠️ 数据来源说明：样本 JSON 是**构造的示例数据**（sample_data=true），
   不是从抖音实时采集所得。生成的 yaml 会带 `source: sample` 标记。

字段映射（样本 -> 知识库）：
    fabric            -> material        （对齐 knowledge-intake.md 约定字段名）
    无                -> variants        （本脚本补充：颜色 x 尺码库存矩阵）
    size_chart.rows   -> 保持 rows 列表形式（空值 null，便于程序遍历）

用法：
    python scripts/build_store_data.py
"""
from __future__ import annotations

import io
import json
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SAMPLE = os.path.join(ROOT, "data", "samples", "clothing_products_sample.json")
OUT_DIR = os.path.join(ROOT, "data", "stores", "shop_001", "products")

# ---------------------------------------------------------------------------
# 颜色 x 尺码 库存矩阵。样本 JSON 只到商品级库存，这里补到 SKU 级，
# 目的是能回答「有没有 M 码」这类问题。0 表示该组合售罄。
# ---------------------------------------------------------------------------
VARIANTS = {
    "P1001": [
        {"color": "米白",    "sizes": {"S": 20, "M": 25, "L": 15, "XL": 8}},
        {"color": "雾霾蓝",  "sizes": {"S": 18, "M": 30, "L": 0,  "XL": 12}},
    ],
    "P1002": [
        {"color": "深蓝",    "sizes": {"26": 24, "27": 30, "28": 36, "29": 22, "30": 18}},
        {"color": "浅蓝",    "sizes": {"26": 20, "27": 28, "28": 30, "29": 16, "30": 0}},
    ],
    "P1003": [
        {"color": "白",      "sizes": {"M": 0, "L": 0, "XL": 0, "2XL": 0}},
        {"color": "黑",      "sizes": {"M": 0, "L": 0, "XL": 0, "2XL": 0}},
        {"color": "灰",      "sizes": {"M": 0, "L": 0, "XL": 0, "2XL": 0}},
    ],
    "P1004": [
        {"color": "黑",      "sizes": {"M": 0, "L": 0, "XL": 0, "2XL": 0}},
        {"color": "米白",    "sizes": {"M": 0, "L": 0, "XL": 0, "2XL": 0}},
    ],
    "P1005": [
        {"color": "黑",      "sizes": {"S": 0,  "M": 20, "L": 18, "XL": 10}},
        {"color": "驼色",    "sizes": {"S": 12, "M": 15, "L": 12, "XL": 0}},
    ],
    "P1006": [
        {"color": "黑",      "sizes": {"S": 0, "M": 0, "L": 0, "XL": 0}},
        {"color": "卡其",    "sizes": {"S": 0, "M": 0, "L": 0, "XL": 0}},
    ],
    "P1007": [
        {"color": "燕麦",    "sizes": {"M": 40, "L": 45, "XL": 30, "2XL": 15}},
        {"color": "藏青",    "sizes": {"M": 35, "L": 40, "XL": 25, "2XL": 20}},
    ],
    "P1008": [
        {"color": "香槟",    "sizes": {"S": 0, "M": 0, "L": 0, "XL": 0}},
        {"color": "雾灰",    "sizes": {"S": 0, "M": 0, "L": 0, "XL": 0}},
    ],
    "P1009": [
        {"color": "黑",      "sizes": {"S": 22, "M": 34, "L": 28, "XL": 16}},
        {"color": "米白",    "sizes": {"S": 18, "M": 26, "L": 0,  "XL": 24}},
    ],
    "P1010": [
        {"color": "米白",    "sizes": {"M": 0, "L": 0, "XL": 0}},
        {"color": "浅灰",    "sizes": {"M": 0, "L": 0, "XL": 0}},
    ],
}


def build_material(fabric: dict) -> dict:
    """fabric -> material，字段名对齐 knowledge-intake.md。"""
    return {
        "composition": fabric.get("composition") or [],
        "lining": fabric.get("lining"),
        "weight_gsm": fabric.get("weight_gsm"),
        "elasticity": fabric.get("elasticity"),
        "thickness": fabric.get("thickness"),
        # 样本未提供洗护说明，故意留 None：
        # 用于验证「资料缺失时不编造」这条规则
        "care": None,
    }


def convert(item: dict) -> dict:
    pid = item["product_id"]
    return {
        "product_id": pid,
        "sku_code": item["sku_code"],
        "title": item["title"],
        "category": item["category"],
        "status": item["status"],
        "price_cny": item["price_cny"],
        "stock": item["stock"],
        "season": item["season"],
        "updated_at": item["updated_at"],
        "size_chart": {
            "basis": item["size_chart"]["basis"],   # 成衣尺寸 / 净尺寸，必须写明
            "unit": item["size_chart"]["unit"],
            "rows": item["size_chart"]["rows"],
        },
        "material": build_material(item.get("fabric") or {}),
        "fit": item.get("fit") or {},
        "variants": VARIANTS.get(pid, []),
        "source": "sample",
        "sample_data": True,
    }


def main() -> int:
    with io.open(SAMPLE, "r", encoding="utf-8") as f:
        samples = json.load(f)

    os.makedirs(OUT_DIR, exist_ok=True)

    written = []
    for item in samples:
        doc = convert(item)
        fname = doc["product_id"].lower() + ".yaml"
        fpath = os.path.join(OUT_DIR, fname)
        with io.open(fpath, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(
                doc, f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=200,
            )
        written.append(fname)

    print("generated %d files -> %s" % (len(written), OUT_DIR))
    for w in written:
        print("  ", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
