"""MCP 风格工具注册表。

为什么要有这一层：
- 模型侧不再靠 `[[cmd]]` 这类自造文本标记发指令，而是走**标准工具调用**
  （OpenAI `tools` / MCP `tools`）的 JSON Schema，任何支持 function calling
  或 MCP 的客户端都能直接驱动机器人。
- 权限门控收在一处：只读工具放行，危险工具只认白名单玩家。
- 每次调用返回**结构化回执**并带 `action_id`，便于把「指令 → 结果」关联起来
  （抄 MC-AI-Agent-Player 的 action_id 设计），也是 Voyager 式自检闭环的输入。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

SAFE = "safe"
DANGEROUS = "dangerous"


@dataclass
class CallContext:
    """一次工具调用的上下文。"""

    actor: str = ""                       # 谁下达的：玩家名 / "console" / "mcp"
    dry: bool = False                     # 只打印不真做
    dangerous_allowed: tuple[str, ...] = ()  # 危险操作白名单（来自 robot.json）
    version: str | None = None


@dataclass
class ToolResult:
    """统一的回执。给模型看 `text`，给代码看 `data`。"""

    ok: bool
    tool: str
    action_id: str
    text: str = ""
    error: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "tool": self.tool, "action_id": self.action_id,
                "text": self.text, "error": self.error, "data": self.data}


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    risk: str = SAFE
    fn: Callable[[dict, CallContext], ToolResult] | None = None

    def mcp_spec(self) -> dict:
        """MCP `tools/list` 的格式。"""
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}

    def openai_spec(self) -> dict:
        """OpenAI function calling 的格式。"""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.input_schema}}


class Refused(Exception):
    """危险操作未获授权。"""


class Registry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"未知工具 {name}；可用：{', '.join(sorted(self._tools))}")

    def names(self) -> list[str]:
        return sorted(self._tools)

    def mcp_specs(self) -> list[dict]:
        return [t.mcp_spec() for t in (self._tools[n] for n in self.names())]

    def openai_specs(self) -> list[dict]:
        return [t.openai_spec() for t in (self._tools[n] for n in self.names())]

    def call(self, name: str, args: dict | None, ctx: CallContext) -> ToolResult:
        """执行一次工具调用，异常一律转成回执（不让模型把链路搞崩）。"""
        aid = uuid.uuid4().hex[:8]
        args = args or {}
        try:
            t = self.tool(name)
        except KeyError as e:
            return ToolResult(False, name, aid, error=str(e))
        if t.risk == DANGEROUS and ctx.actor not in ctx.dangerous_allowed:
            return ToolResult(False, name, aid,
                              error="未授权：该操作会改世界/动权限，只有服主白名单里的人能下")
        if t.fn is None:
            return ToolResult(False, name, aid, error="工具未实现")
        try:
            r = t.fn(args, ctx)
        except Refused as e:
            return ToolResult(False, name, aid, error=str(e))
        except Exception as e:
            return ToolResult(False, name, aid,
                              error=f"{type(e).__name__}: {str(e)[:300]}")
        r.action_id = r.action_id or aid
        return r


def dangerous_allowed(version: str | None = None) -> tuple[str, ...]:
    """从版本文件夹的 robot.json 读危险操作白名单。"""
    from ..paths import robot_json

    try:
        cfg = json.loads(robot_json(version).read_text(encoding="utf-8"))
    except Exception:
        return ()
    return tuple(cfg.get("auth", {}).get("dangerous_allowed", ()) or ())
