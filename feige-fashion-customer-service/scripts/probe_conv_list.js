(function(){
  var sels = [
    '[data-qa-id="qa-conversation-chat-item"]',
    '.conversationItem', '.chat-item', '[class*="conversation"] [class*="item"]'
  ];
  var items = null, used = '';
  for (var k = 0; k < sels.length; k++) {
    var n = document.querySelectorAll(sels[k]);
    if (n.length) { items = n; used = sels[k]; break; }
  }
  if (!items) return JSON.stringify({ok: false, reason: 'no conversation items found'});
  var out = [];
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var cls = (typeof it.className === 'string') ? it.className : '';
    out.push({
      i: i,
      text: (it.innerText || '').replace(/\s+/g, ' ').slice(0, 100),
      cls: cls.slice(0, 180),
      active: /active|selected|current|checked/i.test(cls)
    });
  }
  return JSON.stringify({ok: true, selector: used, count: out.length, list: out});
})()
