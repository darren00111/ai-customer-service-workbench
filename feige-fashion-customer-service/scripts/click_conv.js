(function(){
  var IDX = __IDX__;
  var items = document.querySelectorAll('[data-qa-id="qa-conversation-chat-item"]');
  if (!items.length) {
    return JSON.stringify({ok: false, reason: 'no conversation items', count: 0});
  }
  if (IDX >= items.length) {
    return JSON.stringify({ok: false, reason: 'index out of range',
                           count: items.length, want: IDX});
  }
  var t = items[IDX];
  var before = (t.innerText || '').replace(/\s+/g, ' ').slice(0, 60);
  t.click();
  // React 合成事件有时不吃 .click()，补一发原生事件
  if (document.createEvent) {
    var ev = document.createEvent('MouseEvents');
    ev.initEvent('click', true, true);
    t.dispatchEvent(ev);
  }
  return JSON.stringify({ok: true, idx: IDX, text: before, count: items.length});
})()
