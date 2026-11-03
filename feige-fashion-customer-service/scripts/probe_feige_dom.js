// 飞鸽 DOM 探测 v4：锁定消息区容器、消息方向判定、会话项、商品区、转人工入口
// 用法：python cdp_eval.py probe_feige_dom.js --emit-payload payload.txt
(function () {
  function norm(el) {
    if (!el || el.tagName === 'BODY') return null;
    var s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    var c = (el.className && typeof el.className === 'string')
      ? el.className.trim().split(/\s+/).filter(Boolean) : [];
    if (c.length) s += '.' + c.slice(0, 2).join('.');
    return s;
  }
  function path(el, depth) {
    depth = depth || 4;
    if (!el) return null;
    var parts = [], cur = el;
    for (var i = 0; i < depth && cur && cur.tagName !== 'BODY'; i++) {
      var s = norm(cur);
      if (s) parts.unshift(s);
      cur = cur.parentElement;
    }
    return parts.join('>');
  }
  function deepestByText(t) {
    var hits = Array.prototype.slice.call(document.querySelectorAll('*'))
      .filter(function (e) { return (e.textContent || '').indexOf(t) >= 0; });
    if (!hits.length) return null;
    hits.sort(function (a, b) {
      var da = 0, db = 0, x = a, y = b;
      while (x) { da++; x = x.parentElement; }
      while (y) { db++; y = y.parentElement; }
      return db - da;
    });
    return hits[0];
  }
  function ancestors(el, n) {
    var out = [], cur = el;
    for (var i = 0; i < n && cur; i++) {
      var r = cur.getBoundingClientRect();
      out.push({
        lv: i, path: path(cur, 2), children: cur.children.length,
        id: cur.id || '', qa: (cur.getAttribute && cur.getAttribute('data-qa-id')) || '',
        box: [Math.round(r.width), Math.round(r.height)],
        scrollable: cur.scrollHeight > cur.clientHeight + 20
      });
      cur = cur.parentElement;
    }
    return out;
  }

  var out = { url: location.href, ts: Date.now() };

  // 1. 消息气泡与容器
  var bubbles = Array.prototype.slice.call(
    document.querySelectorAll('[data-qa-id="qa-message-warpper"]'));
  out.bubbleCount = bubbles.length;
  out.bubbleSamples = bubbles.slice(0, 6).map(function (b) {
    return {
      text: (b.textContent || '').trim().slice(0, 60),
      cls: (b.className || '').slice(0, 160),
      parentCls: b.parentElement ? (b.parentElement.className || '').slice(0, 120) : '',
      parentQa: b.parentElement ? (b.parentElement.getAttribute('data-qa-id') || '') : '',
      childCount: b.children.length
    };
  });
  if (bubbles.length) {
    // 全部气泡的最近公共祖先 = 消息列表容器
    var anc = ancestors(bubbles[0], 8);
    out.bubbleAncestors = anc;
    var container = bubbles[0].parentElement;
    for (var k = 0; k < 6 && container; k++) {
      var all = bubbles.every(function (b) {
        return container && (container === b || container.contains(b));
      });
      if (all && container.children.length >= bubbles.length) break;
      container = container.parentElement;
    }
    out.msgListContainer = container ? {
      path: path(container, 5), children: container.children.length,
      id: container.id || '', cls: (container.className || '').slice(0, 140),
      scrollable: container.scrollHeight > container.clientHeight + 20
    } : null;
  }

  // 2. 会话列表项
  var items = Array.prototype.slice.call(
    document.querySelectorAll('[data-qa-id="qa-conversation-chat-item"]'));
  out.convItemCount = items.length;
  out.convItemSamples = items.slice(0, 4).map(function (it) {
    return {
      text: (it.textContent || '').trim().slice(0, 50),
      cls: (it.className || '').slice(0, 140),
      parentId: it.parentElement ? (it.parentElement.id || '') : '',
      parentCls: it.parentElement ? (it.parentElement.className || '').slice(0, 100) : ''
    };
  });
  if (items.length) out.convAncestors = ancestors(items[0], 6);

  // 3. 商品区
  var prod = deepestByText('暂无咨询商品') || deepestByText('咨询商品');
  out.productArea = prod ? {
    path: path(prod, 5), text: (prod.textContent || '').trim().slice(0, 60),
    ancestors: ancestors(prod, 4)
  } : null;

  // 4. 转人工 / 结束会话 入口
  ['转人工', '转接', '结束会话', '邀请评价'].forEach(function (t) {
    var el = deepestByText(t);
    out['entry_' + t] = el ? { path: path(el, 4), text: (el.textContent || '').trim().slice(0, 30) } : null;
  });

  // 5. 顶部会话信息（买家名 / 消费额）
  var top = document.querySelector('#topbar-left-info');
  out.topLeft = top ? { text: (top.textContent || '').trim().slice(0, 60) } : null;
  var sec = document.querySelector('#topbar-sec-container');
  out.topSec = sec ? { text: (sec.textContent || '').trim().slice(0, 80) } : null;

  // 6. 输入区文本域与发送按钮（确认可用）
  var ta = document.querySelector('[data-qa-id="qa-send-message-textarea"]');
  var btn = document.querySelector('[data-qa-id="qa-send-message-button"]');
  out.input = ta ? { found: true, placeholder: ta.placeholder || '', disabled: ta.disabled } : { found: false };
  out.sendBtn = btn ? { found: true, text: (btn.textContent || '').trim() } : { found: false };

  // 7. 当前会话可见消息全文（供人工核对，验证读取一致性）
  out.chatText = (function () {
    var c = document.querySelector('#workspace-chat');
    return c ? (c.innerText || '').slice(0, 2000) : '';
  })();

  return JSON.stringify(out);
})()
