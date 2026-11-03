# data/ 目录说明

本目录存放店铺、商品与对话数据。

```
data/
├── stores/
│   └── shop_001/                        # 一家店一个目录
│       ├── store.yaml                   # 店铺级配置（话术、尺码对照、兜底策略）
│       ├── products/                    # 商品结构化资料，一商品一文件
│       │   ├── p1001.yaml
│       │   └── ...
│       └── raw/                         # 原始上传文件，原样保留
├── samples/
│   └── clothing_products_sample.json    # ⚠️ 构造的示例商品（非采集所得）
├── conversations/                       # 对话记录（阶段 4 启用）
└── last_read.json                       # L1 最近一次读取结果（可覆盖）
```

## 当前状态（2026-09-22）

| 项 | 状态 |
|---|---|
| `stores/shop_001/store.yaml` | ✅ 已建（含话术、尺码对照、兜底策略） |
| `stores/shop_001/products/` | ✅ 10 件商品，**构造的示例数据** |
| `stores/shop_001/raw/` | ⬜ 空，等真实资料 |
| `conversations/` | ⬜ 空，阶段 4 启用 |

**⚠️ 重要**：`products/*.yaml` 里的商品来自 `samples/clothing_products_sample.json`，
是**构造的拟真示例数据**（每个文件带 `sample_data: true` 和 `source: sample`），
**不是从抖店采集的真实商品**。它足够验证决策链路，但：

- `store.yaml` 的 `size_guide` 是**示例对照表**，必须商家核准后替换
- 商品价格、库存、尺码数据均不可用于真实客服答复

换成真实资料时，只需替换 `products/` 下的 yaml，**不用改任何代码**。

## 生成方式

`products/*.yaml` 由 `scripts/build_store_data.py` 从 `samples/*.json` 生成。
重新生成：

```
python scripts/build_store_data.py
```

需要 `pyyaml`（见项目根 `requirements.txt`）。

## 约定

- `raw/` 里的原始文件**不改不删**，可追溯
- `*.yaml` 由原始资料整理而来
- 冲突时以 `raw/` 为准，并在 yaml 中标注
- 不写入真实客户敏感信息（手机号、地址、订单号）

字段定义见 `../references/product-data-schema.md`。
入库流程与最小标准见 `../references/knowledge-intake.md`。
