#!/usr/bin/env python3
"""回单兜底器：给 DeepSink 请求回单；单子若已被客户端放弃/清理，直接改发游戏聊天。

为什么需要它：
人肉大脑（CodeBuddy 窗口）回单有延迟，经常超过 brain_llm 的 HTTP 超时；
客户端一放弃，服务端就把请求归档，`deepsink_brain.py reply` 便报「找不到请求」——
回复就此丢失，玩家白等。本工具把「回单」降级为「主动发言」，回复永不丢。

用法：
  python tools/reply_mc.py req_xxxx "回复内容"      # 先回单，失败则发聊天
  python tools/reply_mc.py --say-only "内容"        # 直接发聊天（不回单）
  python tools/reply_mc.py --latest "内容"          # 回最近一条 CLAIM 记录的单

退出码：0=走通（至少一种方式送达），1=都没送达。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
from ..paths import REPO
SIM = REPO / "sim"
WORKER_LOG = REPO / "instance" / "robot" / "worker.out"

from ..body import bridge as cc


def reply(rid: str, text: str) -> bool:
    """给 DeepSink 请求回单。成功 True。"""
    r = subprocess.run(
        [sys.executable, "deepsink_brain.py", "reply", rid, text],
        cwd=str(SIM), capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    ok = r.returncode == 0
    print(f"[reply_mc] 回单 {'成功' if ok else '失败'}"
          f"{'' if ok else '：' + (r.stderr or r.stdout).strip()[:120]}")
    return ok


def say(text: str) -> bool:
    """直接发到游戏聊天。"""
    try:
        r = cc.chat(text)
        ok = bool(r and r.get("ok"))
        print(f"[reply_mc] 游戏发言 {'成功' if ok else '失败'}: {r}")
        return ok
    except Exception as e:
        print(f"[reply_mc] 游戏发言异常: {e}")
        return False


def latest_id() -> str | None:
    """从 worker 日志里取最近一条被认领的请求 id。"""
    if not WORKER_LOG.exists():
        return None
    raw = WORKER_LOG.read_text(encoding="utf-8", errors="replace")
    ms = list(re.finditer(r"=== CLAIM.*?===\s*(\[.*?\])\s*(?==== CLAIM|\Z)", raw, re.S))
    for m in reversed(ms):
        try:
            d = json.loads(m.group(1))
            r = d[-1] if isinstance(d, list) else d
            if r.get("id"):
                return r["id"]
        except Exception:
            continue
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回单兜底：单子没了就改发游戏聊天")
    ap.add_argument("rid", nargs="?", help="请求 id（req_xxx）")
    ap.add_argument("text", nargs="?", help="回复内容")
    ap.add_argument("--latest", action="store_true", help="用 worker 日志里最近认领的 id")
    ap.add_argument("--say-only", action="store_true", help="跳过回单，直接发聊天")
    args = ap.parse_args(argv)

    text = args.text
    rid = args.rid
    if args.latest and not rid:
        rid = latest_id()
        print(f"[reply_mc] 取到最近 id: {rid}")
    if not text:
        print("[reply_mc] 缺回复内容", file=sys.stderr)
        return 1

    if not args.say_only and rid:
        if reply(rid, text):
            return 0
        print("[reply_mc] 单子不可用，降级为游戏发言")
    return 0 if say(text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
