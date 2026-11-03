#!/usr/bin/env bash
# 飞鸽自动回复：跑完整链路（读会话 -> 判定 -> 发送）。
#
# 为什么用 shell 串、而不是全塞进 Python：
#   本机沙箱下 Python 起子进程调 agent-browser 会挂死（实测 3 分钟无返回，
#   连 --version 都不回）。所以浏览器动作必须由 shell 执行。
#   详见 scripts/reply_once.py 文件头。
#
# 用法：
#   bash scripts/auto_reply.sh                    # 只读 + 判定，不发送
#   bash scripts/auto_reply.sh --send             # 真实发送
#   bash scripts/auto_reply.sh --send --force     # 忽略「无新消息」，强制处理当前最新买家消息
#   bash scripts/auto_reply.sh --send-handoff     # 连转人工占位回复也发（默认不发，见下）
#
# 默认不发送、转人工占位回复默认不发，两个默认值都是刻意的：
#   - 不发送：这套东西会真的替商家对外说话，必须显式开口。
#   - 不发 handoff：占位话术写着「已为您转接」，但转人工入口尚未标定，
#     我们其实没有真的转接。承诺做不到比不承诺更糟。
set -uo pipefail

# 注意必须用 pwd -W：Git Bash 的 pwd 给的是 /c/... 这种 MSYS 路径，
# 原生 Windows 版 Python 不认，会报 FileNotFoundError。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && { pwd -W 2>/dev/null || pwd; })"
cd "$ROOT"

PY="${PY:-C:/Users/1154/.workbuddy/binaries/python/envs/default/Scripts/python.exe}"
AB="${AB:-C:/Users/1154/.workbuddy/binaries/node/versions/22.22.2-3/node_modules/agent-browser/bin/agent-browser-win32-x64.exe}"
CDP="${FEIGE_CDP:-9222}"
TMP="$ROOT/_tmp"
mkdir -p "$TMP"

SEND=0
FORCE=""
SEND_HANDOFF=""
for a in "$@"; do
  case "$a" in
    --send)         SEND=1 ;;
    --force)        FORCE="--force" ;;
    --send-handoff) SEND_HANDOFF="--send-handoff" ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

echo "== 1/4 读取会话 =="
"$PY" scripts/reply_once.py --emit "$TMP/p_load.txt" >/dev/null || exit 2
"$AB" --cdp "$CDP" --max-output 200000 eval "$(cat "$TMP/p_load.txt")" > "$TMP/raw.json" 2>"$TMP/err.txt"
if [ ! -s "$TMP/raw.json" ]; then
  echo "读取失败：$(cat "$TMP/err.txt")" >&2
  exit 2
fi

echo "== 2/4 判定 =="
"$PY" scripts/reply_once.py --from-raw "$TMP/raw.json" $FORCE $SEND_HANDOFF --outbox "$TMP/outbox.txt"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "无可发内容，结束（exit=$RC）"
  exit 0
fi

if [ "$SEND" -ne 1 ]; then
  echo "== 只读模式，未发送。加 --send 才会真的发出去 =="
  exit 0
fi

echo "== 3/4 写入输入框 =="
"$PY" scripts/send_reply.py --stage fill --text-file "$TMP/outbox.txt" --emit "$TMP/p_fill.txt" >/dev/null || exit 2
"$AB" --cdp "$CDP" --max-output 200000 eval "$(cat "$TMP/p_fill.txt")" > "$TMP/raw_fill.json" 2>&1
"$PY" scripts/send_reply.py --stage fill --from-raw "$TMP/raw_fill.json" --text-file "$TMP/outbox.txt" || exit 4

echo "== 4/4 提交 =="
"$PY" scripts/send_reply.py --stage submit --emit "$TMP/p_sub.txt" >/dev/null || exit 2
"$AB" --cdp "$CDP" --max-output 200000 eval "$(cat "$TMP/p_sub.txt")" > "$TMP/raw_sub.json" 2>&1
"$PY" scripts/send_reply.py --stage submit --from-raw "$TMP/raw_sub.json" --text-file "$TMP/outbox.txt" --allow-send || exit 5
echo "完成。"
