/* 飞鸽常驻页面代理（Feige Agent 注入脚本）
 *
 * 与旧方案的差别：旧方案每次操作都把整段 JS 当字符串发进页面执行，一边一次
 * 进程启动。这里改为「注入一次，常驻内存」，之后每次调用只走一次 CDP 往返
 * （实测 0.17 ms）。页面刷新会丢注入，由 Python 侧检测 __FGA.__v 后重注。
 *
 * 安装：window.__FGA
 * 方法：read() / status() / fill(text) / submit() / listConvs() / clickConv(i)
 *
 * 安全约束（每条对应一次真实事故）：
 *   1) 只写输入框、只点发送按钮，绝不点击别处
 *   2) submit 前必须确认输入框非空 —— 否则就是发空消息给客户
 *   3) fill 之后校验 value 与「N/800」计数，React state 没跟上就拒绝发送
 */
(function () {
  if (window.__FGA && window.__FGA.__v === 8) return 'already';

  var V = 8;
  var activeConversationKey = '';
  /* 平台通知必须判在买家之前，否则「抖音电商智能客服已开启」这类文案
     会被当成买家消息送进判定，甚至被当成最后一条买家话来回复。 */
  var SYSTEM_PAT = /用户超时未回复|当前会话已长时间未回复|会话已结束|会话已关闭|系统消息|系统自动|客服.+接入|智能客服已(开启|关闭)|已接入(人工|智能客服)|人工客服为您服务/;
  var TA_SEL = '[data-qa-id="qa-send-message-textarea"]';
  var BTN_SEL = '[data-qa-id="qa-send-message-button"]';
  var CONV_SEL = '[data-qa-id="qa-conversation-chat-item"]';
  var BUBBLE_SEL = '[data-qa-id="qa-message-warpper"]';
  /* 侧栏三个页签：当前会话 / 最近联系 / 平台消息。
     会话项只在「有内容」的页签里渲染，所以列会话前必须先切对页签。 */
  var TAB_SEL = '[data-qa-id$="-tab"]';

  function txt(el) { return el ? (el.innerText || el.textContent || '').trim() : ''; }
  function all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function comparable(s) { return String(s || '').replace(/\s*\/\s*/g, ' ').replace(/\s+/g, ' ').trim(); }

  function convKey(el, index) {
    var attrs = ['data-conversation-id', 'data-id', 'data-user-id', 'data-im-id'];
    for (var i = 0; i < attrs.length; i++) {
      var value = el.getAttribute(attrs[i]);
      if (value) return attrs[i] + ':' + value;
    }
    var link = el.querySelector('a[href]');
    if (link && link.getAttribute('href')) return 'href:' + link.getAttribute('href');
    var lines = txt(el).split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
    return 'fallback:' + (lines[0] || 'unknown') + ':' + index;
  }

  function detectRole(bubble) {
    var t = txt(bubble);
    if (SYSTEM_PAT.test(t)) return 'system';
    if (/抖音电商智能客服发送|智能客服发送/.test(t)) return 'ai';
    if (/商家配置发送/.test(t)) return 'merchant';
    if (/(已读|未读)/.test(t)) return 'agent';
    if (/[^\n]{0,12}发送\s*$/.test(t)) return 'agent';
    return 'buyer';
  }

  function extractContent(bubble) {
    var t = txt(bubble);
    var lines = t.split('\n').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    var drop = /^(\d{1,2}:\d{2}(:\d{2})?|已读|未读|系统消息|智能客服|商家配置发送|抖音电商智能客服发送|.*发送|0\/\d+)$/;
    return lines.filter(function (s) { return !drop.test(s); }).join(' / ').slice(0, 500);
  }

  function extractTime(bubble) {
    var m = txt(bubble).match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
    return m ? m[1] : '';
  }

  function ta() { return document.querySelector(TA_SEL); }
  function btn() { return document.querySelector(BTN_SEL); }

  function counter() {
    var box = document.getElementById('im-input-box');
    if (!box) return null;
    var m = (box.innerText || '').match(/(\d+)\s*\/\s*800/);
    return m ? parseInt(m[1], 10) : null;
  }

  function snap() {
    var t = ta(), b = btn();
    return {
      value: t ? t.value : null,
      valueLen: t ? t.value.length : -1,
      counter: counter(),
      hasTextarea: !!t,
      hasButton: !!b,
      buttonText: b ? (b.innerText || '').replace(/\s+/g, '') : null,
      buttonDisabled: b ? (!!b.disabled || b.getAttribute('aria-disabled') === 'true') : null
    };
  }

  function setNativeValue(el, val) {
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value') ||
               Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
    if (desc && desc.set) { desc.set.call(el, val); return true; }
    el.value = val;
    return false;
  }

  function read() {
    var out = { url: location.href, ts: Date.now(), ok: true, warnings: [] };
    var topLeft = document.querySelector('#topbar-left-info');
    var topSec = document.querySelector('#topbar-sec-container');
    out.buyerName = topLeft ? txt(topLeft).replace(/添加备注\s*$/, '').trim() : '';
    var listed = listConvs();
    var current = listed.filter(function (c) { return c.isCurrent; })[0];
    if (current) activeConversationKey = current.key;
    out.conversationKey = activeConversationKey || ('buyer:' + out.buyerName);
    out.buyerMeta = topSec ? txt(topSec).replace(/\n/g, ' ').slice(0, 120) : '';

    var bubbles = all(BUBBLE_SEL);
    out.bubbleCount = bubbles.length;
    out.messages = bubbles.map(function (b, i) {
      var attrs = ['data-message-id', 'data-msg-id', 'data-id', 'data-item-id'];
      var messageKey = '';
      for (var k = 0; k < attrs.length; k++) {
        if (b.getAttribute(attrs[k])) { messageKey = attrs[k] + ':' + b.getAttribute(attrs[k]); break; }
      }
      return { role: detectRole(b), time: extractTime(b), content: extractContent(b),
               key: messageKey, visibleIndex: i };
    });

    var buyers = out.messages.filter(function (m) { return m.role === 'buyer'; });
    out.latestBuyerMessage = buyers.length ? buyers[buyers.length - 1].content : '';
    out.latestBuyerTime = buyers.length ? buyers[buyers.length - 1].time : '';
    out.latestBuyerKey = buyers.length ? buyers[buyers.length - 1].key : '';
    out.buyerMessageCount = buyers.length;
    out.latestMessage = out.messages.length ? out.messages[out.messages.length - 1].content : '';
    out.lastRole = out.messages.length ? out.messages[out.messages.length - 1].role : '';

    var prodRoot = document.querySelector('#leftSide');
    out.productPanel = prodRoot ? txt(prodRoot).replace(/\n/g, ' | ').slice(0, 200) : '';

    out.input = snap();
    if (!bubbles.length) out.warnings.push('no_bubbles');
    return out;
  }

  function status() { return { ok: !!ta(), snap: snap() }; }

  function fill(text) {
    var t = ta();
    if (!t) return { ok: false, reason: 'no_textarea' };
    var s = String(text == null ? '' : text);
    var r = { requestedLen: s.length, before: snap() };
    t.focus();
    setNativeValue(t, '');
    t.dispatchEvent(new Event('input', { bubbles: true }));
    r.usedNativeSetter = setNativeValue(t, s);
    t.dispatchEvent(new Event('input', { bubbles: true }));
    t.dispatchEvent(new Event('change', { bubbles: true }));
    r.after = snap();
    r.valueMatches = t.value === s;
    r.counterSynced = (r.after.counter === s.length);
    r.ok = r.valueMatches && r.counterSynced;
    if (!r.ok) r.reason = !r.valueMatches ? 'value_mismatch' : 'react_state_not_synced';
    return r;
  }

  async function submit() {
    var t = ta(), b = btn();
    if (!t) return { ok: false, reason: 'no_textarea', deliveryStatus: 'not_sent', safeToRetry: true };
    if (!b) return { ok: false, reason: 'no_send_button', deliveryStatus: 'not_sent', safeToRetry: true };
    if (!t.value || !t.value.trim()) return { ok: false, reason: 'empty_input_refuse_to_send', deliveryStatus: 'not_sent', safeToRetry: true };
    if (!!b.disabled || b.getAttribute('aria-disabled') === 'true') {
      return { ok: false, reason: 'send_button_disabled', deliveryStatus: 'not_sent', safeToRetry: true };
    }
    var intended = t.value;
    var beforeMessages = read().messages || [];
    var beforeCount = beforeMessages.length;
    var isMatchingOutgoing = function (m) {
      return m.role !== 'buyer' && m.role !== 'system' &&
             comparable(m.content) === comparable(intended);
    };
    var beforeMatches = beforeMessages.filter(isMatchingOutgoing).length;
    var r = { intendedLen: intended.length, before: snap(), beforeMessageCount: beforeCount,
              beforeMatchingBubbles: beforeMatches };
    var box = b.getBoundingClientRect();
    var opts = { bubbles: true, cancelable: true,
                 clientX: box.left + box.width / 2, clientY: box.top + box.height / 2, view: window };
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach(function (ev) {
      var E = (ev.indexOf('pointer') === 0 && window.PointerEvent) ? PointerEvent : MouseEvent;
      b.dispatchEvent(new E(ev, opts));
    });
    b.click();
    var deadline = Date.now() + 3500;
    while (Date.now() < deadline) {
      await new Promise(function (resolve) { setTimeout(resolve, 80); });
      var afterRead = read();
      var afterMessages = afterRead.messages || [];
      var afterMatches = afterMessages.filter(isMatchingOutgoing).length;
      var matched = afterMatches > beforeMatches;
      r.after = snap();
      r.cleared = (t.value === '');
      r.afterMessageCount = afterMessages.length;
      r.afterMatchingBubbles = afterMatches;
      if (matched) {
        r.ok = true;
        r.deliveryStatus = 'confirmed';
        r.safeToRetry = false;
        r.confirmedBy = 'outgoing_bubble';
        return r;
      }
    }
    r.after = snap();
    r.cleared = (t.value === '');
    r.ok = false;
    r.deliveryStatus = 'unknown';
    r.safeToRetry = false;
    r.reason = r.cleared ? 'delivery_unconfirmed_after_click' : 'click_result_unknown';
    return r;
  }

  function tabs() {
    return all(TAB_SEL).map(function (el, i) {
      var qa = el.getAttribute('data-qa-id') || '';
      return { i: i, qa: qa, name: txt(el),
               // 选中态判据不靠 qa-id（「当前会话」这个页签的 id 本身就叫
               // qa-active-chat-tab，并不代表它被选中），只认 class / aria。
               selected: /(^|\s|-)(active|selected|checked)/i.test(el.className || '')
                         || el.getAttribute('aria-selected') === 'true' };
    });
  }

  function clickTab(name) {
    var list = all(TAB_SEL);
    var el = null;
    for (var k = 0; k < list.length; k++) {
      if (txt(list[k]).indexOf(name) >= 0) { el = list[k]; break; }
    }
    if (!el) {
      return { ok: false, reason: 'tab_not_found',
               tabs: list.map(function (e) { return txt(e); }) };
    }
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach(function (ev) {
      var E = (ev.indexOf('pointer') === 0 && window.PointerEvent) ? PointerEvent : MouseEvent;
      el.dispatchEvent(new E(ev, { bubbles: true, cancelable: true, view: window }));
    });
    el.click();
    return { ok: true, name: txt(el) };
  }

  function listConvs() {
    var currentName = txt(document.querySelector('#topbar-left-info')).replace(/添加备注\s*$/, '').trim();
    return all(CONV_SEL).slice(0, 40).map(function (it, i) {
      var t = txt(it);
      var lines = t.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      return { i: i, key: convKey(it, i), name: lines[0] || '', preview: lines.slice(1).join(' ').slice(0, 40),
               unread: /\d+\s*$/.test(lines[0] || '') || t.indexOf('未读') >= 0,
               isCurrent: lines[0] === currentName };
    });
  }

  function clickConv(i) {
    var items = all(CONV_SEL);
    if (i < 0 || i >= items.length) return { ok: false, reason: 'index_out_of_range', count: items.length };
    var el = items[i];
    activeConversationKey = convKey(el, i);
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach(function (ev) {
      var E = (ev.indexOf('pointer') === 0 && window.PointerEvent) ? PointerEvent : MouseEvent;
      el.dispatchEvent(new E(ev, { bubbles: true, cancelable: true, view: window }));
    });
    el.click();
    return { ok: true, count: items.length, name: txt(el).split('\n')[0] || '' };
  }

  function openConv(key, name) {
    var rows = listConvs();
    var stable = key && key.indexOf('fallback:') !== 0;
    var matches = stable ? rows.filter(function (row) { return row.key === key; }) : [];
    if (!stable && !matches.length && name) {
      matches = rows.filter(function (row) { return row.name === name; });
    }
    if (matches.length !== 1) return { ok: false, reason: matches.length ? 'ambiguous' : 'not_found' };
    var row = matches[0];
    if (activeConversationKey === row.key &&
        txt(document.querySelector('#topbar-left-info')).indexOf(row.name) === 0) {
      return { ok: true, switched: false, key: row.key };
    }
    var opened = clickConv(row.i);
    opened.switched = !!opened.ok;
    opened.key = row.key;
    return opened;
  }

  var sidebarRevision = 0;
  var sidebarSignature = '';
  var sidebarWaiters = [];
  var sidebarTimer = null;
  function sidebarFingerprint() {
    return listConvs().map(function (row) {
      return row.key + '|' + row.preview + '|' + row.unread + '|' + row.isCurrent;
    }).join('\n');
  }
  function checkSidebar() {
    sidebarTimer = null;
    var next = sidebarFingerprint();
    if (next === sidebarSignature) return;
    sidebarSignature = next;
    sidebarRevision += 1;
    sidebarWaiters.slice().forEach(function (wake) { wake(); });
  }
  function waitForSidebarChange(since, timeoutMs) {
    if (since !== sidebarRevision) {
      return Promise.resolve({ changed: true, revision: sidebarRevision });
    }
    return new Promise(function (resolve) {
      var finished = false;
      var timer;
      function finish(changed) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        sidebarWaiters = sidebarWaiters.filter(function (item) { return item !== wake; });
        resolve({ changed: changed, revision: sidebarRevision });
      }
      function wake() { finish(true); }
      sidebarWaiters.push(wake);
      timer = setTimeout(function () { finish(false); }, Math.min(5000, Math.max(100, timeoutMs || 1000)));
    });
  }
  sidebarSignature = sidebarFingerprint();
  var sidebarObserver = new MutationObserver(function () {
    if (sidebarTimer !== null) return;
    sidebarTimer = setTimeout(checkSidebar, 60);
  });
  sidebarObserver.observe(document.body || document.documentElement,
                          { subtree: true, childList: true, characterData: true, attributes: true });

  window.__FGA = {
    __v: V, read: read, status: status, fill: fill, submit: submit,
    listConvs: listConvs, clickConv: clickConv, openConv: openConv,
    waitForSidebarChange: waitForSidebarChange, tabs: tabs, clickTab: clickTab
  };
  return 'installed:v' + V;
})()
