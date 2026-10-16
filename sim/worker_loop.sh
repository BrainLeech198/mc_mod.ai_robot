#!/bin/bash
# DeepSink 推理 worker 常驻循环（人肉大脑 = 当前 CodeBuddy 窗口）。
#
# 为什么需要它：deepsink_brain.py watch 认领一批就退出（设计给外层循环重挂），
# 没有外层循环时 worker 认领一轮就下线——MC 机器人的召唤就没人接了。
#
# 防僵尸：认领到 age>60s 的旧请求（客户端早超时放弃了）直接释放认领锁，
# 让服务端 patience 清扫；否则锁被反复续期，旧请求永远删不掉。
#
# 用法：bash sim/worker_loop.sh [日志路径]
# 认领记录带 "=== CLAIM ===" 标记写进日志，供哨兵/人工发现新单。
cd "$(dirname "$0")" || exit 1
LOG="${1:-../instance/robot/worker.out}"
WORKER="claude-mc"

while true; do
  out=$(python deepsink_brain.py watch --worker "$WORKER" --timeout 300 2>&1)
  if [ "$out" != "[]" ] && [ -n "$out" ]; then
    info=$(echo "$out" | python -c "
import json, sys, time
try:
    d = json.load(sys.stdin)
    r = d[-1] if isinstance(d, list) else d
    print(r.get('id', ''), int(time.time() - float(r.get('ts', 0) or 0)))
except Exception:
    print('', 999)
" 2>/dev/null)
    rid=$(echo "$info" | cut -d' ' -f1)
    age=$(echo "$info" | cut -d' ' -f2)
    if [ -n "$rid" ] && [ "${age:-999}" -gt 60 ]; then
      # 旧请求：客户端已放弃，放锁让服务端清掉，不当僵尸接
      python deepsink_brain.py release "$rid" >/dev/null 2>&1
      continue
    fi
    echo "=== CLAIM $(date +%H:%M:%S) age=${age}s ==="
    echo "$out"
  fi
  sleep 0.3
done >>"$LOG" 2>&1
