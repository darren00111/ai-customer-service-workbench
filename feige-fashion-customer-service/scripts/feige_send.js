(function () {
  /* 飞鸽回复写入 / 提交（Stage 3）。
     占位符由 send_reply.py 注入：
       ACTION      fill | submit | status
       TEXT_B64    待发送文本的 UTF-8 base64

     注意：本文件里除注入点外，不要出现那两个占位符的字面量。
     replace() 是全局替换，会连带改掉注释和判断里的同名字符串；
     一旦出现在比较表达式中就会退化成自比较（恒真），静默吃掉全部文本。

     设计原则（每一条都对应一次真实事故）：
       1) 分两步。先 fill 再 submit，Python 侧在两步之间校验 React 状态，
          避免「写着空串却点了发送」——那是发空消息给人。
       2) 用 HTMLTextAreaElement.prototype 的原生 value setter 写值，
          再派发 input 事件。直接 el.value = x 对 React 受控组件无效：
          值会显示但组件的 state 还是空，点发送发出的是空的。
       3) 用输入框右下角的「N/800」字数计数作为 React state 是否同步的
          指示灯。计数不跟随 value，就说明 state 没同步，中止发送。
       4) submit 前必须确认 value 非空。
  */
  var ACTION = '__ACTION__';
  var B64 = '__TEXT_B64__';

  function dec(b64) {
    /* 此处只判空。不要跟占位符字面量比较——全局替换后会变成自比较恒真。 */
    if (!b64) return '';
    var s = atob(b64);
    var u = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
    return new TextDecoder('utf-8').decode(u);
  }

  var TA_SEL = '[data-qa-id="qa-send-message-textarea"]';
  var BTN_SEL = '[data-qa-id="qa-send-message-button"]';

  function ta() { return document.querySelector(TA_SEL); }
  function btn() { return document.querySelector(BTN_SEL); }

  /* 「N/800」计数：React 是否吃到了我们的输入 */
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
      buttonDisabled: b ? (!!b.disabled || b.getAttribute('aria-disabled') === 'true' || /disabled/i.test(String(b.className))) : null,
      placeholder: t ? t.getAttribute('placeholder') : null
    };
  }

  function setNativeValue(el, val) {
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value') ||
               Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
    if (desc && desc.set) {
      desc.set.call(el, val);
      return true;
    }
    el.value = val;
    return false;
  }

  var result = { action: ACTION, ok: false };

  if (ACTION === 'status') {
    result.before = snap();
    result.ok = true;
    return JSON.stringify(result);
  }

  if (ACTION === 'fill') {
    var t = ta();
    if (!t) { result.reason = 'no_textarea'; return JSON.stringify(result); }
    var text = dec(B64);
    result.requested = text;
    result.before = snap();

    t.focus();
    // 先清空：残留文本会导致拼接出脏内容
    setNativeValue(t, '');
    t.dispatchEvent(new Event('input', { bubbles: true }));
    // 再写入目标文本
    var usedNativeSetter = setNativeValue(t, text);
    t.dispatchEvent(new Event('input', { bubbles: true }));
    t.dispatchEvent(new Event('change', { bubbles: true }));

    result.usedNativeSetter = usedNativeSetter;
    result.after = snap();
    // React state 是否跟上：value 必须完全等于目标，计数必须等于长度
    result.valueMatches = t.value === text;
    result.counterSynced = (result.after.counter === text.length);
    result.ok = result.valueMatches && result.counterSynced;
    if (!result.ok) {
      result.reason = !result.valueMatches ? 'value_mismatch' : 'react_state_not_synced';
    }
    return JSON.stringify(result);
  }

  if (ACTION === 'submit') {
    var t2 = ta(), b2 = btn();
    if (!t2) { result.reason = 'no_textarea'; return JSON.stringify(result); }
    if (!b2) { result.reason = 'no_send_button'; return JSON.stringify(result); }
    result.before = snap();
    if (!t2.value || !t2.value.trim()) {
      result.reason = 'empty_input_refuse_to_send';
      return JSON.stringify(result);
    }
    result.intended = t2.value;

    // 合成完整指针事件序列，比裸 click() 更能兼容自研组件
    var r = b2.getBoundingClientRect();
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var opts = { bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window };
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach(function (ev) {
      var E = (ev.indexOf('pointer') === 0 && window.PointerEvent) ? PointerEvent : MouseEvent;
      b2.dispatchEvent(new E(ev, opts));
    });
    b2.click();

    result.afterClick = snap();
    // 发送成功的强信号：输入框被清空
    result.cleared = (t2.value === '');
    result.ok = result.cleared;
    if (!result.ok) result.reason = 'input_not_cleared_after_click';
    return JSON.stringify(result);
  }

  result.reason = 'unknown_action';
  return JSON.stringify(result);
})()
