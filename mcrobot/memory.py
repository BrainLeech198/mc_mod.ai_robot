#!/usr/bin/env python3
"""MC AI 机器人的「长期记忆」（按 MC 版本隔离）。

为什么用文件而不是模型上下文：模型侧历史按轮裁剪会忘事；机器人要像真实玩家一样
记得住「上周服主让我建学校」「我不喜欢呆方盒」这类跨会话的事。记忆放在
version/<版本>/memory/robot_memory.jsonl —— 换 MC 版本自动换一份，不串味。

两类记录：
- event  交互事件流：谁说了什么、我回了什么、做了什么（brain_llm 自动写）。
- thought AI 便签：我自己的想法/计划/偏好（worker 人工写，或以后模型自己写）。

用法：
  python -m mcrobot.memory add "oahnuy198 夸我建造有进步"
  python -m mcrobot.memory note "打算把学校建成三层，用模组方块"
  python -m mcrobot.memory recent [n]
  python -m mcrobot.memory fmt                # 打印可注入提示词的记忆块（调试用）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import os

from .paths import REPO
# 默认按 MC 版本隔离；激活了客户端预设时由 MCROBOT_MEMORY_DIR 指到该客户端的
# <config_dir>/memory/，于是记忆跟着客户端走，换客户端不串味。
MEM_DIR = Path(os.environ.get("MCROBOT_MEMORY_DIR")
               or (REPO / "version" / "1.20.1-forge" / "memory"))
MEM_PATH = MEM_DIR / "robot_memory.jsonl"

KINDS = ("event", "thought")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _read() -> list[dict]:
    if not MEM_PATH.exists():
        return []
    out = []
    for ln in MEM_PATH.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def add(kind: str, text: str, by: str = "") -> dict:
    """追加一条记忆。kind: event（交互事件）| thought（AI 便签）。"""
    if kind not in KINDS:
        raise ValueError(f"kind 必须是 {KINDS}")
    rec = {"ts": _now(), "kind": kind, "text": text}
    if by:
        rec["by"] = by
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    with MEM_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def recent(n: int = 8, kind: str | None = None) -> list[dict]:
    recs = _read()
    if kind:
        recs = [r for r in recs if r.get("kind") == kind]
    return recs[-max(0, n):]


def fmt_for_prompt(events: int = 8, notes: int = 3) -> str:
    """拼一块注入 system prompt 的记忆文本；没有记忆返回空串。"""
    blocks: list[str] = []
    ev = recent(events, "event")
    if ev:
        lines = [f"- {r['text']}" for r in ev]
        blocks.append("【最近发生的事】\n" + "\n".join(lines))
    th = recent(notes, "thought")
    if th:
        lines = [f"- {r['text']}" for r in th]
        blocks.append("【我现在的想法/计划】\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="memory")
    sub = ap.add_subparsers(dest="action", required=True)
    a = sub.add_parser("add", help="记一条交互事件"); a.add_argument("text")
    a.add_argument("--by", default="")
    n = sub.add_parser("note", help="记一条 AI 便签"); n.add_argument("text")
    r = sub.add_parser("recent"); r.add_argument("n", type=int, nargs="?", default=8)
    r.add_argument("--kind", default="")
    sub.add_parser("fmt")
    args = ap.parse_args(argv)

    if args.action == "add":
        rec = add("event", args.text, by=args.by)
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    if args.action == "note":
        rec = add("thought", args.text)
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    if args.action == "recent":
        for r_ in recent(args.n, args.kind or None):
            print(f"[{r_['ts'][:19]}] ({r_['kind']}) {r_['text']}")
        return 0
    if args.action == "fmt":
        print(fmt_for_prompt() or "（还没有记忆）")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
