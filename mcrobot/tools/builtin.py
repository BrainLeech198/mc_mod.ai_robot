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
        ("replace", "把选区内某方块替换成另一种"),
    ):
        reg.register(Tool(
            f"worldedit_{sub}", f"WorldEdit //{sub}：{desc}。",
            {"type": "object",
             "properties": {"block": {"type": "string", "description": "方块 ID，如 stone_bricks"}},
             "required": ["block"]}, DANGEROUS,
            (lambda s: (lambda a, c: _we(a, c, s)))(sub)))
    reg.register(Tool(
        "worldedit_undo", "撤回上一次 WorldEdit 操作（受撤回纪律约束）。",
        {"type": "object", "properties": {}, "required": []}, DANGEROUS, _t_we_undo))
    return reg


REGISTRY = build_registry()
