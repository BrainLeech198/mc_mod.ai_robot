#!/usr/bin/env python3
"""版本文件夹即身份：一个 version/<版本>/ 目录 = 一个完整 AI 身份。

设计原则（2026-10-16 用户拍板）：**不再另建平行配置层**。账号/服务器/客户端实例/mod
清单/方块素材/建造边界/撤回纪律/记忆/人格，全部以版本文件夹里的文件为唯一权威，
本模块只负责「读进来 → 拼成提示词」。改文档 = 改 AI 的认知，不用改代码。

版本文件夹里与本模块相关的文件：
  identity.md    人格 + 手写世界常识 + 后端选择（本模块解析 `## 元信息/人格/世界常识/后端`）
  config.json    {bot_name} 服务器地址 {server_host}（账号名/密码/皮肤/SFTP）
  runtime.json   Java 路径、客户端实例目录、Forge 版本、ControlMod 端口 → {server_note}
  mods.md        装了哪些 mod → 自动进「世界常识」
  方块目录.md     1426 个可用方块 → 自动进「世界常识」+ 支持 [[block]]关键词 查询
  workspace.json 建造区边界 → 自动进「活动边界」
  undo-policy.json 撤回纪律 → 自动进「活动边界」
  robot.json     聊天行为 + 危险操作白名单 {dangerous_allowed}（robot.py 直接读）
  memory/        长期记忆（按版本隔离）

用法：
  python -m mcrobot.identity versions
  python -m mcrobot.identity use 1.20.1-forge
  python -m mcrobot.identity show               # 解析出的身份（含自动生成的世界常识）
  python -m mcrobot.identity prompt             # 打印最终 system prompt（调试用）
  python -m mcrobot.identity blocks 课桌        # 查方块目录
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .paths import REPO, VERSIONS as VERSIONS_DIR
ACTIVE_PATH = VERSIONS_DIR / "active.json"
DEFAULT_VERSION = "1.20.1-forge"

BACKEND_PRESETS: dict[str, dict] = {
    "deepsink-local": {
        "label": "DeepSink 本机伪 API（mock_api :8788）",
        "endpoint": "http://127.0.0.1:8788/v1",
        "model": "deepsink-chat",
        "key_env": "MCROBOT_LLM_KEY",
        "key_hint": "DeepSink 平台 key（官网 /login 注册后生成）；本机默认可留空",
        "timeout_seconds": 90,
    },
    "deepseek-official": {
        "label": "DeepSeek 官方 API",
        "endpoint": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": "MCROBOT_LLM_KEY",
        "key_hint": "sk- 开头，官方平台创建",
        "timeout_seconds": 60,
    },
    "openai-compatible": {
        "label": "OpenAI 兼容（自定义端点）",
        "endpoint": "",
        "model": "",
        "key_env": "MCROBOT_LLM_KEY",
        "key_hint": "按服务商要求填（OpenAI/硅基流动/月之暗面/OpenRouter…）",
        "timeout_seconds": 60,
    },
}

# 自动生成世界常识时的抽样条数（每个 mod 分类抽几条代表性方块）
CATALOG_SAMPLE = 12


class IdentityError(Exception):
    pass


# ---------------- 版本定位 ----------------
def list_versions() -> list[str]:
    if not VERSIONS_DIR.exists():
        return []
    return sorted(p.name for p in VERSIONS_DIR.iterdir() if p.is_dir())


def active_version() -> str:
    try:
        if ACTIVE_PATH.exists():
            v = json.loads(ACTIVE_PATH.read_text(encoding="utf-8")).get("version")
            if v:
                return v
    except Exception:
        pass
    return os.environ.get("MCROBOT_VERSION") or DEFAULT_VERSION


def set_active(version: str) -> str:
    if version not in list_versions():
        raise IdentityError(f"没有这个版本目录：{version}（可用：{list_versions()}）")
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_PATH.write_text(
        json.dumps({"version": version,
                    "_说明": "当前激活的版本。改这一行即换 AI 身份；也可用环境变量 MCROBOT_VERSION 覆盖。"},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return version


def version_dir(version: str | None = None) -> Path:
    return VERSIONS_DIR / (version or active_version())


def _json_in(vdir: Path, name: str) -> dict:
    p = vdir / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _md_in(vdir: Path, name: str) -> str:
    p = vdir / name
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


# ---------------- identity.md 解析 ----------------
def _sections(text: str) -> dict[str, str]:
    """按 `## 标题` 切分，返回 {标题: 正文}。"""
    out: dict[str, str] = {}
    cur = ""
    for ln in text.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip()
            out[cur] = ""
        elif cur:
            out[cur] += ln + "\n"
    return out


def _kv(text: str) -> dict[str, str]:
    """解析 `键: 值` 行（忽略 markdown 列表/表格/注释行）。"""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip().lstrip("-*").strip()
        if not ln or ln.startswith("#") or ln.startswith("|") or ln.startswith(">"):
            continue
        if ":" in ln or "：" in ln:
            parts = re.split(r"[:：]", ln, maxsplit=1)
            if len(parts) != 2:
                continue
            out[parts[0].strip()] = parts[1].strip().strip("`")
    return out


def _bullets(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("- "):
            out.append(ln[2:].strip())
    return out


def save_sections(updates: dict[str, str], version: str | None = None) -> Path:
    """原地替换 identity.md 指定分节的正文，**保留其余内容**（表头/说明表/注释）。

    整文件重写会把「权威来源」表格这类手写说明冲掉，所以只能按分节改。
    """
    vdir = version_dir(version)
    path = vdir / "identity.md"
    text = _md_in(vdir, "identity.md")
    lines = text.splitlines()
    out: list[str] = []
    cur = ""
    done: set[str] = set()
    for ln in lines:
        if ln.startswith("## "):
            head = ln[3:].strip()
            # 标题可能带括号说明（如「世界常识（手写）」），按前缀匹配
            key = next((k for k in updates if head == k or head.startswith(k)), None)
            if key:
                out.append(ln)
                out.extend((updates[key] or "").splitlines())
                cur, done = key, done | {key}
                continue
            cur = head
        if cur in done and cur:
            continue  # 跳过被替换分节的旧正文
        out.append(ln)
    for k, v in updates.items():
        if k not in done:  # 文件里没有这个分节 → 追加
            out += ["", f"## {k}", "", *(v or "").splitlines()]
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return path


def _sec(sec: dict[str, str], name: str) -> str:
    """按前缀取分节：标题可以带括号说明（如「世界常识（手写）」）。"""
    if name in sec:
        return sec[name]
    for k, v in sec.items():
        if k.startswith(name):
            return v
    return ""


def load_identity(version: str | None = None) -> dict:
    """解析版本文件夹的 identity.md → {meta, persona, world_facts, backend}。"""
    vdir = version_dir(version)
    sec = _sections(_md_in(vdir, "identity.md"))
    if not sec:
        raise IdentityError(f"{vdir.name}/identity.md 不存在或没有 ## 分节")
    return {
        "version": vdir.name,
        "dir": str(vdir.relative_to(REPO)).replace("\\", "/"),
        "meta": _kv(_sec(sec, "元信息")),
        "persona": _sec(sec, "人格").strip(),
        "world_facts": _bullets(_sec(sec, "世界常识")),
        "backend": _kv(_sec(sec, "后端")),
    }


# ---------------- 从版本文档自动生成世界常识 ----------------
def mods_summary(vdir: Path | None = None) -> dict:
    """从 mods.md 抽客户端 mod 名（形如 `[中文名] xxx.jar`）。"""
    vdir = vdir or version_dir()
    txt = _md_in(vdir, "mods.md")
    names: list[str] = []
    in_block = False
    for ln in txt.splitlines():
        if ln.strip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            m = re.match(r"^\s*\[(.+?)\]", ln)
            if m:
                names.append(m.group(1).strip())
    return {"count": len(names), "names": names}


def catalog_summary(vdir: Path | None = None, sample: int = CATALOG_SAMPLE) -> list[dict]:
    """从方块目录.md 抽每个 mod 分类的方块数与代表性方块。"""
    vdir = vdir or version_dir()
    txt = _md_in(vdir, "方块目录.md")
    cats: list[dict] = []
    cur: dict | None = None
    for ln in txt.splitlines():
        m = re.match(r"^##\s+(.+?)\s*/\s*([A-Za-z0-9_.\-]+)\s*[（(](\d+)\s*块", ln)
        if m:
            cur = {"label": m.group(1).strip(), "modid": m.group(2),
                   "count": int(m.group(3)), "rows": []}
            cats.append(cur)
            continue
        if cur is not None:
            m2 = re.match(r"^\|\s*`([A-Za-z0-9_.\-:]+)`\s*\|\s*([^|]+?)\s*\|", ln)
            if m2:
                cur["rows"].append((m2.group(1), m2.group(2).strip()))
    # 均匀抽样：只取前 N 条会全是 acacia_/anti_ 之类的同前缀方块，代表性为零
    for c in cats:
        rows = c.pop("rows")
        step = max(1, len(rows) // sample)
        c["samples"] = rows[::step][:sample]
    return cats


def search_blocks(keyword: str, limit: int = 20, vdir: Path | None = None) -> list[dict]:
    """按关键词查方块目录（匹配方块 ID 或中文名）。"""
    vdir = vdir or version_dir()
    txt = _md_in(vdir, "方块目录.md")
    kw = (keyword or "").strip().lower()
    hits: list[dict] = []
    if not kw:
        return hits
    modid = ""
    for ln in txt.splitlines():
        m0 = re.match(r"^##\s+.+?/\s*([A-Za-z0-9_.\-]+)", ln)
        if m0:
            modid = m0.group(1)
            continue
        m = re.match(r"^\|\s*`([A-Za-z0-9_.\-:]+)`\s*\|\s*([^|]+?)\s*\|", ln)
        if not m:
            continue
        bid, name = m.group(1), m.group(2).strip()
        if kw in bid.lower() or kw in name.lower():
            hits.append({"id": bid, "name": name, "mod": modid})
            if len(hits) >= limit:
                break
    return hits


def auto_world_facts(vdir: Path | None = None) -> list[str]:
    """自动生成：装了哪些 mod + 有哪些方块（抽样）。改文档即改这里。"""
    vdir = vdir or version_dir()
    out: list[str] = []
    mods = mods_summary(vdir)
    if mods["count"]:
        keys = ("创世神", "家具", "机械", "铁路", "城建", "车辆", "飞机", "砍树")
        # 「前置·被XX依赖」这类名字又长又没信息量，只留能看懂的
        key = [n for n in mods["names"]
               if any(k in n for k in keys) and "前置·被" not in n]
        rest = mods["count"] - len(key)
        out.append(f"客户端装了 {mods['count']} 个 mod，跟建造/交通有关的是："
                   + "、".join(key[:10]) + (f"，另有 {rest} 个（含前置库）" if rest > 0 else "") + "。")
    cats = catalog_summary(vdir)
    if cats:
        total = sum(c["count"] for c in cats)
        out.append(f"可用方块共 {total} 块，按 mod 分："
                   + "、".join(f"{c['label']}/{c['modid']} {c['count']} 块" for c in cats) + "。")
        for c in cats:
            if c["samples"]:
                s = "、".join(f"`{i}`（{n}）" for i, n in c["samples"][:6])
                out.append(f"{c['label']} 抽样：{s}（还有 {c['count'] - len(c['samples'])} 块）")
        out.append("要用具体方块 ID 时用 [[block]]关键词 查 `方块目录.md`，别凭空编方块名。")
    return out


# ---------------- 边界与纪律 ----------------
def boundaries(vdir: Path | None = None) -> list[str]:
    vdir = vdir or version_dir()
    out: list[str] = []
    ws = _json_in(vdir, "workspace.json")
    b = (ws.get("bounds") or {})
    if b.get("min") and b.get("max"):
        mn, mx = b["min"], b["max"]
        world = ws.get("world", "")
        out.append(f"建造区（{world}）：x {mn.get('x')}..{mx.get('x')}、"
                   f"y {mn.get('y')}..{mx.get('y')}、z {mn.get('z')}..{mx.get('z')}；"
                   f"越界一律拒绝（enforcement.reject_out_of_bounds="
                   f"{bool((ws.get('enforcement') or {}).get('reject_out_of_bounds'))}）。")
    up = _json_in(vdir, "undo-policy.json")
    if up:
        out.append(f"撤回纪律：WE 历史上限 {up.get('we_history_limit')} 次，"
                   f"剩余 ≤ {up.get('stop_threshold')} 次必须停手上报；"
                   f"批量前打点={bool((up.get('rules') or {}).get('checkpoint_before_batch'))}、"
                   f"只撤自己的改动={bool((up.get('rules') or {}).get('undo_only_own_changes'))}。")
    return out


# ---------------- 占位符上下文 ----------------
def context(version: str | None = None) -> dict:
    """提示词占位符的值——全部来自版本文件夹，不在别处重复写。"""
    vdir = version_dir(version)
    ident = load_identity(version)
    cfg = _json_in(vdir, "config.json")
    rt = _json_in(vdir, "runtime.json")
    robot = _json_in(vdir, "robot.json")
    server = cfg.get("server") or {}
    client = (rt.get("client") or {})
    host = server.get("host", "")
    port = server.get("port", "")
    meta = ident["meta"]
    note_parts = []
    if host:
        note_parts.append(f"{host}:{port}" if port else host)
    if client.get("forge"):
        note_parts.append(f"Forge {client['forge']}")
    if client.get("launcher_type"):
        note_parts.append(f"{client['launcher_type']} 客户端")
    if client.get("instance_dir"):
        note_parts.append(f"实例 {Path(str(client['instance_dir'])).name}")
    return {
        "bot_name": (cfg.get("account") or {}).get("username") or meta.get("名字", "AI"),
        "server_name": meta.get("服务器名", "服务器"),
        "server_host": f"{host}:{port}" if host and port else (host or "未知"),
        "server_note": "，".join(note_parts) or "（见 config.json / runtime.json）",
        "home": meta.get("常驻维度", "主城"),
        "max_chars": meta.get("回复字数上限", 60),
        "language": meta.get("语言", "zh"),
        "dangerous_allowed": "、".join((robot.get("auth") or {}).get("dangerous_allowed", [])) or "服主",
    }


# ---------------- 路径与后端 ----------------
def robot_json_path(version: str | None = None) -> Path:
    return version_dir(version) / "robot.json"


def memory_path(version: str | None = None) -> Path:
    return version_dir(version) / "memory" / "robot_memory.jsonl"


def memory_dir(version: str | None = None) -> Path:
    return version_dir(version) / "memory"


def resolve_backend(version: str | None = None) -> dict:
    b = load_identity(version)["backend"]
    tpl = BACKEND_PRESETS.get(b.get("模板", ""), {})
    out = {**tpl, **{k: v for k, v in b.items() if v}}
    # identity.md 用中文键，统一成字段名
    mapped = {
        "endpoint": b.get("endpoint") or tpl.get("endpoint", ""),
        "model": b.get("model") or tpl.get("model", ""),
        "key_env": b.get("key_env") or tpl.get("key_env", "MCROBOT_LLM_KEY"),
        "timeout_seconds": float(b.get("timeout_seconds") or tpl.get("timeout_seconds") or 60),
        "preset": b.get("模板") or "deepsink-local",
    }
    out.update(mapped)
    out["key_env"] = mapped["key_env"]
    out["key_set"] = bool(os.environ.get(mapped["key_env"], ""))
    out["label"] = tpl.get("label", b.get("模板") or "自定义")
    return out


# ---------------- 提示词渲染 ----------------
def render_system(version: str | None = None, memory_block: str = "") -> str:
    """人格 + 自动世界常识 + 手写常识 + 边界纪律 + 记忆 → 最终 system prompt。"""
    vdir = version_dir(version)
    ident = load_identity(version)
    ctx = context(version)
    text = ident["persona"] or "你是 {bot_name}，一个在线玩服务器的 AI 玩家。"
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", str(v))

    blocks: list[str] = []
    auto = auto_world_facts(vdir)
    if auto:
        blocks.append("【这个世界里有这些】（自动生成自 mods.md / 方块目录.md，改文档即生效）\n"
                      + "\n".join(f"- {f}" for f in auto))
    if ident["world_facts"]:
        blocks.append("【这些是经验，别踩坑】\n"
                      + "\n".join(f"- {f}" for f in ident["world_facts"]))
    bd = boundaries(vdir)
    if bd:
        blocks.append("【活动边界与纪律】\n" + "\n".join(f"- {f}" for f in bd))
    if blocks:
        text += "\n\n" + "\n\n".join(blocks)
    if memory_block:
        text += "\n\n" + memory_block
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="identity")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("versions")
    sub.add_parser("show")
    sub.add_parser("prompt")
    u = sub.add_parser("use"); u.add_argument("version")
    b = sub.add_parser("blocks"); b.add_argument("keyword")
    b.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)

    if args.action == "versions":
        cur = active_version()
        for v in list_versions():
            print(f"{'*' if v == cur else ' '} {v}")
        return 0
    if args.action == "use":
        print(f"已切换到 {set_active(args.version)}")
        return 0
    if args.action == "show":
        print(json.dumps({"identity": load_identity(), "context": context(),
                          "auto_world_facts": auto_world_facts(),
                          "boundaries": boundaries(),
                          "backend": resolve_backend()},
                         ensure_ascii=False, indent=2))
        return 0
    if args.action == "prompt":
        print(render_system())
        return 0
    if args.action == "blocks":
        hits = search_blocks(args.keyword, args.limit)
        if not hits:
            print(f"（没找到「{args.keyword}」）")
            return 1
        for h in hits:
            print(f"{h['id']}\t{h['name']}\t[{h['mod']}]")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
