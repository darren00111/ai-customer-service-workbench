(function () {
  function info(el) {
    if (!el) return null;
    var cls = el.className;
    if (typeof cls !== 'string') cls = (el.getAttribute && el.getAttribute('class')) || '';
    return {
      tag: el.tagName,
      id: el.id || '',
      cls: cls.slice(0, 200),
      contentEditable: el.isContentEditable,
      ceAttr: el.getAttribute('contenteditable'),
      role: el.getAttribute('role') || '',
      placeholder: el.getAttribute('placeholder') || el.getAttribute('data-placeholder') || '',
      disabled: !!el.disabled,
      value: (typeof el.value === 'string') ? el.value.slice(0, 120) : null,
      innerText: (el.innerText || '').slice(0, 120),
      textLen: (el.innerText || el.textContent || '').length,
      rect: (function () { var r = el.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; })()
    };
  }

  var qa = document.querySelector('[data-qa-id="qa-send-message-textarea"]');
  var inp = qa || document.querySelector('textarea, [contenteditable="true"]');

  // 输入框附近的祖先，看看受控结构
  var chain = [];
  var cur = inp;
  for (var i = 0; cur && i < 5; i++) {
    chain.push(info(cur));
    cur = cur.parentElement;
  }

  // 发送按钮
  var btn = document.querySelector('[data-qa-id="qa-send-message-button"]');
  var btns = [];
  var all = document.querySelectorAll('[data-qa-id]');
  for (var j = 0; j < all.length; j++) {
    var t = (all[j].innerText || '').replace(/\s+/g, '');
    if (t === '发送' || t === '发 送') btns.push({ qa: all[j].getAttribute('data-qa-id'), txt: t, cls: String(all[j].className).slice(0, 120) });
  }

  return JSON.stringify({
    ok: true,
    qaFound: !!qa,
    input: info(inp),
    chain: chain,
    sendButton: info(btn),
    sendCandidates: btns,
    editorCount: document.querySelectorAll('[contenteditable="true"]').length,
    textareaCount: document.querySelectorAll('textarea').length
  }, null, 1);
})()
