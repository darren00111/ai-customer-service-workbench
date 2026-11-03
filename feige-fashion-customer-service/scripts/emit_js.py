# -*- coding: utf-8 -*-
"""把 JS 文件包装成 CDP eval 载荷，支持占位符替换 + UTF-8 安全编码。

## 为什么不能直接 eval(atob(...))

atob 产出 Latin-1 字符串，中文会变乱码。必须走：
    base64 -> Uint8Array -> TextDecoder('utf-8') -> eval

## 占位符替换

JS 里的 `__KEY__` 会被 --var KEY=VALUE 的值原样替换（不加引号）。
不加引号是刻意的：有时要注入数字（__IDX__），有时要注入 JSON。

## 用法

    python emit_js.py scripts/click_conv.js --var IDX=0 --out _tmp/p.txt

## 与 cdp_eval.py 的区别

cdp_eval.py 只做编码（不含占位符替换）；本脚本 = 替换 + 编码。
需要注入变量时用本脚本，纯探针用 cdp_eval.py。
"""
import argparse
import base64
import io
import re
import sys


def build(js: str) -> str:
    b = base64.b64encode(js.encode("utf-8")).decode("ascii")
    return (
        "(function(){var s=atob('%s');var u=new Uint8Array(s.length);"
        "for(var i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
        "return eval(new TextDecoder('utf-8').decode(u));})()" % b
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 CDP eval 载荷（占位符替换 + base64）")
    ap.add_argument("js", help="JS 模板文件路径")
    ap.add_argument("--var", action="append", default=[],
                    help="占位符赋值，形如 KEY=VALUE，可重复")
    ap.add_argument("--out", default=None, help="载荷输出路径；不给则打到 stdout")
    args = ap.parse_args()

    with io.open(args.js, "r", encoding="utf-8") as f:
        js = f.read()

    missing = []
    for pair in args.var:
        if "=" not in pair:
            print("--var 需要 KEY=VALUE 形式：%r" % pair, file=sys.stderr)
            return 2
        k, v = pair.split("=", 1)
        token = "__%s__" % k
        if token not in js:
            missing.append(token)
        js = js.replace(token, v)

    if missing:
        print("JS 里找不到占位符：%s" % ", ".join(missing), file=sys.stderr)
        return 2

    left = re.findall(r"__[A-Z0-9_]+__", js)
    if left:
        print("仍有未替换的占位符：%s" % ", ".join(sorted(set(left))), file=sys.stderr)
        return 2

    payload = build(js)
    if args.out:
        with io.open(args.out, "w", encoding="ascii") as f:
            f.write(payload)
        print("payload written: %s (%d chars)" % (args.out, len(payload)))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
