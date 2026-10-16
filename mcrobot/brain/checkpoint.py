#!/usr/bin/env python3
"""断点续跑：把一次任务的状态落盘（LangGraph 的 checkpoint 概念，零依赖版）。

为什么要有：大脑跑任务时可能中途挂掉（超时、端点抽风、手滑关窗），
重建一次上下文很贵；状态落盘后，同一个 task_id 可以从断掉的那一拍接着跑，
也能事后翻文件复盘「它到底干了什么」。

落盘位置：instance/robot/checkpoints/<task_id>.json（每次节点走完就覆盖一次）。

用法：
  python -m mcrobot.brain.checkpoint list
  python -m mcrobot.brain.checkpoint show <task_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..paths import ROBOT_DIR
from .agent_loop import AgentState

DEFAULT_DIR = ROBOT_DIR / "checkpoints"


class Checkpointer:
    def __init__(self, directory: Path | str | None = None):
        self.dir = Path(directory) if directory else DEFAULT_DIR

    def path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def save(self, s: AgentState) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(s.task_id)
        p.write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p

    def load(self, task_id: str) -> AgentState | None:
        p = self.path(task_id)
        if not p.exists():
            return None
        try:
            return AgentState.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return None

    def latest(self) -> AgentState | None:
        if not self.dir.exists():
            return None
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        try:
            return AgentState.from_dict(json.loads(files[-1].read_text(encoding="utf-8")))
        except Exception:
            return None

    def ids(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="checkpoint")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("task_id", nargs="?", default="")
    args = ap.parse_args(argv)
    ck = Checkpointer()
    if args.action == "list":
        for i in ck.ids():
            print(i)
        return 0
    if args.action == "show":
        st = ck.load(args.task_id) if args.task_id else ck.latest()
        if not st:
            print("（没有这个 checkpoint）")
            return 1
        print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
