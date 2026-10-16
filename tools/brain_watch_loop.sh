#!/bin/bash
# 人肉大脑的常驻接单循环。
#
# 为什么需要它：brain.py watch 等到一批召唤就退出（交给人处理），
# 退出后若没人重新挂，期间玩家说的话就没人接——表现为「机器人又不理人了」。
# 本循环 watch→记日志→立刻再 watch，召唤一条不漏；人（AI 窗口）看日志回单即可。
#
# 用法：bash tools/brain_watch_loop.sh [日志路径]
# 回单：python tools/brain.py say "@对方 回复内容"
# 看积压：python tools/brain.py status
cd "$(dirname "$0")" || exit 1
LOG="${1:-../instance/robot/brain_watch.out}"
WORKDIR="$(dirname "$LOG")"
mkdir -p "$WORKDIR" 2>/dev/null

while true; do
  out=$(python brain.py watch --timeout 600 2>&1)
  if [ -n "$out" ] && [ "$out" != "[]" ]; then
    echo "=== SUMMON $(date +%H:%M:%S) ==="
    echo "$out"
  fi
  sleep 0.3
done >>"$LOG" 2>&1
