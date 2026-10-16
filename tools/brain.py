#!/usr/bin/env python3
"""在线大脑（AI）与机器人之间的桥。

机器人把「需要动脑的发言」写进 inbox.jsonl；本工具负责把新消息送到 AI、把 AI 的
回复/指令写回 outbox.jsonl（常驻循环 robots.py 下一拍自动发出）。

用法：
  python tools/brain.py watch [--timeout 600]   # 阻塞等待新消息；有则打印并退出(0)，超时退出(2)
  python tools/brain.py read                    # 非阻塞：打印未读消息并推进指针
  python tools/brain.py say "回复文本"           # 写一条聊天回复到 outbox
  python tools/brain.py cmd "/指令"             # 写一条指令到 outbox
  python tools/brain.py status                  # 打印指针/积压情况

AI 工作流：起 watch（后台）→ 被唤醒 → 读消息 → 用 say/cmd 回写 → 再起 watch。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROBOT_DIR = REPO / "instance" / "robot"
INBOX = ROBOT_DIR / "inbox.jsonl"
OUTBOX = ROBOT_DIR / "outbox.jsonl"
PTR = ROBOT_DIR / "inbox.ptr"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _lines() -> list[str]:
    if not INBOX.exists():
        return []
    return INBOX.read_text(encoding="utf-8").splitlines()


def _ptr() -> int:
    if PTR.exists():
        try:
            return int(PTR.read_text(encoding="utf-8").strip() or 0)
        except Exception:
            return 0
    return 0


def _save_ptr(n: int):
    PTR.parent.mkdir(parents=True, exist_ok=True)
    PTR.write_text(str(n), encoding="utf-8")


def _parse(line: str) -> dict | None:
    try:
        return json.loads(line)
    except Exception:
        return None


def emit(obj: dict):
    """写一条动作到 outbox。多带一个 ts 只给控制台画时间线用，robot.py 只认 say/cmd。"""
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), **obj}, ensure_ascii=False) + "\n")


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
    lines = _lines()
    ob = OUTBOX.read_text(encoding="utf-8").splitlines() if OUTBOX.exists() else []
    info = {
        "inbox_lines": len(lines),
        "read_ptr": _ptr(),
        "unread": max(0, len(lines) - _ptr()),
        "outbox_pending": len(ob),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
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
