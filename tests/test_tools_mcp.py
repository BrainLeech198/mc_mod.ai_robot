"""MCP server 协议层单测（不启进程，直接测 handle）。"""
from __future__ import annotations

import json
import unittest

from mcrobot.tools import builtin, mcp_server


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.reg = builtin.build_registry()

    def test_initialize(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {}}, self.reg)
        self.assertEqual(r["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        self.assertEqual(r["result"]["serverInfo"]["name"], "mcrobot")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_tools_list_matches_registry(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.reg)
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(sorted(names), self.reg.names())

    def test_tools_call_ok(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "block_lookup",
                                          "arguments": {"query": "chair", "limit": 2}}},
                              self.reg)
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(r["result"]["content"][0]["type"], "text")
        self.assertIn(":", r["result"]["content"][0]["text"])
        self.assertTrue(r["result"]["structuredContent"]["ok"])

    def test_tools_call_refused_when_unauthorized(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "worldedit_set",
                                          "arguments": {"block": "stone"}}}, self.reg)
        self.assertTrue(r["result"]["isError"])          # MCP 默认 actor=mcp，不在白名单
        self.assertIn("未授权", r["result"]["content"][0]["text"])

    def test_unknown_method_errors(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"}, self.reg)
        self.assertEqual(r["error"]["code"], -32601)

    def test_notification_returns_none(self):
        self.assertIsNone(mcp_server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.reg))

    def test_ping(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"}, self.reg)
        self.assertEqual(r["result"], {})

    def test_output_is_one_json_per_line(self):
        """stdout 被协议占用：一条消息必须是单行 JSON（不能含裸换行）。"""
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "block_lookup",
                                          "arguments": {"query": "lamp", "limit": 3}}},
                              self.reg)
        line = json.dumps(r, ensure_ascii=False)
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
