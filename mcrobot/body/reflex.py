#!/usr/bin/env python3
"""聊天回执解析 + 指令意图分级。

纯函数、无副作用，便于单测。ControlMod 的 /state、/chat 回执文本走这里解析。
本服玩家聊天格式：`<Name> 消息`。其余（含 yzt/系统提示）一律当 system。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 玩家名：Minecraft 允许 3–16 位 [A-Za-z0-9_]
_PLAYER_RE = re.compile(r"^<([A-Za-z0-9_]{1,16})>\s?(.*)$", re.DOTALL)


def parse(line: str) -> tuple[str, str | None, str]:
    """把一行回执解析成 (kind, speaker, text)。

    kind ∈ {"player", "system", "empty"}。
    """
    s = (line or "").strip()
    if not s:
        return ("empty", None, "")
    m = _PLAYER_RE.match(s)
    if m:
        return ("player", m.group(1), m.group(2).strip())
    return ("system", None, s)


# ----------------------------- 意图分级 -----------------------------

# 只读 / 选区类 WE 子命令（任何人可用，不改世界）
_WE_SAFE = {
    "size", "pos1", "pos2", "hpos1", "hpos2", "sel", "help", "wand", "count",
    "distr", "desel", "toggleeditwand", "chunkinfo", "biomeinfo", "limit",
    "searchitem", "version", "world", "tool", "ascend", "descend", "unstuck",
    "jumpto", "thru", "up", "cs", "calc", "calculate",
}

# 改世界的 WE 子命令（危险：需授权）
_WE_DANGEROUS = {
    "set", "replace", "fill", "drain", "regen", "hsphere", "hcyl", "sphere",
    "cyl", "copy", "paste", "move", "stack", "rotate", "deform", "smooth",
    "naturalize", "cube", "line", "walls", "hollow", "overlay", "center",
    "outline", "forest", "flora", "pyramid", "delete", "remove", "expand",
    "contract", "shift", "inset", "outs", "spline", "generate", "gen",
    "paste", "regen", "redo", "undo", "clearclipboard", "flip", "schem",
    "load", "save", "paste", "green", "fixlava", "fixwater", "snow", "thaw",
    "removeabove", "removebelow", "replacenear", "extinguish", "butcher",
    "paste", "deltree", "fillr", "removenear", "replacenear", "snapshot",
    "restore", "brush", "mask", "gmask",
}

# 非 WE 的危险原版/插件指令（权限、管理、删除类）
_DANGEROUS_CMD_RE = re.compile(
    r"^/?(op|deop|ban|ban-ip|pardon|pardon-ip|kick|stop|whitelist|save-off|"
    r"save-all|reload|restart|lp|luckperms|multiworld|mv|gamerule|difficulty)\b",
    re.IGNORECASE,
)

_DANGEROUS_WORDS = (
    "给op", "给个op", "撤op", "撤销op", "封号", "封禁", "踢出", "踢掉",
    "删掉", "删除", "拆掉", "拆除", "炸掉", "清空", "倒下", "倒掉",
)


@dataclass(frozen=True)
class Intent:
    kind: str          # "we" | "command" | "chat"
    command: str | None
    dangerous: bool
    reason: str


def analyze(text: str) -> Intent:
    """判定一条玩家发言的意图与危险等级。"""
    t = (text or "").strip()

    # WE 命令：//xxx 或 /we xxx
    m = re.match(r"^//\s*([a-zA-Z]+)", t)
    if m:
        sub = m.group(1).lower()
        if sub in _WE_SAFE:
            return Intent("we", t, False, f"只读WE:{sub}")
        if sub in _WE_DANGEROUS:
            return Intent("we", t, True, f"改世界WE:{sub}")
        # 未知 WE 子命令：保守当危险
        return Intent("we", t, True, f"未知WE:{sub}")

    # 原版/插件指令：/xxx
    if t.startswith("/"):
        body = t[1:]
        if _DANGEROUS_CMD_RE.match("/" + body):
            return Intent("command", t, True, "管理类指令")
        return Intent("command", t, False, "普通指令")

    # 自然语言：查危险关键词
    low = t.lower()
    for w in _DANGEROUS_WORDS:
        if w in low:
            return Intent("chat", None, True, f"危险词:{w}")
    return Intent("chat", None, False, "自然语言")


def is_summon(text: str, nickname: str, prefixes) -> bool:
    """是否在叫机器人：提及名字 或 以某前缀开头。"""
    low = (text or "").lower()
    if nickname and nickname.lower() in low:
        return True
    return any(low.startswith(p.lower()) for p in prefixes)


def strip_summon(text: str, nickname: str, prefixes) -> str:
    """去掉召唤词/前缀，留下正文。"""
    t = (text or "").strip()
    low = t.lower()
    for p in prefixes:
        if low.startswith(p.lower()):
            t = t[len(p):]
            break
    else:
        if nickname:
            t = re.sub(re.escape(nickname), "", t, flags=re.IGNORECASE)
    return t.strip(" ,:，：。!！")
