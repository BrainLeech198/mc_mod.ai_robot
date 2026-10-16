"""MCP server：stdlib 实现 JSON-RPC 2.0 over stdio，不装 SDK。

为什么：MCP 已经是工具层的事实标准（2025-12 捐给 Linux 基金会）。做成 MCP server
后，**任何 MCP 客户端**（Claude Desktop、CodeBuddy、各类 IDE）都能直接指挥机器人，
不用为每种客户端各写一遍适配——这就是「通用入口」。

协议要点（抄官方规范）：
- 传输 stdio：一行一条 JSON-RPC 消息，日志只能走 stderr（stdout 被协议占用）
- 方法：`initialize` / `tools/list` / `tools/call`，外加 `notifications/*` 与 `ping`
- `tools/call` 返回 `content:[{type:"text",text:...}]` + `isError`

用法（在客户端里配成一个 stdio MCP server）：
    python -m mcrobot.tools.mcp_server

调试（手动喂一行）：
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -m mcrobot.tools.mcp_server
"""
from __future__ import annotations

import json
import os
import sys

from .builtin import build_registry
from .registry import CallContext, dangerous_allowed

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "mcrobot", "version": "0.2.0"}

# MCP 客户端没有游戏内身份，默认以 "mcp" 身份调用 → 危险操作会被白名单挡下。
# 需要完整权限时把 MCROBOT_MCP_ACTOR 设成白名单里的玩家名（如 oahnuy198）。
def _actor() -> str:
    return os.environ.get("MCROBOT_MCP_ACTOR", "mcp")


def _log(msg: str):
    print(f"[mcp] {msg}", file=sys.stderr, flush=True)


def _result(req: dict, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req.get("id"), "result": payload}


def _error(req: dict, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req.get("id"),
            "error": {"code": code, "message": message}}


def handle(req: dict, reg) -> dict | None:
    """处理一条请求；通知类（无 id）返回 None 表示不回包。"""
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        return _result(req, {"protocolVersion": PROTOCOL_VERSION,
                             "capabilities": {"tools": {}},
                             "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(req, {})
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return _result(req, {"tools": reg.mcp_specs()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        ctx = CallContext(actor=_actor(),
                          dangerous_allowed=dangerous_allowed())
        r = reg.call(name, args, ctx)
        text = r.text or r.error or ""
        if not r.ok:
            text = f"[失败] {r.error}"
        return _result(req, {"content": [{"type": "text", "text": text}],
                             "isError": not r.ok,
                             "structuredContent": r.to_dict()})
    if req.get("id") is None:
        return None  # 未知通知，吞掉
    return _error(req, -32601, f"不支持的方法：{method}")


def main(argv=None) -> int:
    reg = build_registry()
    _log(f"mcrobot MCP server 就绪；工具 {len(reg.names())} 个；actor={_actor()}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            print(json.dumps(_error({}, -32700, f"解析失败：{e}")), flush=True)
            continue
        try:
            resp = handle(req, reg)
        except Exception as e:  # 协议层绝不能崩
            resp = _error(req, -32603, f"{type(e).__name__}: {str(e)[:200]}")
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
