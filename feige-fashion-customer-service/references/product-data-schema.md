# 店铺与商品资料结构

每家店单独保存一份配置。推荐至少包含以下字段。

## 店铺配置

| 字段 | 示例 | 说明 |
|---|---|---|
| store_id | shop_001 | 内部唯一标识 |
| store_name | 示例女装店 | 飞鸽显示名称 |
| shipping_policy | 48小时内发货 | 仅填写已批准话术 |
| default_express | 中通/极兔，以仓库为准 | 不确定时明确说明 |
| handoff_message | 已为您转接人工客服 | 售后占位回复 |
| notification_provider | personal_wechat | 默认通道；个人微信桌面端 |
| notification_target | 文件传输助手 | 必须使用精确会话名称，默认即此值 |

## 商品配置

| 字段 | 说明 |
|---|---|
| product_id / external_code | 抖店商品ID和内部货号 |
| title | 商品名称 |
| category | 裤装、连衣裙、外套等 |
| sku | 颜色、尺码及对应SKU |
| size_chart | 每个尺码的净尺寸或成衣尺寸，并标明口径 |
| fit | 修身、合身、宽松及版型备注 |
| material | 面料、里料和成分比例 |
| features | 厚薄、弹性、季节、工艺等有依据属性 |
| care | 洗涤与保养说明 |
| approved_faq | 已审核问题与答案 |
| prohibited_claims | 本商品不能承诺的表述 |
| updated_at | 资料更新时间 |

不要把不同颜色或批次可能变化的属性写成全商品统一结论。尺码表发生变化时保留更新时间，并确保旧链接不再被客服使用。

## 对话记录

为排查重复回复和持续改进，记录：`store_id`、脱敏会话ID、消息ID或消息摘要哈希、意图、知识来源、回复或转人工动作、置信状态、时间戳。不要在本地记录完整敏感个人信息。
