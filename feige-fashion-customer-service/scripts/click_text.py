# -*- coding: utf-8 -*-
"""在飞鸽页面里点击指定文本的元素（UTF-8 安全通道，两阶段）。

为什么需要它：Windows 命令行默认 GBK，直接把中文塞进 eval 会乱码。
所以中文一律走 base64 + TextDecoder，和 cdp_eval.py 同一套编码纪律。

用法（两阶段 —— 本机沙箱不允许 Python 起子进程）：
    python click_text.py --text "最近联系" --emit-payload click.txt
    agent-browser --cdp 9222 eval "$(< click.txt)" > r.json
    python click_text.py --from-raw r.json
"""
import argparse
import base64
import io
import json
import os
import sys

JS = r"""(function(){
  var WANT = "__WANT__";
  var MAXLEN = 40;
  var all = Array.from(document.querySelectorAll('*')).filter(function(e){
    return e.children.length === 0;
  });
  var target = null, how = '';
  var exact = all.filter(function(e){ return (e.textContent||'').trim() === WANT; });
  if (exact.length) { target = exact[0]; how = 'exact'; }
  if (!target) {
    var loose = all.filter(function(e){
      var s = (e.textContent||'').trim();
      return s && s.indexOf(WANT) >= 0 && s.length <= MAXLEN;
    });
    if (loose.length) { target = loose[0]; how = 'contains'; }
  }
  if (!target) {
    return JSON.stringify({ok:false, reason:'notfound', want:WANT, leaves:all.length});
  }
  var box = target.getBoundingClientRect();
  if (target.click) { target.click(); }
  // 有些节点 click() 不触发 React 合成事件，补一发原生事件
  if (document.createEvent) {
    var ev = document.createEvent('MouseEvents');
    ev.initEvent('click', true, true);
    target.dispatchEvent(ev);
  }
  return JSON.stringify({
    ok:true, how:how, tag:target.tagName,
    text:(target.textContent||'').trim().slice(0,30),
    w:Math.round(box.width), h:Math.round(box.height)
  });
})()"""


def build_payload(text: str) -> str:
    js = JS.replace("__WANT__", json.dumps(text, ensure_ascii=False)[1:-1])
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    return (
        "(function(){var s=atob('%s');var u=new Uint8Array(s.length);"
        "for(var i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
        "return eval(new TextDecoder('utf-8').decode(u));})()" % b64
    )


def parse(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, str):
            obj = json.loads(obj)
        return obj
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="要点击的元素文本")
    ap.add_argument("--emit-payload", default=None)
    ap.add_argument("--from-raw", default=None)
    args = ap.parse_args()

    if args.emit_payload:
        if not args.text:
            ap.error("--emit-payload 需要 --text")
        payload = build_payload(args.text)
        with io.open(args.emit_payload, "w", encoding="ascii") as f:
            f.write(payload)
        print("payload written: %s (%d chars)" % (args.emit_payload, len(payload)))
        return 0

    if args.from_raw:
        with io.open(args.from_raw, "r", encoding="utf-8") as f:
            obj = parse(f.read())
        if obj is None:
            print("无法解析 %s" % args.from_raw, file=sys.stderr)
            return 2
        print(json.dumps(obj, ensure_ascii=False))
        return 0 if obj.get("ok") else 3

    ap.error("需要 --emit-payload 或 --from-raw")
    return 1


if __name__ == "__main__":
    sys.exit(main())
