#!/usr/bin/env python3
"""知识层：把「这一版世界里摸出来的事实」记成可查的表。

和 memory.py 的分工（两件事别混）：
- memory.py    流水账：谁说了什么、我回了什么、我临时想什么 → 按时间倒序注入。
- knowledge.py 查表：稳定的、有出处的事实（方块真名、指令坑、坐标、玩家偏好）
               → 按当前话题相关性检索后注入，检索不到就不注入。

用户的原话（2026-10-17）：「AI 在本版本文件发现的内容，可以写入文档，这样下次
玩家有什么需求就可以直接查表参考了。然后让 AI 模拟有玩家一样的记忆上下文。」

存储（都跟着版本走，换版本不串味）：
  version/<版本>/memory/facts.jsonl   一行一条事实（机器读）
  version/<版本>/knowledge.md         人看的表（手写区 + 自动汇总区）

四条红线：
1. **必须写出处**：只有工具回执 / 玩家原话 / 手册小节能当出处，模型猜的不记。
2. **自动区不手改**：consolidate 只替换 `AUTO:FACTS` 标记之间的内容，手写区永不动。
3. **检索不到就不注入**：宁可不提，也别把无关事实塞进上下文，浪费 token 还误导。
4. **冲突不自动删**：同一话题下矛盾的两条都留着，靠出处和时间自己判断。

用法：
  python -m mcrobot.knowledge remember "方块" "CFM 没有窗帘" --source "方块目录.md" --tags 家具
  python -m mcrobot.knowledge search 家具
  python -m mcrobot.knowledge list
  python -m mcrobot.knowledge forget 3f2a1b9c
  python -m mcrobot.knowledge consolidate     # facts.jsonl → knowledge.md 自动区
  python -m mcrobot.knowledge fmt "怎么换皮肤"  # 打印可注入提示词的块（调试用）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .textmatch import tokens

AUTO_BEGIN = "<!-- AUTO:FACTS:BEGIN（由 facts.jsonl 生成，手改会被覆盖） -->"
AUTO_END = "<!-- AUTO:FACTS:END -->"

DEFAULT_DOC = """# 机器人知识库（本版本内查表）

> 这份文档是 **AI 自己用的表**，不是给玩家看的说明书（那个是 `机器人手册.md`）。
> 上半区人工维护，下半区由 `memory/facts.jsonl` 自动汇总：
> `python -m mcrobot.knowledge consolidate`
> 红线：自动区每次整体重写，别在里面手改；要加东西用 `remember` 或写进手写区。

## 手写区（consolidate 不会动这里）

- （暂无）

## 自动汇总
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _vdir(version: str | None = None) -> Path:
    """版本目录。MCROBOT_KNOWLEDGE_DIR 可临时指到别处（单测用）。"""
    env = os.environ.get("MCROBOT_KNOWLEDGE_DIR")
    if env:
        return Path(env)
    from . import identity
    return identity.version_dir(version)


def facts_path(version: str | None = None) -> Path:
    return _vdir(version) / "memory" / "facts.jsonl"


def doc_path(version: str | None = None) -> Path:
    return _vdir(version) / "knowledge.md"


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


# ------------------------------ 存取 ------------------------------


def load(version: str | None = None) -> list[dict]:
    p = facts_path(version)
    if not p.exists():
        return []
    out: list[dict] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not (d.get("topic") and d.get("text")):
            continue
        d.setdefault("id", uuid.uuid4().hex[:8])
        d.setdefault("ts", "")
        d.setdefault("tags", [])
        d.setdefault("source", "")
        d.setdefault("hits", 0)
        out.append(d)
    return out


def _write_all(facts: list[dict], version: str | None = None):
    p = facts_path(version)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for d in facts:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


def remember(topic: str, text: str, *, tags=(), source: str = "",
             version: str | None = None) -> tuple[dict, bool]:
    """记一条事实。返回 (记录, 是否新建)。

    去重键 = 话题 + 正文（忽略大小写与空白）：同一件事说第二遍只加 hits，
    不会把表撑爆。出处必填——这是防止模型把猜测当知识存进来的唯一闸门。
    """
    topic = " ".join((topic or "").split())
    text = " ".join((text or "").split())
    source = " ".join((source or "").split())
    if not topic or not text:
        raise ValueError("topic 和 text 都要给")
    if not source:
        raise ValueError("必须写出处 source（工具回执 / 玩家原话 / 手册小节）")
    if len(text) > 500:
        raise ValueError("一条事实别超过 500 字，拆成几条更好查")
    facts = load(version)
    for f in facts:
        if (f["topic"].lower() == topic.lower()
                and _normalize(f["text"]) == _normalize(text)):
            f["hits"] = int(f.get("hits") or 0) + 1
            f["last"] = _now()
            _write_all(facts, version)
            return f, False
    rec = {"id": uuid.uuid4().hex[:8], "ts": _now(), "last": _now(),
           "topic": topic, "text": text,
           "tags": [str(t).strip() for t in (tags or ()) if str(t).strip()],
           "source": source, "hits": 0}
    facts.append(rec)
    _write_all(facts, version)
    return rec, True


def forget(fact_id: str, version: str | None = None) -> bool:
    facts = load(version)
    left = [f for f in facts if f.get("id") != fact_id]
    if len(left) == len(facts):
        return False
    _write_all(left, version)
    return True


# ------------------------------ 检索 ------------------------------


def _score(f: dict, toks: list[str]) -> int:
    if not toks:
        return 0
    topic = f["topic"].lower()
    tags = " ".join(str(t) for t in f.get("tags") or []).lower()
    text = f["text"].lower()
    n = 0
    for t in toks:
        if t in tags:
            n += 4
        if t in topic:
            n += 3
        if t in text:
            n += 2
    return n * 10 + min(int(f.get("hits") or 0), 3)


def search(query: str, top_k: int = 3, version: str | None = None) -> list[dict]:
    """关键词检索。0 分（一个词都没对上）的不返回，免得干扰决策。"""
    toks = tokens(query)
    if not toks:
        return []
    scored = [(_score(f, toks), f) for f in load(version)]
    hit = [(n, f) for n, f in scored if n >= 20]
    hit.sort(key=lambda x: (-x[0], x[1].get("ts") or ""), reverse=False)
    return [f for _, f in hit[:max(0, top_k)]]


def recent(n: int = 5, version: str | None = None) -> list[dict]:
    return load(version)[-max(0, n):]


def topics(version: str | None = None) -> list[str]:
    seen: list[str] = []
    for f in load(version):
        if f["topic"] not in seen:
            seen.append(f["topic"])
    return seen


# ------------------------------ 注入提示词 ------------------------------


def _fmt_line(f: dict) -> str:
    src = f.get("source") or "未注明"
    return f"- {f['text']}（{f['topic']} · 出处：{src}）"


def fmt_for_prompt(query: str = "", top_k: int = 3, version: str | None = None) -> str:
    """拼一块「我记得的事」；没有命中返回空串（红线 3）。"""
    hits = search(query, top_k, version) if (query or "").strip() else recent(top_k, version)
    if not hits:
        return ""
    return "【我记得的事】\n" + "\n".join(_fmt_line(f) for f in hits[:top_k])


# ------------------------------ 汇总成文档 ------------------------------


def render_block(facts: list[dict]) -> str:
    """facts → markdown。按话题分组，每条带出处与日期（方便判断新旧/真假）。"""
    lines = [AUTO_BEGIN, ""]
    if not facts:
        lines += ["（还没有记下什么。用 remember 记一条试试。）", ""]
    by_topic: dict[str, list[dict]] = {}
    for f in sorted(facts, key=lambda x: (x.get("ts") or "", x.get("id") or "")):
        by_topic.setdefault(f["topic"], []).append(f)
    for topic in sorted(by_topic):
        lines.append(f"### {topic}")
        for f in by_topic[topic]:
            day = (f.get("ts") or "")[:10]
            source = f.get("source") or "未注明"
            # 出处里已经带年份（如「方块目录.md（2026-10-16 核实）」）就不再补记录日期
            tail = source if re.search(r"\d{4}", source) else f"{source} · {day}"
            lines.append(f"- {f['text']} —— {tail}")
        lines.append("")
    lines.append(AUTO_END)
    return "\n".join(lines)


def consolidate(version: str | None = None) -> Path:
    """把 facts.jsonl 汇总进 knowledge.md 的自动区；手写区一个字都不动。"""
    p = doc_path(version)
    block = render_block(load(version))
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_DOC + "\n" + block + "\n", encoding="utf-8")
        return p
    text = p.read_text(encoding="utf-8")
    if AUTO_BEGIN in text and AUTO_END in text:
        pre = text[:text.index(AUTO_BEGIN)]
        post = text[text.index(AUTO_END) + len(AUTO_END):]
        p.write_text(pre + block + post.rstrip() + "\n", encoding="utf-8")
    else:  # 老文件没有标记 → 追加，不覆盖已有内容
        p.write_text(text.rstrip() + "\n\n## 自动汇总\n\n" + block + "\n", encoding="utf-8")
    return p


# -------------------------------- CLI --------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="knowledge")
    sub = ap.add_subparsers(dest="action", required=True)

    r = sub.add_parser("remember", help="记一条有出处的事实")
    r.add_argument("topic")
    r.add_argument("text")
    r.add_argument("--source", required=True, help="出处：工具回执 / 玩家原话 / 手册小节")
    r.add_argument("--tags", default="", help="逗号分隔")

    s = sub.add_parser("search"); s.add_argument("query")
    s.add_argument("--top", type=int, default=5)
    sub.add_parser("list")
    fg = sub.add_parser("forget"); fg.add_argument("id")
    sub.add_parser("consolidate")
    fm = sub.add_parser("fmt"); fm.add_argument("query", nargs="?", default="")
    args = ap.parse_args(argv)

    if args.action == "remember":
        tags = [t for t in args.tags.replace("，", ",").split(",") if t.strip()]
        rec, created = remember(args.topic, args.text, tags=tags, source=args.source)
        consolidate()   # 顺手刷新 knowledge.md，人也能直接看
        print(("已记下 " if created else "已有同样记录，命中 +1 ") + json.dumps(rec, ensure_ascii=False))
        return 0
    if args.action == "search":
        hits = search(args.query, args.top)
        if not hits:
            print(f"（知识库里没有和「{args.query}」相关的事实）")
            return 1
        for f in hits:
            print(f"[{f['id']}] {f['topic']}：{f['text']}（{f['source']}）")
        return 0
    if args.action == "list":
        facts = load()
        for f in facts:
            print(f"[{f['id']}] {f['topic']}：{f['text']}（{f['source']}）")
        print(f"共 {len(facts)} 条 / {len(topics())} 个话题 → {facts_path()}")
        return 0
    if args.action == "forget":
        ok = forget(args.id)
        if ok:
            consolidate()
        print("已删除" if ok else f"没有这条：{args.id}")
        return 0
    if args.action == "consolidate":
        print(f"已汇总 → {consolidate()}")
        return 0
    if args.action == "fmt":
        print(fmt_for_prompt(args.query) or "（没有相关记忆）")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
