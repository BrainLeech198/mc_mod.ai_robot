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


def _probe_points(points: list[str], block: str, timeout: float = 2.5) -> dict:
    """用 /execute if block 探点：命中就在聊天里回一个唯一 token，再读回来。

    为什么走这条路：ControlMod 还没有「读方块」原语，但服务端有 OP 就能用
    /execute if block 把判断结果写成一条聊天，桥本来就能读聊天。
    等于先用零 Java 改动换来一双眼睛（之后扩 mod 是正解，这是过渡）。
    """
    import time as _t
    import uuid

    base = int(cc.chat_since(0).get("seq") or 0)
    tokens = {}
    for i, p in enumerate(points):
        tok = f"PB{uuid.uuid4().hex[:6]}"
        tokens[tok] = p
        # tellraw @s 只有机器人自己看得见，不会把探测 token 刷到全服聊天
        cc.cmd(f'/execute if block {p} {block} run tellraw @s {{"text":"{tok}"}}')
        _t.sleep(0.12)
    hit, seen, deadline = set(), [], _t.time() + timeout
    while _t.time() < deadline:
        for r in cc.chat_since(base).get("chats") or []:
            txt = str(r.get("text") or "")
            for tok, p in tokens.items():
                if tok in txt and tok not in hit:
                    hit.add(tok)
            if txt and txt not in [s["text"] for s in seen]:
                seen.append({"text": txt[:120]})
        if len(hit) >= len(tokens):
            break
        _t.sleep(0.25)
    miss = [p for tok, p in tokens.items() if tok not in hit]
    return {"total": len(points), "hit": len(hit),
            "matched": [tokens[t] for t in tokens if t in hit], "missed": miss,
            "chat_seen": seen[:6]}


def _t_block_probe(args: dict, ctx: CallContext) -> ToolResult:
    pts = args.get("points") or []
    if isinstance(pts, str):
        pts = [pts]
    pts = [str(p).replace(",", " ").strip() for p in pts if str(p).strip()]
    block = str(args.get("block") or "").strip()
    if not pts:
        return ToolResult(False, "block_probe", "", error="points 不能为空，形如 [\"30 48 -191\"]")
    if not block:
        return ToolResult(False, "block_probe", "", error="block 不能为空，如 stone / minecraft:oak_planks")
    if len(pts) > 24:
        return ToolResult(False, "block_probe", "", error="一次最多探 24 个点（省往返、防刷屏）")
    out = _probe_points(pts, block)
    ok = out["hit"] == out["total"]
    text = f"{out['hit']}/{out['total']} 个点是 {block}"
    if out["missed"]:
        text += f"；不是 {block} 的：{', '.join(out['missed'][:6])}"
    return ToolResult(ok, "block_probe", "", text=text, data=out)


def _parse_int_pos(text: str) -> tuple[int, int, int]:
    parts = str(text).replace(",", " ").split()
    if len(parts) != 3:
        raise ValueError(f"坐标要三个数：{text}")
    return tuple(int(float(p)) for p in parts)  # type: ignore[return-value]


def _t_block_at(args: dict, ctx: CallContext) -> ToolResult:
    try:
        x, y, z = int(args["x"]), int(args["y"]), int(args["z"])
    except (KeyError, TypeError, ValueError):
        return ToolResult(False, "block_at", "", error="x/y/z 都要给整数")
    try:
        b = cc.block_at(x, y, z)
    except cc.ControlError as e:
        return ToolResult(False, "block_at", "", error=str(e))
    if not b.get("loaded"):
        return ToolResult(True, "block_at", "", data=b,
                          text=f"({x},{y},{z}) 所在区块还没加载，读不到（不是空气）")
    return ToolResult(True, "block_at", "", data=b, text=f"({x},{y},{z}) = {b.get('id')}")


def _t_scan_region(args: dict, ctx: CallContext) -> ToolResult:
    try:
        p1 = _parse_int_pos(args["pos1"])
        p2 = _parse_int_pos(args["pos2"])
    except (KeyError, ValueError) as e:
        return ToolResult(False, "scan_region", "", error=f"pos1/pos2 要形如 'x y z'：{e}")
    try:
        r = cc.blocks_in_box(*p1, *p2, limit=int(args.get("limit") or 512))
    except cc.ControlError as e:
        return ToolResult(False, "scan_region", "", error=str(e))
    blocks = r.get("blocks") or []
    counts: dict[str, int] = {}
    for b in blocks:
        key = b.get("id") if b.get("loaded") else ""
        counts[key] = counts.get(key, 0) + 1
    unloaded = sum(1 for b in blocks if not b.get("loaded"))
    head = (f"{r.get('total')} 格，返回 {len(blocks)} 格"
            + ("（已截断）" if r.get("truncated") else "")
            + (f"，其中 {unloaded} 格未加载" if unloaded else ""))
    if len(blocks) <= 40:
        lines = [f"({b['x']},{b['y']},{b['z']}) = {b['id'] if b.get('loaded') else '未加载'}"
                 for b in blocks]
    else:
        lines = [f"{k or '未加载'} x{v}"
                 for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:12]]
    return ToolResult(True, "scan_region", "", data={**r, "counts": counts},
                      text=head + "\n" + "\n".join(lines))


def _t_looking_at(_args: dict, ctx: CallContext) -> ToolResult:
    try:
        h = cc.looking_at()
    except cc.ControlError as e:
        return ToolResult(False, "looking_at", "", error=str(e))
    kind = h.get("type")
    if kind == "none":
        return ToolResult(True, "looking_at", "", data=h, text="准星没对着任何方块或实体")
    what = "方块" if kind == "block" else "实体"
    return ToolResult(True, "looking_at", "", data=h,
                      text=f"准星对着{what} {h.get('id')} @ "
                           f"({h.get('x')},{h.get('y')},{h.get('z')})，距离 {h.get('distance')}")


def _t_screenshot(args: dict, ctx: CallContext) -> ToolResult:
    try:
        max_width = int(args.get("max_width") or 854)
    except (TypeError, ValueError):
        return ToolResult(False, "screenshot", "", error="max_width 要给整数")
    try:
        shot = cc.screenshot(max_width=max_width)
    except cc.ControlError as e:
        return ToolResult(False, "screenshot", "", error=str(e))
    kb = max(1, round(shot["bytes"] / 1024))
    return ToolResult(
        True, "screenshot", "", data=shot,
        text=(f"截图已保存：{shot['path']}（{shot['width']}×{shot['height']}，{kb} KB，"
              f"抓帧 {shot['capture_ms']}ms）"))


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


# ------------------------------ 移动（Baritone） ------------------------------


def _t_goto(args: dict, ctx: CallContext) -> ToolResult:
    """Baritone 寻路。#goto 是聊天前缀，必须走 cc.chat；走 cc.cmd 会变成 /goto。"""
    try:
        x, y, z = float(args["x"]), float(args["y"]), float(args["z"])
    except (KeyError, TypeError, ValueError):
        return ToolResult(False, "goto", "", error="x/y/z 都要给数字")
    if ctx.dry:
        return ToolResult(True, "goto", "", text=f"[dry] 会走到 {x:g} {y:g} {z:g}")
    out = cc.chat(f"#goto {x:g} {y:g} {z:g}")
    ok = bool(out.get("ok")) if isinstance(out, dict) else True
    return ToolResult(ok, "goto", "", data={"target": [x, y, z]},
                      text=f"Baritone 已出发去 ({x:g}, {y:g}, {z:g})；走完用 get_state 看是否到位")


def _t_goto_stop(_args: dict, ctx: CallContext) -> ToolResult:
    cc.chat("#stop")
    return ToolResult(True, "goto_stop", "", text="已让 Baritone 停手")


def _t_follow(args: dict, ctx: CallContext) -> ToolResult:
    who = str(args.get("player") or "").strip()
    if not who:
        return ToolResult(False, "follow", "", error="player 为空")
    cc.chat(f"#follow {who}")
    return ToolResult(True, "follow", "", text=f"Baritone 开始跟随 {who}（用 goto_stop 停）")


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
        "block_probe",
        "探测指定坐标现在是什么方块（只读，靠 /execute if block，需要机器人是 OP）。"
        "砌完东西要自检就用它：points 填坐标列表，block 填期望方块 ID，"
        "返回「命中数/总数」，命中不满就是没砌对。",
        {"type": "object",
         "properties": {
             "points": {"type": "array", "items": {"type": "string"},
                        "description": "坐标列表，每项形如 \"30 48 -191\" 或 \"30,48,-191\""},
             "block": {"type": "string", "description": "期望的方块 ID，如 stone / minecraft:oak_planks"}},
         "required": ["points", "block"]}, SAFE, _t_block_probe))

    reg.register(Tool(
        "block_at",
        "读指定坐标现在是什么方块（客户端本地读，毫秒级，不需要 OP）。"
        "验收砌墙/建造时优先用它，逐点核对；命令回执说成功不等于方块真的变了。"
        "区块没加载会明确返回 loaded=false，不能当空气。",
        {"type": "object",
         "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                        "z": {"type": "integer"}},
         "required": ["x", "y", "z"]}, SAFE, _t_block_at))
    reg.register(Tool(
        "scan_region",
        "读一个长方体区域内的方块（含端点），返回逐格坐标+ID（格数多时给统计）。"
        "适合验收整面墙/整间房：一次拿全，比逐点探快。上限 4096 格。",
        {"type": "object",
         "properties": {"pos1": {"type": "string", "description": "形如 '30 48 -191'"},
                        "pos2": {"type": "string", "description": "形如 '34 50 -191'"},
                        "limit": {"type": "integer", "default": 512}},
         "required": ["pos1", "pos2"]}, SAFE, _t_scan_region))
    reg.register(Tool(
        "looking_at",
        "读准星当前对准的方块或实体（含坐标和距离）。",
        {"type": "object", "properties": {}, "required": []}, SAFE, _t_looking_at))

    reg.register(Tool(
        "screenshot",
        "截一张当前画面存成 PNG，返回文件路径与尺寸（客户端本地抓帧，不需要 OP，不含 HUD）。"
        "用途是留档给玩家看，或给接了视觉的模型看。"
        "注意：纯文字模型看不到图片内容，不要用它判断「砌得对不对」，那要用 scan_region/block_at。",
        {"type": "object",
         "properties": {"max_width": {"type": "integer", "default": 854,
                                      "description": "输出图片最大宽度，等比缩放"}},
         "required": []}, SAFE, _t_screenshot))

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
        "goto",
        "让 Baritone 寻路走到指定坐标（会绕障碍、跳坑）。只负责走，不改方块。"
        "异步：返回后它还在走，要确认到位就再调 get_state 看坐标。",
        {"type": "object",
         "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                        "z": {"type": "number"}},
         "required": ["x", "y", "z"]}, SAFE, _t_goto))
    reg.register(Tool(
        "goto_stop", "让 Baritone 立刻停下（停走 / 停跟随）。",
        {"type": "object", "properties": {}, "required": []}, SAFE, _t_goto_stop))
    reg.register(Tool(
        "follow", "让 Baritone 跟随指定玩家（用 goto_stop 停）。",
        {"type": "object",
         "properties": {"player": {"type": "string"}},
         "required": ["player"]}, SAFE, _t_follow))

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
