(function(){
  var cands = document.querySelectorAll(
    '[role="tab"], [class*="tab" i] [class*="item"], [class*="tabs"] > *, [class*="Tab"] > *'
  );
  var seen = {}, out = [];
  cands.forEach(function(el){
    var t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 20) return;
    if (seen[t]) return;
    seen[t] = 1;
    var cls = (typeof el.className === 'string') ? el.className : '';
    out.push({ text: t, cls: cls.slice(0, 120), selected: /active|selected|current/i.test(cls) });
  });
  return JSON.stringify({ count: out.length, tabs: out.slice(0, 30) });
})()
