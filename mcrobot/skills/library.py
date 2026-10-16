#!/usr/bin/env python3
"""技能库：把「做成过的事」存成可复用的参数化模板（Voyager 三件套之三）。

前两件已经落地：iterative prompting（AgentLoop 多轮重试）+ environment feedback
（每次工具调用的结构化回执）。这一件解决的是**不复盘就永远重学**：
第一次盖墙要试几轮，之后同类任务直接调模板，省 token 也少犯错。

抄 Voyager 的三条规矩：
1. **只存验证过的**：技能来自「自检判成」的那次执行，不是模型凭空写的代码。
2. **检索先于规划**：接到目标先查库里有没有相近技能，把候选塞进观察里给决策器。
3. **带统计**：uses/success 记录，方便以后淘汰不好使的。

存储：`version/<版本>/skills.jsonl`（版本文件夹即身份 → 技能也跟着版本走，不串味）。
环境变量 MCROBOT_SKILLS_FILE 可临时指到别处（单测用）。

用法：
  python -m mcrobot.skills.library install      # 装预置建造技能
  python -m mcrobot.skills.library list
  python -m mcrobot.skills.library search 砌墙
  python -m mcrobot.skills.library show build_wall
  python -m mcrobot.skills.library render build_wall pos1=0,0,0 pos2=10,4,0 block=stone_bricks
  python -m mcrobot.skills.library rm build_wall
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import identity as idy
from ..paths import skills_file

# 只读/说话类工具不值得进技能（学会了也没用）
NON_SKILL_TOOLS = frozenset({"say", "get_state", "list_players", "get_inventory",
                             "block_lookup", "skill_search", "skill_save"})


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _path(version: str | None = None) -> Path:
    env = os.environ.get("MCROBOT_SKILLS_FILE")
    if env:
        return Path(env)
    return skills_file(version or idy.active_version())


# ------------------------------ 数据模型 ------------------------------


@dataclass
class Skill:
    name: str
    description: str
    steps: list[dict] = field(default_factory=list)   # [{"tool","arguments"}]
    params: dict[str, str] = field(default_factory=dict)  # 参数名 -> 默认值
    tags: list[str] = field(default_factory=list)
    source: str = "preset"                            # preset | learned
    uses: int = 0
    success: int = 0
    created: str = ""
    updated: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "steps": self.steps, "params": self.params, "tags": self.tags,
                "source": self.source, "uses": self.uses, "success": self.success,
                "created": self.created, "updated": self.updated}

    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            steps=[s for s in (d.get("steps") or []) if isinstance(s, dict)],
            params={str(k): str(v) for k, v in (d.get("params") or {}).items()},
            tags=[str(t) for t in (d.get("tags") or [])],
            source=str(d.get("source") or "learned"),
            uses=int(d.get("uses") or 0),
            success=int(d.get("success") or 0),
            created=str(d.get("created") or ""),
            updated=str(d.get("updated") or ""),
        )

    # ---------------- 渲染 ----------------

    def fill(self, **overrides: str) -> dict[str, str]:
        """合并默认值与调用方给的值；缺参数抛错（宁可不做，不可做错）。"""
        values = {k: str(overrides.get(k) or v) for k, v in self.params.items()}
        for k, v in overrides.items():
            values[str(k)] = str(v)
        missing = [p for p in _placeholders(self.steps) if not str(values.get(p, "")).strip()]
        if missing:
            raise ValueError(f"技能 {self.name} 缺参数：{', '.join(sorted(missing))}")
        return values

    def render(self, **overrides: str) -> list[dict]:
        """渲染成可直接喂给 Registry.call 的工具调用列表。"""
        values = self.fill(**overrides)
        out = []
        for st in self.steps:
            args = {}
            for k, v in (st.get("arguments") or {}).items():
                args[k] = _substitute(str(v), values)
            out.append({"name": st.get("tool") or "", "arguments": args})
        return out

    def signature(self) -> str:
        """步骤指纹：判断「同一件事是否已经会了」。"""
        return json.dumps([{"t": s.get("tool"), "a": s.get("arguments")}
                           for s in self.steps], sort_keys=True, ensure_ascii=False)

    def brief(self) -> dict:
        """给决策器看的摘要（别把整份 steps 塞进 prompt，太长）。"""
        return {"name": self.name, "description": self.description,
                "params": self.params,
                "success_rate": f"{self.success}/{self.uses}" if self.uses else "新技能"}


def _placeholders(steps: list[dict]) -> set[str]:
    out: set[str] = set()
    blob = json.dumps(steps, ensure_ascii=False)
    out.update(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", blob))
    return out


def _substitute(text: str, values: dict[str, str]) -> str:
    def rep(m):
        return str(values.get(m.group(1), m.group(0)))
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", rep, text)


# ------------------------------ 存取 ------------------------------


def load(version: str | None = None) -> list[Skill]:
    p = _path(version)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("name"):
            out.append(Skill.from_dict(d))
    return out


def _write_all(skills: list[Skill], version: str | None = None):
    p = _path(version)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for s in skills:
            f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")


def get(name: str, version: str | None = None) -> Skill | None:
    for s in load(version):
        if s.name == name:
            return s
    return None


def save(skill: Skill, version: str | None = None) -> Skill:
    """按名字 upsert。预置技能不允许被学到的覆盖（避免手改的模板被冲掉）。"""
    skill.updated = _now()
    if not skill.created:
        skill.created = skill.updated
    skills = load(version)
    for i, s in enumerate(skills):
        if s.name == skill.name:
            if s.source == "preset" and skill.source != "preset":
                return s  # 预置优先，静默跳过
            skills[i] = skill
            break
    else:
        skills.append(skill)
    _write_all(skills, version)
    return skill


def remove(name: str, version: str | None = None) -> bool:
    skills = load(version)
    left = [s for s in skills if s.name != name]
    if len(left) == len(skills):
        return False
    _write_all(left, version)
    return True


def record(name: str, ok: bool, version: str | None = None):
    """记一次使用成败（Voyager 靠这个淘汰不好使的技能）。"""
    skills = load(version)
    for s in skills:
        if s.name == name:
            s.uses += 1
            if ok:
                s.success += 1
            s.updated = _now()
            break
    else:
        return
    _write_all(skills, version)


# ------------------------------ 检索 ------------------------------


def _tokens(q: str) -> list[str]:
    """中英混合切词：英文按词，中文按 2 字滑窗（够用且零依赖）。"""
    q = (q or "").lower()
    out: list[str] = []
    # 英文短词（a / of / we）只会制造噪声，低于 3 个字母的一律丢掉；中文单字有用，保留
    out.extend(t for t in re.findall(r"[a-z0-9_]+", q) if len(t) >= 3)
    for run in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(run) <= 2:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return [t for t in out if t]


def _score(s: Skill, tokens: list[str]) -> int:
    if not tokens:
        return 0
    name, desc = s.name.lower(), s.description.lower()
    tags = " ".join(t.lower() for t in s.tags)
    blob = json.dumps(s.steps, ensure_ascii=False).lower()
    n = 0
    for t in tokens:
        if t in name:
            n += 4
        if t in tags:
            n += 3
        if t in desc:
            n += 2
        elif t in blob:
            n += 1
    # 用得顺手的排前面（但不让它盖过相关性）
    return n * 10 + min(s.success, 5)


def search(query: str, top_k: int = 3, version: str | None = None) -> list[Skill]:
    """关键词检索（无向量库依赖）。命中 0 分的一律不返回，免得污染决策。"""
    tokens = _tokens(query)
    scored = [( _score(s, tokens), s) for s in load(version)]
    hit = [(n, s) for n, s in scored if n >= 10]
    hit.sort(key=lambda x: (-x[0], x[1].name))
    return [s for _, s in hit[:max(0, top_k)]]


def find_by_signature(sig: str, version: str | None = None) -> Skill | None:
    for s in load(version):
        if s.signature() == sig:
            return s
    return None


# ------------------------------ 从一次成功执行里学 ------------------------------


def _slug(goal: str) -> str:
    """目标 → 技能名。中文保留（人看得懂），只去掉标点与空白。"""
    t = re.sub(r"[\s\W_]+", "-", (goal or "").strip(), flags=re.UNICODE).strip("-")
    return t[:24] or "skill"


def learn_from_actions(goal: str, actions: list[dict], *,
                       version: str | None = None) -> Skill | None:
    """把一次成功的动作序列存成 learned 技能。

    只收「真做了事」的步骤（过滤说话/感知），并跳过已会的一模一样的序列。
    """
    steps = [{"tool": a.get("name") or "", "arguments": a.get("arguments") or {}}
             for a in actions
             if (a.get("name") or "") not in NON_SKILL_TOOLS]
    if not steps:
        return None
    probe = Skill(name="probe", description="", steps=steps)
    old = find_by_signature(probe.signature(), version)
    if old:
        record(old.name, True, version)
        return old
    name = "learned-" + _slug(goal)
    base, i = name, 2
    while get(name, version):
        name = f"{base}-{i}"
        i += 1
    return save(Skill(name=name, description=f"从「{goal}」学到的动作序列",
                      steps=steps, source="learned", created=_now()), version)


# ------------------------------ 预置安装 ------------------------------


def install_presets(version: str | None = None) -> int:
    """装预置建造技能。已存在的按名字跳过（不覆盖手改过的）。"""
    from .build import preset_skills
    have = {s.name for s in load(version)}
    n = 0
    for sk in preset_skills():
        if sk.name in have:
            continue
        sk.created = sk.updated = _now()
        save(sk, version)
        n += 1
    return n


# -------------------------------- CLI --------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="skills")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("install", help="装预置建造技能")
    sub.add_parser("list")
    s = sub.add_parser("search"); s.add_argument("query")
    s.add_argument("--top", type=int, default=3)
    sh = sub.add_parser("show"); sh.add_argument("name")
    r = sub.add_parser("render"); r.add_argument("name")
    r.add_argument("assign", nargs="*", help="参数，形如 pos1=0,0,0 block=stone_bricks")
    d = sub.add_parser("rm"); d.add_argument("name")
    args = ap.parse_args(argv)

    if args.action == "install":
        print(f"已装 {install_presets()} 个预置技能 → {_path()}")
        return 0
    if args.action == "list":
        for sk in load():
            rate = f"{sk.success}/{sk.uses}" if sk.uses else "-"
            print(f"{sk.name:<22} [{sk.source}] 成功率 {rate:<6} {sk.description}")
        return 0
    if args.action == "search":
        hits = search(args.query, args.top)
        if not hits:
            print(f"（没检索到「{args.query}」相关的技能）")
            return 1
        for sk in hits:
            print(f"{sk.name:<22} {sk.description}")
            print(f"{'':<22} 参数: {json.dumps(sk.params, ensure_ascii=False)}")
        return 0
    if args.action == "show":
        sk = get(args.name)
        if not sk:
            print(f"没有技能 {args.name}")
            return 1
        print(json.dumps(sk.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.action == "render":
        sk = get(args.name)
        if not sk:
            print(f"没有技能 {args.name}")
            return 1
        kv = dict(a.split("=", 1) for a in args.assign if "=" in a)
        print(json.dumps(sk.render(**kv), ensure_ascii=False, indent=2))
        return 0
    if args.action == "rm":
        print("已删除" if remove(args.name) else f"没有技能 {args.name}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
