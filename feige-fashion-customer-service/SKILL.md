---
name: feige-fashion-customer-service
description: Handle clothing-store customer conversations in the authenticated Feige web console. Read a conversation with the calibrated selectors, classify the latest customer message against the store's approved data, and draft or send a reply; answer product, size, fabric, stock, shipping, and care questions only from approved store data, and compare products by dimension rather than ranking them. Sending is off by default and every send passes write-then-verify gates. Pause automation and notify a human when a conversation involves returns, exchanges, refunds, complaints, missing product data, or uncertain claims; holding replies for such handoffs are withheld while the transfer control is uncalibrated. Notify a human through a WeCom group bot, or through supervised personal-WeChat desktop delivery into an operator-confirmed chat.
---

# 飞鸽服装智能客服

Operate only in an already authenticated Feige browser session. Keep each store's product data and policies isolated; never reuse one store's price, inventory, shipping promise, or after-sales rule in another store.

Before handling live conversations, read [references/operating-policy.md](references/operating-policy.md). When configuring a store or importing its data, also read [references/product-data-schema.md](references/product-data-schema.md) and [references/knowledge-intake.md](references/knowledge-intake.md). For browser and WeChat setup, read [references/setup.md](references/setup.md).

For the overall design and the current build stage, read [references/roadmap.md](references/roadmap.md). It records which layers are operational and what the next step is; trust it over assumptions about what is already built.

Calibrated Feige page controls live in [references/feige-selectors.md](references/feige-selectors.md). Use only `data-qa-id` attributes and semantic class or id names; hash class names change on every frontend release and must never be used as selectors.

How a message becomes a category, an action, and a reply draft is specified in [references/decision-logic.md](references/decision-logic.md). Read it before changing any classification word list or reply wording.

## Read a conversation (L1, read-only)

Start the browser, then read:

```
scripts\start_feige_browser.bat     # visible Chrome with CDP on 9222
scripts\read_feige.bat              # prints buyer, messages, latest buyer message
```

Or call it directly:

```
python scripts/read_conversation.py --emit-payload payload.txt
agent-browser --cdp 9222 --max-output 200000 eval "$(< payload.txt)" > raw.json
python scripts/read_conversation.py --from-raw raw.json
```

Pass several `--from-raw` files to compare runs; the script exits 4 if the reads disagree.

Never write into the input box or press Enter while reading: Enter sends immediately and Ctrl+Enter inserts a newline.

## Classify a message (L2, no side effects)

Decide category, action, and reply draft from the latest customer message plus the store's approved data. This step never touches the browser.

```
python scripts/classify.py --text "身高160，体重50公斤，买什么码" --product P1009
python scripts/classify.py --text "有M码吗" --context P1009
python scripts/classify.py --text "我要退货" --json
python scripts/classify.py --batch cases.json
```

### Locating the product first

Customers frequently do **not** arrive through a product card — they just type "有M码吗" with no product context at all. Product location is therefore its own step, resolved in three levels, highest priority first:

1. `--product` — the Feige product card for this conversation. The most reliable source, independent of how the customer words things. Pass it whenever the card is present.
2. Message text — the customer names or describes the item ("碎花的裙子", "阔腿裤", "针织开衫"). Weaker; ties are never guessed.
3. `--context` — the session's product slot: the product already confirmed earlier in this conversation. Carry it across turns so a bare "这个" or "它" keeps referring to it.

Which level resolved it is reported as `matched_by` (`card` / `text` / `context` / `ambiguous` / `card_miss`).

When the text matches several products equally, the result carries `candidates` and a draft that lists them for the customer to pick, instead of an empty "which product do you mean?". Once the customer answers with a number, write that product into the session slot.

Store-level categories — `pre_sale_policy`, `pre_sale_shipping`, `pre_sale_promo`, and every `handoff_*` — need no product. Answer them normally even when there is no card.

Rules, priority order, and wording constraints: [references/decision-logic.md](references/decision-logic.md).
Knowledge base layout and the minimum field set: [references/knowledge-intake.md](references/knowledge-intake.md).
Store data lives in `data/stores/<store_id>/` (`store.yaml` + `products/*.yaml`); requires `pyyaml`.

Non-negotiable rules:

- Answer only from the current store's approved data. If the data does not state it, hand off — never fill the gap from general clothing knowledge.
- After-sales, disputes, and quality complaints always hand off, even when the same message also contains a presale keyword such as "尺码".
- Distinguish the two kinds of missing information: missing **customer** details means ask a follow-up; missing **product** data means hand off.
- Size advice must say "更建议" or "可参考", and must state whether the size chart is 成衣尺寸 or 净尺寸. Never say "保证合身".
- A product card naming a product that is absent from the knowledge base is a `card_miss`. Hand off; do **not** ask the customer to restate anything.
- Never pick one of several equally-likely products. List them as candidates and let the customer choose.
- A ranking request — "你们哪个裤子材质最好", "这两款有什么区别" — gets a **dimension-by-dimension comparison, never a ranking**. Lay out what actually differs and which need each option suits, then ask which they care about. Never write 最好 / 最佳 / 第一 / 绝对 in the reply. Part of this is advertising-law exposure; the rest is honesty — composition and weight do not tell us what feels best to a particular customer.
- Greetings ("你好", "在吗", "在不在") must be **answered, never handed off**. Only urgent-sounding summons ("人呢", "怎么没人") count as a human request. The greeting branch runs **last**, so it can never swallow a real request: "你好我要退货" is still an after-sales handoff, "你好，这个能便宜吗" is still a bargain handoff.
- Bargaining ("还能便宜吗", "便宜点") always hands off, regardless of whether a product was identified. Never soften it into "which product do you mean?" — the customer would pick one and still get handed off, having been led to think there was room to negotiate.

Regression check before trusting a change:

```
python scripts/test_classify.py          # expect 51/51
```

## Send a reply (L3, has side effects)

This is the only step that speaks to a customer on the store's behalf, so it is **off by default**.

```
bash scripts/auto_reply.sh                    # read + classify + decide; nothing is sent
bash scripts/auto_reply.sh --send             # actually send
bash scripts/auto_reply.sh --send --force     # ignore "no new message"
```

Or drive the stages yourself (the browser action always runs in the shell — Python cannot spawn `agent-browser` here, it hangs):

```
python scripts/reply_once.py --emit payload.txt
agent-browser --cdp 9222 --max-output 200000 eval "$(< payload.txt)" > raw.json
python scripts/reply_once.py --from-raw raw.json --force --outbox outbox.txt
python scripts/send_reply.py --stage fill --text-file outbox.txt --emit p1.txt
# run p1.txt through agent-browser -> raw_fill.json, then:
python scripts/send_reply.py --stage fill --from-raw raw_fill.json --text-file outbox.txt
python scripts/send_reply.py --stage submit --emit p2.txt
# run p2.txt through agent-browser -> raw_sub.json, then:
python scripts/send_reply.py --stage submit --from-raw raw_sub.json --text-file outbox.txt --allow-send
```

The input box is a native, **React-controlled `TEXTAREA`**. Assigning `el.value` is not enough: the DOM shows the text while React state stays empty, and clicking send posts an **empty message**. Write through the native value setter, dispatch an `input` event, then verify the `N/800` character counter matches the text length before submitting. Details and the full parameter-injection caveat: [references/feige-selectors.md](references/feige-selectors.md) §2.4.1.

Never skip these gates:

- Write and submit are **two separate calls**. The check in between is exactly what catches an unsynchronised write.
- Refuse to write when the input box is not empty. Overwriting would paste two messages into one.
- At most one reply per customer message: the same buyer plus identical text inside a five-minute window is rejected.
- A send counts as successful **only when the input box is cleared**. A click that leaves text behind has failed.

`data/sent_log.jsonl` records every send. Never delete it — it is the duplicate guard's memory.

### Handoff replies are held back by default

The holding reply text says "已为您转接人工". The transfer control has **not** been calibrated, so no transfer actually happens. Promising something we do not do is worse than saying nothing: the customer waits for someone who is not coming. `reply_once.py` therefore holds every `handoff` draft unless `--send-handoff` is passed — only pass it once the transfer control is calibrated, or once the wording promises nothing.

## Handle a conversation

1. Read the selected store, customer conversation, linked product card, SKU selection, and any visible order state. Re-read the last customer message immediately before composing a reply.
2. Classify the request using the operating policy:
   - answerable presale question;
   - missing or conflicting information;
   - after-sales or other mandatory human handoff.
3. For an answerable presale question, use only the current store's approved product and policy data. Ask one concise follow-up when size advice lacks necessary information. Phrase size advice as a recommendation, not a guarantee.
4. Before sending, verify that the same store and conversation are still selected and that no human or automation has already answered the message. Send at most one reply for the current customer message.
5. For mandatory handoff, do **not** send the holding reply while the transfer control is uncalibrated — it promises a transfer that never happens. Stop automated replies for that conversation, use Feige's transfer or human-service control when available, and notify the configured personal-WeChat destination. Do not approve or reject a refund, return, exchange, compensation, or complaint.
6. Record the store, conversation reference, category, action, knowledge source, confidence, and timestamp. Do not put a full address, phone number, ID number, payment data, or unnecessary order details in notifications or logs.

## Browser safeguards

- Treat the live conversation as changing state. Re-check the target after any navigation, loading delay, or popup.
- Never send when the store, customer, product, or latest message is ambiguous.
- Avoid duplicate sends: compare the newest message and recent outgoing replies before clicking send.
- If two consecutive browser actions fail, stop that conversation and notify a human with the failure reason.
- Never batch-send the same generated answer across stores or conversations.
- Do not change store settings, refund state, order state, prices, inventory, or customer labels unless the user separately asks for that action.

## WeChat notification

Two channels exist. Both work; pick by supervision level.

| Channel | Reliability | Use for |
|---|---|---|
| WeCom group bot (`--channel webhook`) | Reliable, no GUI dependency | Real handoffs, including unattended |
| Personal WeChat desktop, `--current-chat` (`--channel desktop`) | Verified working | Supervised pilot with an operator present |
| Personal WeChat desktop, search-based | Not yet converged | Avoid |

Default target chat for the desktop channel: **文件传输助手**.

### Preferred path: WeCom group bot

```powershell
python scripts/notify_wecom.py --store "店铺A" --customer "会话尾号1234" --category "退换货" --summary "顾客申请换大一码，订单状态待核实" --channel webhook --dry-run
```

Keep `WECOM_WEBHOOK_URL` in the environment only — never in files, logs, or chat responses. Drop `--dry-run` only after the user has authorized real sending.

### Verified path: personal WeChat desktop via `--current-chat`

The user has granted permission to drive the local computer, so the agent may focus the WeChat window and drive it. **The verified working mode is `--current-chat`:** the operator opens and confirms the target chat by hand, then the script pastes and sends into it. A real delivery through this mode was confirmed on 2026-09-22.

The script cannot verify a chat title on its own. That is why `--current-chat` puts the title check on the operator, and why the search-based variant stays unproven.

```powershell
python scripts/notify_wecom.py --store "店铺A" --customer "会话尾号1234" --category "退换货" --summary "顾客申请换大一码，订单状态待核实" --current-chat --send --confirm-send
```

Known constraints on Weixin.exe 4.x:

- The client is fully self-drawn: its UI Automation tree exposes no editable controls, so keyboard driving is the only option.
- The window class is `Qt51514QWindowIcon` and its visible title is **not** "微信". Locate it by class name via `find_wechat_hwnd()`, never by title.
- **Clipboard text must go through `set_clipboard_unicode()` (Win32 `CF_UNICODETEXT`), never the `clip.exe` pipe.** The pipe caused GBK mojibake in WeChat inputs — the root cause of every earlier failed delivery. If garbage text ever appears in a WeChat input again, suspect the clipboard encoding path first.
- The search-based variant additionally needs the Down arrow after pasting the chat name, and an English IME layout. It has not been converged; prefer `--current-chat`.

### WeChat delivery flow

1. Build the redacted message. Never paste un-redacted content.
2. Prepare only (no desktop action):

```powershell
python scripts/notify_wecom.py --store "店铺A" --customer "会话尾号1234" --category "退换货" --summary "顾客申请换大一码，订单状态待核实"
```

3. Verify in the prepared output that the store, masked conversation tail, category, and summary are correct.
4. Confirm the target chat is open in WeChat and that its title matches exactly.
5. Request action-time confirmation from the user for this exact message and target.
6. After that confirmation, deliver with `--current-chat --send --confirm-send`.

A real desktop send requires both `--send` and `--confirm-send`. Treat `--confirm-send` as representing the user's already-granted standing permission, not as a licence to skip step 5 on a new or ambiguous target.

### Guardrails

- Verify the exact chat title by eye before sending. The script cannot do it for you. If it is ambiguous, stop and report.
- If WeChat is logged out, locked, minimized in a way the script cannot restore, or a different window captures the keystrokes, stop and report. Do not retry blindly.
- **Confirm the message actually appeared in the target chat.** A script exit code of 0 only means the keystrokes were dispatched — it does not prove delivery or correctness. Treat an unverified send as a failure and keep the Feige handoff state.
- Personal-WeChat desktop automation needs an unlocked, interactive session with a human present. It is never suitable for unattended operation.
- Never batch-send the same generated answer across chats or stores.
