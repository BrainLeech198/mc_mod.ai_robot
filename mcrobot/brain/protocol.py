"""身体 ↔ 大脑之间唯一的契约：inbox / outbox / 指针。

身体（`mcrobot.body.loop`）只往 `inbox.jsonl` 追加、只从 `outbox.jsonl` 取动作；
任何大脑（L1 API / L2 bridge / 将来的 MCP 客户端）都只用本模块读写，互不感知。

inbox  一行一条召唤：{"ts","speaker","text","intent","env"}
outbox 一行一条动作：{"ts","say"|"cmd"}（body 下一拍自动发出）
ptr    读指针，保证多个大脑不会重复消费同一条（**同时只许挂一个大脑**）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..paths import ROBOT_DIR

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
    """写一条动作到 outbox。多带一个 ts 只给控制台画时间线用，body 只认 say/cmd。"""
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), **obj}, ensure_ascii=False) + "\n")


def unread() -> list[dict]:
    """取出未读召唤并推进指针。"""
    lines = _lines()
    new = [r for r in (_parse(l) for l in lines[_ptr():]) if r]
    _save_ptr(len(lines))
    return new


def stats() -> dict:
    lines = _lines()
    ob = OUTBOX.read_text(encoding="utf-8").splitlines() if OUTBOX.exists() else []
    return {
        "inbox_lines": len(lines),
        "read_ptr": _ptr(),
        "unread": max(0, len(lines) - _ptr()),
        "outbox_pending": len(ob),
    }
