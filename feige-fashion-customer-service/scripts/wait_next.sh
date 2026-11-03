#!/usr/bin/env bash
# 等一条**新的**买家消息，命中后打印完整判定 JSON 并退出。
#
# 为什么需要它：
#   这套链路是「读一次 -> 判一次 -> 发一次」，不会反向唤醒调用方。
#   顾客发消息不会自动触发任何东西。要让「发了就有回应」成立，
#   要么挂一个循环等（本脚本），要么上定时任务。
#
# 为什么不用 cat / sleep / head：
#   实测这台机器上 Git Bash 的 coreutils 会间歇性失效（dirname、cat、
#   head 全 command not found），一旦失效脚本就静默卡死——比报错更糟，
#   因为你会以为「没消息」，其实是「没在听」。
#   所以外部命令一律换成 Python，Python 路径是绝对的，不依赖 PATH。
#
# 用法：
#   bash scripts/wait_next.sh            # 默认 200 轮 x 3s = 10 分钟
#   bash scripts/wait_next.sh 400        # 自定义轮数
#
# 退出码：0 有新消息（JSON 打到 stdout）/ 2 载荷失败 / 3 等待超时
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && { pwd -W 2>/dev/null || pwd; })"
cd "$ROOT"

PY="${PY:-C:/Users/1154/.workbuddy/binaries/python/envs/default/Scripts/python.exe}"
AB="${AB:-C:/Users/1154/.workbuddy/binaries/node/versions/22.22.2-3/node_modules/agent-browser/bin/agent-browser-win32-x64.exe}"
CDP="${FEIGE_CDP:-9222}"
TMP="$ROOT/_tmp"

ROUNDS="${1:-200}"
INTERVAL="${WAIT_INTERVAL:-3}"

py() { "$PY" -c "$1" ${2+"$2"} ${3+"$3"}; }

py "import os,sys;os.makedirs(sys.argv[1],exist_ok=True)" "$TMP" || exit 2

# 载荷只生成一次：它就是一坨 JS，内容不变，没必要每轮重算
"$PY" scripts/reply_once.py --emit "$TMP/p_wait.txt" >/dev/null || exit 2
PAYLOAD="$(py "import io,sys;sys.stdout.write(io.open(sys.argv[1],encoding='ascii').read())" "$TMP/p_wait.txt")"
if [ -z "$PAYLOAD" ]; then
  echo "载荷为空，无法监听" >&2
  exit 2
fi

i=0
while [ "$i" -lt "$ROUNDS" ]; do
  i=$((i + 1))
  "$AB" --cdp "$CDP" --max-output 200000 eval "$PAYLOAD" > "$TMP/raw_wait.json" 2>"$TMP/err_wait.txt"

  if [ ! -s "$TMP/raw_wait.json" ]; then
    echo "[$i] 读取为空：" "$(py "import io;print(io.open(sys.argv[1],encoding='utf-8',errors='ignore').read()[:120])" "$TMP/err_wait.txt")"
    py "import sys,time;time.sleep(float(sys.argv[1]))" "$INTERVAL"
    continue
  fi

  # --json 模式下 stdout 是纯 JSON（人读的文本走 stderr，见 reply_once.py）
  OUT="$("$PY" scripts/reply_once.py --from-raw "$TMP/raw_wait.json" --json 2>/dev/null)"

  EV="$(printf '%s' "$OUT" | py "
import sys, json
try:
    print(json.loads(sys.stdin.read()).get('event') or '')
except Exception:
    print('')")"

  if [ "$EV" = "NEW" ]; then
    echo "$OUT"
    echo "=== 命中新买家消息（第 $i 轮）==="
    exit 0
  fi

  if [ "$EV" = "NO_BUYER_MESSAGE" ]; then
    echo "[$i] 会话里还没有买家消息"
  fi
  py "import sys,time;time.sleep(float(sys.argv[1]))" "$INTERVAL"
done

echo "=== 等待超时（$ROUNDS 轮 x ${INTERVAL}s）==="
exit 3
