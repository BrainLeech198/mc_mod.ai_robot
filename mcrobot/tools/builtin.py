"""内置工具集。

分类学抄 minecraft-mcp-server（感知 / 动作 / 方块 / 聊天），权限门控抄
mcagent 的教训（绝不默认放行管理类指令）。
"""
from __future__ import annotations

import json

from ..body import bridge as cc
from ..body import reflex
from ..brain import protocol
from .registry import (DANGEROUS, SAFE, CallContext, Refused, Registry, Tool,
                       ToolResult)

# ------------------------------ 感知（只读） ------------------------------


def _t_get_state(_args: dict, ctx: CallContext) -> ToolResult:
    st = cc.state()
    p = st.get("player") or {}
    return ToolResult(True, "get_state", "", data=st,
                      text=json.dumps({"in_game": st.get("inGame"), **p},
                                      ensure_ascii=False))


def _t_list_players(_args: dict, ctx: CallContext) -> ToolResult:
    out = cc.cmd("/list")
    return ToolResult(True, "list_players", "", data={"raw": out}, text=str(out)[:500])


def _t_get_inventory(_args: dict, ctx: CallContext) -> ToolResult:
    out = cc.cmd("/data get entity @s Inventory")
    return ToolResult(True, "get_inventory", "", data={"raw": out}, text=str(out)[:800])


def _t_block_lookup(args: dict, ctx: CallContext) -> ToolResult:
    """查方块目录（version/<版本>/方块目录.md）。显示名是英文，中文查不到要换英文。"""
    from ..identity import search_blocks
    kw = str(args.get("query") or "").strip()
    if not kw:
        return ToolResult(False, "block_lookup", "", error="query 为空")
    limit = int(args.get("limit") or 20)
    hits = search_blocks(kw, limit, None)
    lines = [f"`{h['id']}`（{h.get('name', '')}）" for h in hits]
    return ToolResult(True, "block_lookup", "", data={"hits": hits},
                      text=("\n".join(lines) if lines
                            else f"没查到「{kw}」。方块目录里的显示名是英文，换英文关键词再试。"))


# ------------------------------ 动作 ------------------------------


def _t_say(args: dict, ctx: CallContext) -> ToolResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(False, "say", "", error="text 为空")
    if ctx.dry:
        return ToolResult(True, "say", "", text=f"[dry] 会说：{text}")
    protocol.emit({"say": text})
    return ToolResult(True, "say", "", data={"text": text}, text=f"已排队：{text}")


def _run_one(command: str, ctx: CallContext) -> ToolResult:
    """执行一条指令，先过危险判定。"""
    cmd = command.strip()
    if not cmd:
        return ToolResult(False, "run_command", "", error="command 为空")
    intent = reflex.analyze(cmd)
    if intent.dangerous and ctx.actor not in ctx.dangerous_allowed:
        raise Refused(f"未授权：{intent.reason}（{cmd}）")
    if ctx.dry:
        return ToolResult(True, "run_command", "", text=f"[dry] 会执行：{cmd}")
    out = cc.cmd(cmd)
    return ToolResult(True, "run_command", "", data={"command": cmd, "raw": out},
                      text=str(out)[:800])


def _t_run_command(args: dict, ctx: CallContext) -> ToolResult:
    return _run_one(str(args.get("command") or ""), ctx)


def _t_run_batch(args: dict, ctx: CallContext) -> ToolResult:
    """批量执行（抄 mcagent「合并命令」：一次回合多条指令，省往返）。"""
    cmds = args.get("commands") or []
    if not isinstance(cmds, list) or not cmds:
        return ToolResult(False, "run_batch", "", error="commands 必须是非空数组")
    budget = int(args.get("budget") or 20)
    if len(cmds) > budget:
        return ToolResult(False, "run_batch", "",
                          error=f"一次最多 {budget} 条，收到 {len(cmds)} 条（行动预算）")
    outs = []
    for c in cmds:
        try:
            r = _run_one(str(c), ctx)
        except Refused as e:
            outs.append({"command": c, "ok": False, "error": str(e)})
            break  # 失败止损：被拒就停，不继续刷
        outs.append({"command": c, "ok": r.ok, "output": r.text})
        if not r.ok:
            break
    ok = all(o.get("ok") for o in outs)
    return ToolResult(ok, "run_batch", "", data={"results": outs},
                      text=json.dumps(outs, ensure_ascii=False)[:1500])


def _we(args: dict, ctx: CallContext, sub: str) -> ToolResult:
    block = str(args.get("block") or "").strip()
    if not block:
        return ToolResult(False, f"worldedit_{sub}", "", error="block 为空")
    if sub == "replace":
        # //replace 要两个参数：源方块 + 目标方块，只给一个会造成整片区域被误填
        to = str(args.get("to") or "").strip()
        if not to:
            return ToolResult(False, "worldedit_replace", "",
                              error="replace 需要 block（源）和 to（目标）两个方块")
        return _run_one(f"//replace {block} {to}", ctx)
    return _run_one(f"//{sub} {block}", ctx)


def _t_we_select(args: dict, ctx: CallContext) -> ToolResult:
    p1 = str(args.get("pos1") or "").strip()
    p2 = str(args.get("pos2") or "").strip()
    if not p1 or not p2:
        return ToolResult(False, "worldedit_select", "", error="pos1/pos2 都要给，形如 x,y,z")
    r1 = _run_one(f"//pos1 {p1}", ctx)
    if not r1.ok:
        return r1
    return _run_one(f"//pos2 {p2}", ctx)


def _t_we_undo(_args: dict, ctx: CallContext) -> ToolResult:
    return _run_one("//undo", ctx)


# ------------------------------ 技能库 ------------------------------

# 技能里不允许再套技能工具（防递归、防凭空造技能）
_SKILL_TOOLS = frozenset({"skill_search", "skill_run", "skill_save"})


def _t_skill_search(args: dict, ctx: CallContext) -> ToolResult:
    from ..skills import library as skl
    q = str(args.get("query") or "").strip()
    if not q:
        return ToolResult(False, "skill_search", "", error="query 为空")
    hits = skl.search(q, int(args.get("top_k") or 3))
    if not hits:
        return ToolResult(True, "skill_search", "", data={"hits": []},
                          text=f"没有与「{q}」相关的已有技能，这次从头做。")
    briefs = [h.brief() for h in hits]
    return ToolResult(True, "skill_search", "", data={"hits": briefs},
                      text=json.dumps(briefs, ensure_ascii=False))


def _t_skill_run(args: dict, ctx: CallContext) -> ToolResult:
    """跑一个技能模板。每一步都过注册表 → 危险门控照旧生效。"""
    from ..skills import library as skl
    name = str(args.get("name") or "").strip()
    sk = skl.get(name) if name else None
    if not sk:
        return ToolResult(False, "skill_run", "", error=f"没有技能 {name}（先 skill_search）")
    params = {k: v for k, v in (args.get("params") or {}).items()}
    try:
        calls = sk.render(**params)
    except ValueError as e:
        return ToolResult(False, "skill_run", "", error=str(e))
    outs = []
    for c in calls:
        if c["name"] in _SKILL_TOOLS:
            outs.append({"tool": c["name"], "ok": False, "error": "技能里不能套技能"})
            break
        r = REGISTRY.call(c["name"], c["arguments"], ctx)
        outs.append({"tool": r.tool, "ok": r.ok, "output": r.text, "error": r.error})
        if not r.ok:
            break  # 失败止损，和 run_batch 一致
    ok = bool(outs) and all(o.get("ok") for o in outs)
    try:
        skl.record(name, ok)
    except Exception:
        pass
    return ToolResult(ok, "skill_run", "", data={"name": name, "results": outs},
                      text=json.dumps(outs, ensure_ascii=False)[:1500])


def _t_skill_save(args: dict, ctx: CallContext) -> ToolResult:
    """把一串验证过的动作存成技能（Voyager：只存做成过的）。"""
    from ..skills import library as skl
    name = str(args.get("name") or "").strip()
    steps = args.get("steps") or []
    if not name or not isinstance(steps, list) or not steps:
        return ToolResult(False, "skill_save", "", error="name 和 steps 都要给")
    clean = []
    for st in steps:
        if not isinstance(st, dict) or not st.get("tool"):
            return ToolResult(False, "skill_save", "", error="steps 每项要含 tool")
        if st["tool"] in _SKILL_TOOLS:
            return ToolResult(False, "skill_save", "", error=f"{st['tool']} 不能进技能")
        try:
            REGISTRY.tool(st["tool"])       # 工具必须真实存在，别存废技能
        except KeyError as e:
            return ToolResult(False, "skill_save", "", error=str(e))
        clean.append({"tool": st["tool"], "arguments": st.get("arguments") or {}})
    sk = skl.Skill(name=name,
                   description=str(args.get("description") or f"模型整理的技能 {name}"),
                   steps=clean,
                   params={str(k): str(v) for k, v in (args.get("params") or {}).items()},
                   tags=[str(t) for t in (args.get("tags") or [])],
                   source="learned", created="")
    skl.save(sk)
    return ToolResult(True, "skill_save", "", data=sk.to_dict(),
                      text=f"技能 {name} 已入库（{len(clean)} 步）")


# ------------------------------ 注册 ------------------------------


def build_registry() -> Registry:
    reg = Registry()

    reg.register(Tool(
        "get_state", "读机器人当前状态：是否在游戏、坐标、维度、朝向、血量。",
        {"type": "object", "properties": {}, "required": []}, SAFE, _t_get_state))
    reg.register(Tool(
        "list_players", "查服务器在线玩家。",
        {"type": "object", "properties": {}, "required": []}, SAFE, _t_list_players))
    reg.register(Tool(
        "get_inventory", "读自己背包内容。",
        {"type": "object", "properties": {}, "required": []}, SAFE, _t_get_inventory))
    reg.register(Tool(
        "block_lookup",
        "按关键词查本服可用方块（含模组方块），返回真实方块 ID。注意目录里的显示名是英文，"
        "中文查不到要换成英文关键词（如 desk / chair / lamp / rubbish_bin / toilet）。",
        {"type": "object",
         "properties": {"query": {"type": "string", "description": "关键词，英文优先"},
                        "limit": {"type": "integer", "default": 20}},
         "required": ["query"]}, SAFE, _t_block_lookup))

    reg.register(Tool(
        "say", "在游戏里说一句话（会经身体发出）。",
        {"type": "object",
         "properties": {"text": {"type": "string", "description": "要说的原话"}},
         "required": ["text"]}, SAFE, _t_say))
    reg.register(Tool(
        "run_command",
        "执行一条 Minecraft 指令。只读指令放行；改世界/权限/管理类指令只认服主白名单。",
        {"type": "object",
         "properties": {"command": {"type": "string", "description": "含斜杠的指令，如 /list"}},
         "required": ["command"]}, SAFE, _t_run_command))
    reg.register(Tool(
        "run_batch",
        "批量执行指令（省往返）。任一条失败即停手（失败止损），默认上限 20 条（行动预算）。",
        {"type": "object",
         "properties": {"commands": {"type": "array", "items": {"type": "string"}},
                        "budget": {"type": "integer", "default": 20}},
         "required": ["commands"]}, SAFE, _t_run_batch))

    reg.register(Tool(
        "worldedit_select",
        "设 WorldEdit 选区。pos1/pos2 形如 'x,y,z'（相对坐标可写 ~）。",
        {"type": "object",
         "properties": {"pos1": {"type": "string"}, "pos2": {"type": "string"}},
         "required": ["pos1", "pos2"]}, SAFE, _t_we_select))
    for sub, desc in (
        ("set", "用指定方块填满选区"),
        ("walls", "沿选区边界砌墙"),
        ("hollow", "把选区做成空心（墙+地板+天花板）"),
    ):
        reg.register(Tool(
            f"worldedit_{sub}", f"WorldEdit //{sub}：{desc}。",
            {"type": "object",
             "properties": {"block": {"type": "string", "description": "方块 ID，如 stone_bricks"}},
             "required": ["block"]}, DANGEROUS,
            (lambda s: (lambda a, c: _we(a, c, s)))(sub)))
    reg.register(Tool(
        "worldedit_replace",
        "WorldEdit //replace：把选区内某方块替换成另一种（两个方块都要给）。",
        {"type": "object",
         "properties": {"block": {"type": "string", "description": "源方块 ID，如 dirt"},
                        "to": {"type": "string", "description": "目标方块 ID，如 grass_block"}},
         "required": ["block", "to"]}, DANGEROUS,
        lambda a, c: _we(a, c, "replace")))
    reg.register(Tool(
        "worldedit_undo", "撤回上一次 WorldEdit 操作（受撤回纪律约束）。",
        {"type": "object", "properties": {}, "required": []}, DANGEROUS, _t_we_undo))

    reg.register(Tool(
        "skill_search",
        "查技能库里有没有做成过的相近技能（返回摘要，不返回完整步骤）。干活前先查，别重复造轮子。",
        {"type": "object",
         "properties": {"query": {"type": "string", "description": "要干什么，如「砌一圈墙」"},
                        "top_k": {"type": "integer", "default": 3}},
         "required": ["query"]}, SAFE, _t_skill_search))
    reg.register(Tool(
        "skill_run",
        "跑一个已入库的技能模板。每一步照样过危险门控：没授权的步骤会被拒。",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "技能名，如 build_wall"},
                        "params": {"type": "object",
                                   "description": "参数值，如 {pos1:'0,0,0', pos2:'10,4,0'}"}},
         "required": ["name"]}, SAFE, _t_skill_run))
    reg.register(Tool(
        "skill_save",
        "把一串**已经做成过**的动作存成可复用技能（带 {参数} 占位更好用）。只存验证过的。",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "技能名，英文小写下划线"},
                        "description": {"type": "string"},
                        "steps": {"type": "array",
                                  "items": {"type": "object",
                                            "properties": {"tool": {"type": "string"},
                                                           "arguments": {"type": "object"}}}},
                        "params": {"type": "object", "description": "参数名 → 默认值"},
                        "tags": {"type": "array", "items": {"type": "string"}}},
         "required": ["name", "steps"]}, SAFE, _t_skill_save))
    return reg


REGISTRY = build_registry()
