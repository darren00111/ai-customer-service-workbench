# -*- coding: utf-8 -*-
"""为 agent-browser 的 CDP 通道准备 JS 执行载荷（payload）。

两种用法：
  1) 只生成载荷（推荐，避免 Python 起子进程被沙箱拦）：
        python cdp_eval.py probe.js --emit-payload payload.txt
     然后：
        AB=".../agent-browser-win32-x64.exe"
        P=$(< payload.txt)
        "$AB" --cdp 9222 --max-output 200000 eval "$P"

  2) 直接执行（Python 调用 agent-browser）：
        python cdp_eval.py probe.js --out result.txt

为什么要绕这一层：
    1) agent-browser 的 eval 只接受命令行字符串，Windows 命令行对引号/换行极不友好；
    2) 直接传 UTF-8 中文会因 GBK 代码页变成乱码（与微信剪贴板同一个坑）；
    3) 所以：JS 文件 -> base64 -> atob -> UTF-8 解码 -> eval，全程 ASCII，零编码损耗。
    4) 关键：不能 eval(atob(b64))。atob 产出 Latin-1（一字节一个 code unit），
       中文会被拆成乱码，导致所有按中文文本定位的元素全部落空。
"""
import argparse
import base64
import io
import os
import subprocess
import sys

DEFAULT_AGENT_BROWSER = (
    r"C:\Users\1154\.workbuddy\binaries\node\versions\22.22.2-3"
    r"\node_modules\agent-browser\bin\agent-browser-win32-x64.exe"
)

PAYLOAD_TMPL = (
    "(function(){var s=atob('%s');var u=new Uint8Array(s.length);"
    "for(var i=0;i<s.length;i++)u[i]=s.charCodeAt(i);"
    "return eval(new TextDecoder('utf-8').decode(u));})()"
)


def build_payload(js_path):
    with io.open(js_path, "r", encoding="utf-8") as f:
        js = f.read()
    b64 = base64.b64encode(js.encode("utf-8")).decode("ascii")
    return PAYLOAD_TMPL % b64


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("js_file")
    ap.add_argument("--cdp", default=os.environ.get("FEIGE_CDP", "9222"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--exe", default=None)
    ap.add_argument("--emit-payload", default=None,
                    help="只把可执行的 ASCII 载荷写入文件，不调用 agent-browser")
    args = ap.parse_args()

    payload = build_payload(args.js_file)

    if args.emit_payload:
        with io.open(args.emit_payload, "w", encoding="ascii") as f:
            f.write(payload)
        print("payload written: %s (%d chars)" % (args.emit_payload, len(payload)))
        return 0

    exe = args.exe or os.environ.get("AGENT_BROWSER_EXE") or DEFAULT_AGENT_BROWSER
    cmd = [exe]
    if args.cdp:
        cmd += ["--cdp", str(args.cdp)]
    cmd += ["--max-output", "200000", "eval", payload]
    proc = subprocess.run(cmd, capture_output=True)
    text = proc.stdout.decode("utf-8", errors="replace") or \
        proc.stderr.decode("utf-8", errors="replace")
    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print("written: %s (%d chars)" % (args.out, len(text)))
    else:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
