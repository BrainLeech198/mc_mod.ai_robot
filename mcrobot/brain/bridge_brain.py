#!/usr/bin/env python3
"""L2 大脑桥：人肉 / Claude Code 在线接单的命令行入口。

协议实现在 `mcrobot.brain.protocol`，本文件只做 CLI。

用法：
  python -m mcrobot.brain.bridge_brain watch [--timeout 600]  # 阻塞等新消息；有则打印并退出(0)，超时退出(2)
  python -m mcrobot.brain.bridge_brain read                   # 非阻塞：打印未读消息并推进指针
  python -m mcrobot.brain.bridge_brain say "回复文本"          # 写一条聊天回复到 outbox
  python -m mcrobot.brain.bridge_brain cmd "/指令"            # 写一条指令到 outbox
  python -m mcrobot.brain.bridge_brain status                 # 打印指针/积压情况

AI 工作流：起 watch（后台）→ 被唤醒 → 读消息 → 用 say/cmd 回写 → 再起 watch。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .protocol import _lines, _parse, _ptr, _save_ptr, emit, stats


def cmd_watch(timeout: float, from_start: bool) -> int:
    start = 0 if from_start else _ptr()
    deadline = time.monotonic() + timeout
    while True:
        lines = _lines()
        if len(lines) > start:
            new = [r for r in (_parse(l) for l in lines[start:]) if r]
            _save_ptr(len(lines))
            print(json.dumps(new, ensure_ascii=False, indent=2))
            return 0
        if time.monotonic() >= deadline:
            return 2  # 无新消息
        time.sleep(0.5)


def cmd_read() -> int:
    lines = _lines()
    start = _ptr()
    new = [r for r in (_parse(l) for l in lines[start:]) if r]
    _save_ptr(len(lines))
    print(json.dumps(new, ensure_ascii=False, indent=2))
    return 0


def cmd_say(text: str) -> int:
    emit({"say": text})
    print(f"queued say: {text}")
    return 0


def cmd_cmd(command: str) -> int:
    emit({"cmd": command})
    print(f"queued cmd: {command}")
    return 0


def cmd_status() -> int:
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="brain")
    sub = ap.add_subparsers(dest="action", required=True)
    w = sub.add_parser("watch"); w.add_argument("--timeout", type=float, default=600.0)
    w.add_argument("--from-start", action="store_true")
    sub.add_parser("read")
    sub.add_parser("status")
    s = sub.add_parser("say"); s.add_argument("text")
    c = sub.add_parser("cmd"); c.add_argument("command")
    args = ap.parse_args(argv)
    if args.action == "watch":
        return cmd_watch(args.timeout, args.from_start)
    if args.action == "read":
        return cmd_read()
    if args.action == "status":
        return cmd_status()
    if args.action == "say":
        return cmd_say(args.text)
    if args.action == "cmd":
        return cmd_cmd(args.command)
    return 1


if __name__ == "__main__":
    sys.exit(main())
