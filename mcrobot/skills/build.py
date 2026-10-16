#!/usr/bin/env python3
"""预置建造技能：参数化的 WorldEdit 模板。

为什么先做这几个：机器人的活儿八成是「圈一块地方 → 填/砌/换」，这几条覆盖了
大部分重复劳动；学会了别的，由 library.learn_from_actions 自动入库。

每个技能就是一串**带占位符的工具调用**：{pos1} {pos2} {block} 在 render 时替换。
装进版本文件夹：`python -m mcrobot.skills.library install`
改模板直接改这里再 install --force（或直接手改 version/<版本>/skills.jsonl）。
"""
from __future__ import annotations

from .library import Skill

BRICK = "stone_bricks"


def _select(pos1: str = "{pos1}", pos2: str = "{pos2}") -> dict:
    return {"tool": "worldedit_select", "arguments": {"pos1": pos1, "pos2": pos2}}


def _we(sub: str, **args: str) -> dict:
    return {"tool": f"worldedit_{sub}", "arguments": dict(args)}


def preset_skills() -> list[Skill]:
    return [
        Skill(name="build_wall",
              description="在两个对角点之间砌一圈墙（选区边界）。",
              params={"pos1": "", "pos2": "", "block": BRICK},
              tags=["墙", "砌墙", "围墙", "wall"],
              source="preset",
              steps=[_select(), _we("walls", block="{block}")]),
        Skill(name="build_room",
              description="把选区做成空心房间（墙+地板+天花板）。",
              params={"pos1": "", "pos2": "", "block": BRICK},
              tags=["房间", "空心", "房子", "room", "house"],
              source="preset",
              steps=[_select(), _we("hollow", block="{block}")]),
        Skill(name="build_platform",
              description="铺一块实心平台/地板。",
              params={"pos1": "", "pos2": "", "block": BRICK},
              tags=["平台", "地板", "铺地", "platform", "floor"],
              source="preset",
              steps=[_select(), _we("set", block="{block}")]),
        Skill(name="replace_blocks",
              description="把选区里某种方块换成另一种（比如 dirt → grass_block）。",
              params={"pos1": "", "pos2": "", "block": "dirt", "to": "grass_block"},
              tags=["替换", "换方块", "replace"],
              source="preset",
              steps=[_select(), _we("replace", block="{block}", to="{to}")]),
        Skill(name="clear_area",
              description="清空一块区域（填成空气）。注意：不可撤回之外的破坏。",
              params={"pos1": "", "pos2": ""},
              tags=["清空", "夷平", "拆", "clear"],
              source="preset",
              steps=[_select(), _we("set", block="air")]),
        Skill(name="undo_last",
              description="撤回上一次 WorldEdit 操作（受撤回纪律约束）。",
              params={},
              tags=["撤回", "后悔", "undo"],
              source="preset",
              steps=[{"tool": "worldedit_undo", "arguments": {}}]),
    ]
