#!/bin/bash
# 人肉大脑的常驻接单循环。
#
# 为什么需要它：bridge_brain watch 等到一批召唤就退出（交给人/AI 窗口处理），
# 退出后若没人重新挂，期间玩家说的话就没人接——表现为「机器人又不理人了」。
# 本循环 watch→记日志→立刻再 watch，召唤一条不漏；人（AI 窗口）看日志回单即可。
#
# 用法（仓库根或任意位置）：bash mcrobot/ops/brain_watch_loop.sh [日志路径]
# 回单：python -m mcrobot.brain.bridge_brain say "@对方 回复内容"
# 看积压：python -m mcrobot.brain.bridge_brain status
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 1
LOG="${1:-$REPO_ROOT/instance/robot/brain_watch.out}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null

echo "=== loop start $(date +%H:%M:%S) ===" >>"$LOG"
while true; do
  out=$(python -m mcrobot.brain.bridge_brain watch --timeout 600 2>&1)
  if [ -n "$out" ] && [ "$out" != "[]" ]; then
    echo "=== SUMMON $(date +%H:%M:%S) ==="
    echo "$out"
  fi
  sleep 0.3
done >>"$LOG" 2>&1
