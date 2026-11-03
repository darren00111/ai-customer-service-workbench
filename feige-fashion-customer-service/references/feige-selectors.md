# 飞鸽选择器基线（feige-selectors）

> 校准日期：2026-09-22　校准环境：Windows / Chrome 133+ / 飞鸽 PC 卖家版 v2
> 校准会话：`热力台球`（店铺：热力台球工作室）
> 状态：✅ 已通过「连续 3 次读取完全一致」验收

---

## 1. 结论先行

飞鸽前端大量使用 **hash 类名**（如 `msgItemWrap` 之外的 `mLCJMMXV5O16gqWK_ZhX`），
这类类名**一次发版就会变，绝对不能写进选择器**。

但有两类锚点是稳定的，本项目**只依赖这两类**：

| 锚点类型 | 稳定性 | 例子 |
|---|---|---|
| `data-qa-id` 属性 | ★★★★★ 语义化、随功能走 | `qa-send-message-textarea` |
| 语义类名 / 语义 id | ★★★★☆ 人工命名 | `.messageList` `#im-input-box` |

**铁律：选择器只能用 `data-qa-id` 和语义类名/id，禁止用 hash 类名。**

---

## 2. 控件清单（Stage 1 校准成果）

### 2.1 会话列表

| 控件 | 选择器 | 备注 |
|---|---|---|
| 会话列表项 | `[data-qa-id="qa-conversation-chat-item"]` | 实测 23 项，含「最近联系」「最近联系人」两组重复项 |
| 列表滚动容器 | `#chantListScrollArea` → `.scroller` → `.scroller_content` → `.list_items` | |
| 当前会话页签 | `[data-qa-id="qa-active-chat-tab"]` / `#rc-tabs-0-tab-current` | 文案「当前会话」 |
| 最近联系页签 | `[data-qa-id="qa-last-chat-tab"]` / `#rc-tabs-0-tab-history` | 文案「最近联系」 |
| 平台消息页签 | `[data-qa-id="qa-platform-message-tab"]` | |

⚠️ **坑**：`当前会话` 页签实测为空（会话关闭后不在此列），真实联系人都在 `最近联系`。
测试时务必先点 `qa-last-chat-tab`。

⚠️ **坑**：选中态类名 `wmvLQcpt39Hk9PSISrlN` 是 hash 类，**不可靠**，
仅作启发式；判定「当前打开的是哪个会话」应优先读 `#topbar-left-info`。

### 2.2 会话头部

| 控件 | 选择器 | 实测值 |
|---|---|---|
| 买家昵称 | `#topbar-left-info` | `热力台球`（尾部带「添加备注」，需剥离） |
| 买家消费画像 | `#topbar-sec-container` | `抖音-店铺页 / 店铺消费 ¥292.40 (18次) / 退货率 5%` |

### 2.3 消息区

| 控件 | 选择器 |
|---|---|
| 消息气泡 | `[data-qa-id="qa-message-warpper"]`（类名 `.msgItemWrap`） |
| 消息列表容器 | `.messageList` → `.scroller` → `.scroller_content` → `.list_items` |
| 已读回执 | `[data-qa-id="qa-message-read-mark"]` |

**消息角色判定**（飞鸽不给 role 属性，靠多重信号，顺序即优先级）：

```
1) /用户超时未回复|当前会话已长时间未回复|客服.+接入|会话已结束|系统消息|系统自动/  → system
2) /抖音电商智能客服发送|智能客服发送/                                          → ai
3) /商家配置发送/                                                              → merchant
4) /(已读|未读)/                          ← 只有商家侧发出的消息才带回执          → agent
5) /[^\n]{0,12}发送\s*$/                                                       → agent
6) 兜底                                                                        → buyer
```

⚠️ **坑**：不要用「气泡水平位置」判断左右对齐。
`.msgItemWrap` 实测宽度撑满容器（495px），中心点全在中间，判不出来。
必须走上面的文本信号。

### 2.4 输入区

| 控件 | 选择器 | 实测 |
|---|---|---|
| 输入区根 | `#im-input-box` | 高度 152px，可拖拽调高 |
| 输入框 | `[data-qa-id="qa-send-message-textarea"]` | placeholder：`发送给 热力台球，使用Enter 发送消息，使用Ctrl+Enter 换行` |
| 发送按钮 | `[data-qa-id="qa-send-message-button"]` | 文案 `发 送`（中间有空格） |
| 表情 / 图片 / 文件 / 视频 | `qa-send-emoji` / `qa-send-img` / `qa-send-file` / `qa-send-video` | |
| 快捷短语 | `[data-qa-id="qa-quick-replay"]` | |

⚠️ **坑**：发送按钮文案是 `发 送`（字间有空格），
用 `text == '发送'` 精确匹配会**永远匹配不到**。

⚠️ **坑**：Enter 直接发送、Ctrl+Enter 换行。
自动回复脚本如果直接 `press Enter`，等于**真发消息**。

### 2.4.1 写入与发送机制（Stage 3 实测）

输入框是**原生 `TEXTAREA`**（全页唯一一个 textarea），不是 contenteditable。
它是 **React 受控组件**，这一点决定了写入方式：

| 做法 | 结果 |
|---|---|
| `el.value = text` | ❌ 界面上能看到字，但 React state 还是空 —— 点发送**发出的是空消息** |
| `Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(el, text)` + `dispatchEvent(new Event('input',{bubbles:true}))` | ✅ state 与界面同步 |

**怎么判断 React state 真的跟上了**：输入区右下角的 **`N/800` 字数计数**。
它是 state 的反映，不是 DOM 的反映。计数不跟随 value，就说明没同步，必须中止发送。
实测 200 字符含换行的文本：`counter == value.length == 200`，换行按 1 个字符计。

**发送按钮**是 `<div role="button">`（不是 `<button>`），
用合成指针事件序列（`pointerdown/mousedown/pointerup/mouseup`）+ `click()` 触发，
比裸 `click()` 兼容性更好。

**发送成功的判定信号**：**输入框被清空**（`value === ''` 且计数回 0）。
只看到"字消失了"不够——必须同时比对计数。清空失败即判为发送失败。

**分两步做的原因**：写入与提交拆成两次独立调用，中间由 Python 校验 state。
一步到位的话，「写了但没同步」在 UI 上完全看不出来，发出去就是一条空消息。

代码位置：`scripts/feige_send.js`（页面内逻辑）+ `scripts/send_reply.py`（编排与闸门）。

⚠️ **坑**：`feige_send.js` 里的占位符（`ACTION` / `TEXT_B64`）是全局字符串替换注入的。
**除注入点外，文件里不能再出现同名字面量**——一旦出现在比较表达式里，
替换后会退化成自比较（恒真），静默吃掉全部文本。这个坑已经踩过一次。

### 2.5 侧边业务区

| 控件 | 选择器 / 位置 |
|---|---|
| 左侧面板根 | `#leftSide` |
| 咨询商品区 | 当前测试会话显示 `暂无咨询商品` |
| 订单页签 | `qa-order-all-type-tab` / `qa-order-unfinish-type-tab` / `qa-order-pending-type-tab` / `qa-order-finished-type-tab` / `qa-order-closed-type-tab` |
| 订单搜索框 | `[data-qa-id="qa-user-order-search"]` |
| 发物流卡 | `[data-qa-id="qa-check-logistics-button"]` |
| 邀评 | `[data-qa-id="qa-invite-comment-button"]` |
| 打款 | `[data-qa-id="qa-small-payment-button"]` |
| 拉黑 | `[data-qa-id="qa-block"]` |

### 2.6 尚未校准

- **咨询商品卡片**：测试会话没有咨询商品，只有订单商品。
  等一个有「咨询商品」的真实会话再补。
  ⚠️ 它是**自动答复率的天花板**：顾客不点商品卡直接打字时，
  它是唯一可靠的商品来源，没有它只能靠顾客措辞去猜。
- **转人工入口**：当前账号已是人工客服，入口不可见（`转接`/`结束会话` 均未命中）。
  需在有智能客服接待中的会话里补测。
  ⚠️ **标定之前，转人工的占位回复不应发出**：占位话术写着「已为您转接」，
  我们其实并没有真的转接，顾客会干等一个不会来的人。
  现行策略是 `reply_once.py` 默认拦下 handoff 草稿，需显式 `--send-handoff` 才发。

---

## 3. 打开飞鸽页面的正确姿势

### 3.1 为什么不能用 `agent-browser open`

实测结论：

- `agent-browser open` 默认**无头**，`--headed` 在本机**不生效**（进程 `MainWindowHandle = 0`）
- 更坑的是：带 `--headed` 的 `open` 与不带参数的后续命令，会连到**不同的守护进程实例**，
  导致 `eval` 打到 `about:blank`，看起来像"页面没加载"

### 3.2 可用的做法（已验证）

用 Chrome 起一个**带调试端口的可见窗口**，再让 agent-browser 通过 CDP 接管：

```bash
"C:/Program Files/Google/Chrome/Application/chrome.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="C:/Users/1154/AppData/Local/Temp/wb-chrome-profile" \
  --no-first-run --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  "https://im.jinritemai.com/pc_seller_v2/main/workspace"
```

然后所有操作都显式带 `--cdp 9222`：

```bash
agent-browser --cdp 9222 eval "<js>"
```

好处：窗口看得见、登录态可持久化在某个 user-data-dir、不被 `--headed` 的坑影响。

### 3.3 编码：JS 必须走 base64

`agent-browser eval` 只吃命令行字符串，Windows 命令行是 GBK 代码页，
直接传中文 JS 必乱码。链路必须是：

```
JS 文件(UTF-8) → base64 → atob → Uint8Array → TextDecoder('utf-8') → eval
```

⚠️ **最容易漏的一步**：`eval(atob(b64))` 是**错的**。
`atob` 产出 Latin-1（一字节一个 code unit），中文会被拆成乱码，
表现为「按中文文本查找的元素全部落空」——非常隐蔽，因为英文属性名全都正常。
必须显式用 `TextDecoder` 重解码。

封装在 `scripts/cdp_eval.py`（`--emit-payload`）。

---

## 4. 隐私

会话数据含真实买家昵称、消费金额、收货信息。
落盘前一律过 `redact()`：手机号 `前3+****+后4`、身份证、长数字 `***+后4`。
`data/conversations/` 已在 `.gitignore` 中排除。
