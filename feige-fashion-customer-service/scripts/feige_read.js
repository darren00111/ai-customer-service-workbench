// 飞鸽会话读取（只读，绝不点击发送、绝不写入输入框）
// 输出结构化 JSON：买家信息 / 商品卡 / 消息列表（含角色判定）/ 最新用户消息
(function () {
  function txt(el) { return el ? (el.innerText || el.textContent || '').trim() : ''; }

  // ---- 角色判定 ----
  // 飞鸽气泡没有直接的 role 属性，用多重信号判定，按可信度排序：
  //   1) 气泡内出现"xxx发送"署名 -> 非买家（客服 / 智能客服 / 商家配置）
  //   2) 气泡内出现"系统消息" -> 系统
  //   3) 水平对齐：买家消息靠左，客服消息靠右（用气泡中心 x 与容器中心比较）
  //   4) 兜底 -> unknown
  // 平台通知必须先于买家判定：像「抖音电商智能客服已开启」这类系统文案，
  // 若不判掉会被当成买家消息，甚至被当成最后一条买家话来回复。
  var SYSTEM_PAT = /用户超时未回复|当前会话已长时间未回复|会话已结束|会话已关闭|系统消息|系统自动|客服.+接入|智能客服已(开启|关闭)|已接入(人工|智能客服)|人工客服为您服务/;

  function detectRole(bubble) {
    var t = txt(bubble);
    // 顺序即优先级：系统通知最容易和买家消息混淆，必须先判掉
    if (SYSTEM_PAT.test(t)) return 'system';
    if (/抖音电商智能客服发送|智能客服发送/.test(t)) return 'ai';
    if (/商家配置发送/.test(t)) return 'merchant';
    // 商家侧看板：只有自己发出的消息才带已读/未读回执
    if (/(已读|未读)/.test(t)) return 'agent';
    if (/[^\n]{0,12}发送\s*$/.test(t)) return 'agent';
    return 'buyer';
  }

  // 从气泡文本里剥掉时间、署名、已读等噪声，取出正文
  function extractContent(bubble) {
    var t = txt(bubble);
    var lines = t.split('\n').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
    var drop = /^(\d{1,2}:\d{2}(:\d{2})?|已读|未读|系统消息|智能客服|商家配置发送|抖音电商智能客服发送|.*发送|0\/\d+)$/;
    var body = lines.filter(function (s) { return !drop.test(s); });
    return body.join(' / ').slice(0, 500);
  }
  function extractTime(bubble) {
    var t = txt(bubble);
    var m = t.match(/(\d{1,2}:\d{2}(?::\d{2})?)/);
    return m ? m[1] : '';
  }

  var out = { url: location.href, ts: Date.now(), ok: true, warnings: [] };

  // 买家 / 会话头
  var topLeft = document.querySelector('#topbar-left-info');
  var topSec = document.querySelector('#topbar-sec-container');
  out.buyerName = topLeft ? txt(topLeft).replace(/添加备注\s*$/, '').trim() : '';
  out.buyerMeta = topSec ? txt(topSec).replace(/\n/g, ' ').slice(0, 120) : '';

  // 消息区
  var bubbles = Array.prototype.slice.call(
    document.querySelectorAll('[data-qa-id="qa-message-warpper"]'));
  var container = null;
  if (bubbles.length) {
    var c = bubbles[0].parentElement;
    for (var k = 0; k < 6 && c; k++) {
      var all = bubbles.every(function (b) { return c === b || c.contains(b); });
      if (all && c.children.length >= bubbles.length) { container = c; break; }
      c = c.parentElement;
    }
  }
  var cbox = container ? container.getBoundingClientRect() : null;
  out.messageContainerFound = !!container;
  out.bubbleCount = bubbles.length;

  out.messages = bubbles.map(function (b) {
    return {
      role: detectRole(b, cbox),
      time: extractTime(b),
      content: extractContent(b),
      rawLen: txt(b).length
    };
  });

  // 最新一条买家消息（决策层的输入）
  var buyers = out.messages.filter(function (m) { return m.role === 'buyer'; });
  out.latestBuyerMessage = buyers.length ? buyers[buyers.length - 1].content : '';
  out.latestMessage = out.messages.length ? out.messages[out.messages.length - 1].content : '';

  // 会话列表（含未读判断）
  var currentName = out.buyerName;
  out.conversations = Array.prototype.slice.call(
    document.querySelectorAll('[data-qa-id="qa-conversation-chat-item"]'))
    .map(function (it) {
      var t = txt(it);
      var lines = t.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      var cls = it.className || '';
      // 选中态类名是 hash 类，会随前端发版变化，所以再用买家名兜一层
      var selected = cls.indexOf('wmvLQcpt39Hk9PSISrlN') >= 0;
      return {
        name: lines[0] || '',
        time: (t.match(/\d{1,2}:\d{2}/) || [''])[0],
        preview: lines.slice(1).join(' ').slice(0, 40),
        selected: selected || (currentName && lines[0] === currentName && selected)
      };
    }).slice(0, 30);

  // 商品区（左侧"咨询商品"，当前会话可能为空）
  var prodRoot = document.querySelector('#leftSide');
  out.productPanel = prodRoot ? txt(prodRoot).replace(/\n/g, ' | ').slice(0, 200) : '';

  // 输入框状态（只读，不写入）
  var ta = document.querySelector('[data-qa-id="qa-send-message-textarea"]');
  out.input = ta ? { present: true, value: ta.value || '', disabled: ta.disabled } : { present: false };
  var btn = document.querySelector('[data-qa-id="qa-send-message-button"]');
  out.sendButton = btn ? { present: true, text: txt(btn) } : { present: false };

  if (!container) out.warnings.push('未定位到消息容器（list_items）');
  if (!bubbles.length) out.warnings.push('未读到任何消息气泡，会话可能未打开或页面未加载完');

  return JSON.stringify(out);
})()
