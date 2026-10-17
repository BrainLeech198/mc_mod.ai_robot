"""工具层单测：JSON Schema 形态、权限门控、行动预算、回执关联。"""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from mcrobot.brain import protocol as br
from mcrobot.tools import builtin, registry
from mcrobot.tools.registry import CallContext, Registry, Tool, ToolResult


class _Ctx:
    """把 protocol 的文件路径指到临时目录，避免测试污染真实 inbox/outbox。"""

    def __enter__(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self._old = (br.INBOX, br.OUTBOX, br.PTR)
        br.INBOX = td / "inbox.jsonl"
        br.OUTBOX = td / "outbox.jsonl"
        br.PTR = td / "inbox.ptr"
        self.dir = td
        return self

    def __exit__(self, *a):
        br.INBOX, br.OUTBOX, br.PTR = self._old
        self._td.cleanup()

    def outbox(self) -> list[dict]:
        if not br.OUTBOX.exists():
            return []
        return [json.loads(l) for l in br.OUTBOX.read_text(encoding="utf-8").splitlines()]


class SpecTest(unittest.TestCase):
    def setUp(self):
        self.reg = builtin.build_registry()

    def test_names_are_snake_case(self):
        for n in self.reg.names():
            self.assertRegex(n, r"^[a-z][a-z0-9_]*$", n)

    def test_mcp_spec_shape(self):
        for s in self.reg.mcp_specs():
            self.assertEqual(set(s), {"name", "description", "inputSchema"}, s["name"])
            self.assertEqual(s["inputSchema"].get("type"), "object")
            self.assertIn("properties", s["inputSchema"])

    def test_openai_spec_shape(self):
        for s in self.reg.openai_specs():
            self.assertEqual(s["type"], "function")
            self.assertEqual(set(s["function"]), {"name", "description", "parameters"})

    def test_every_tool_has_a_handler(self):
        for n in self.reg.names():
            self.assertIsNotNone(self.reg.tool(n).fn, n)


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.reg = builtin.build_registry()

    def test_dangerous_refused_for_stranger(self):
        ctx = CallContext(actor="stranger", dry=True,
                          dangerous_allowed=("oahnuy198",))
        r = self.reg.call("worldedit_set", {"block": "stone"}, ctx)
        self.assertFalse(r.ok)
        self.assertIn("未授权", r.error)

    def test_dangerous_allowed_for_owner(self):
        ctx = CallContext(actor="oahnuy198", dry=True,
                          dangerous_allowed=("oahnuy198",))
        r = self.reg.call("worldedit_set", {"block": "stone"}, ctx)
        self.assertTrue(r.ok, r.error)
        self.assertIn("//set stone", r.text)

    def test_unsafe_command_refused(self):
        ctx = CallContext(actor="stranger", dry=True, dangerous_allowed=())
        r = self.reg.call("run_command", {"command": "/op stranger"}, ctx)
        self.assertFalse(r.ok)
        self.assertIn("未授权", r.error)

    def test_readonly_command_passes(self):
        ctx = CallContext(actor="stranger", dry=True, dangerous_allowed=())
        r = self.reg.call("run_command", {"command": "/list"}, ctx)
        self.assertTrue(r.ok, r.error)

    def test_unknown_tool(self):
        r = self.reg.call("nope", {}, CallContext())
        self.assertFalse(r.ok)
        self.assertIn("未知工具", r.error)


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.reg = builtin.build_registry()

    def test_budget_guard(self):
        ctx = CallContext(actor="stranger", dry=True, dangerous_allowed=())
        r = self.reg.call("run_batch", {"commands": ["/list"] * 50}, ctx)
        self.assertFalse(r.ok)
        self.assertIn("行动预算", r.error)

    def test_stops_on_first_failure(self):
        ctx = CallContext(actor="stranger", dry=True, dangerous_allowed=())
        r = self.reg.call("run_batch", {"commands": ["/list", "/op me", "/list"]}, ctx)
        results = r.data["results"]
        self.assertEqual(len(results), 2)          # 第二条被拒即停，不再跑第三条
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])

    def test_all_ok(self):
        ctx = CallContext(actor="stranger", dry=True, dangerous_allowed=())
        r = self.reg.call("run_batch", {"commands": ["/list", "/data get entity @s Pos"]}, ctx)
        self.assertTrue(r.ok)
        self.assertEqual(len(r.data["results"]), 2)


class SayAndLookupTest(unittest.TestCase):
    def test_say_writes_outbox_with_action_id(self):
        with _Ctx() as c:
            r = builtin.REGISTRY.call("say", {"text": "你好"},
                                      CallContext(actor="oahnuy198"))
            self.assertTrue(r.ok)
            self.assertEqual(len(r.action_id), 8)
            out = c.outbox()
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["say"], "你好")
            self.assertIn("ts", out[0])

    def test_block_lookup_real_catalog(self):
        r = builtin.REGISTRY.call("block_lookup", {"query": "desk", "limit": 5},
                                  CallContext())
        self.assertTrue(r.ok, r.error)
        self.assertTrue(r.data["hits"])
        self.assertIn(":", r.data["hits"][0]["id"])   # 形如 modid:name

    def test_block_lookup_miss_is_explained(self):
        r = builtin.REGISTRY.call("block_lookup", {"query": "绝不可能的方块名xyz"},
                                  CallContext())
        self.assertTrue(r.ok)
        self.assertIn("英文", r.text)


class BaritoneTest(unittest.TestCase):
    """Baritone 是聊天前缀（#goto），不是原版命令（/goto）——走错通道就是静默无效。"""

    def setUp(self):
        self.reg = builtin.build_registry()

    def test_goto_uses_chat_prefix(self):
        with mock.patch.object(builtin.cc, "chat",
                               return_value={"ok": True}) as m:
            r = self.reg.call("goto", {"x": 27, "y": -56, "z": -191}, CallContext())
        self.assertTrue(r.ok, r.error)
        m.assert_called_once_with("#goto 27 -56 -191")

    def test_goto_rejects_missing_coords(self):
        r = self.reg.call("goto", {"x": 1}, CallContext())
        self.assertFalse(r.ok)
        self.assertIn("数字", r.error)

    def test_stop_and_follow(self):
        with mock.patch.object(builtin.cc, "chat",
                               return_value={"ok": True}) as m:
            self.reg.call("goto_stop", {}, CallContext())
            self.reg.call("follow", {"player": "oahnuy198"}, CallContext())
        self.assertEqual([c.args[0] for c in m.call_args_list], ["#stop", "#follow oahnuy198"])


class ScreenshotTest(unittest.TestCase):
    """截图是原始字节（不是 JSON），工具层只负责落盘和把路径说清楚。"""

    def setUp(self):
        self.reg = builtin.build_registry()

    def test_screenshot_returns_saved_path(self):
        shot = {"path": "instance/robot/shots/x.png", "abs_path": "/tmp/x.png",
                "width": 854, "height": 480, "bytes": 4096, "capture_ms": 12}
        with mock.patch.object(builtin.cc, "screenshot", return_value=shot) as m:
            r = self.reg.call("screenshot", {"max_width": 640}, CallContext())
        self.assertTrue(r.ok, r.error)
        m.assert_called_once_with(max_width=640)
        self.assertIn("854×480", r.text)
        self.assertEqual(r.data["path"], "instance/robot/shots/x.png")

    def test_screenshot_error_becomes_receipt(self):
        with mock.patch.object(builtin.cc, "screenshot",
                               side_effect=builtin.cc.ControlError("HTTP 504: capture timeout")):
            r = self.reg.call("screenshot", {}, CallContext())
        self.assertFalse(r.ok)
        self.assertIn("504", r.error)


class ResultTest(unittest.TestCase):
    def test_exception_becomes_receipt(self):
        def boom(_a, _c):
            raise RuntimeError("炸了")

        reg = Registry()
        reg.register(Tool("boom", "会炸", {"type": "object", "properties": {}},
                          registry.SAFE, boom))
        r = reg.call("boom", {}, CallContext())
        self.assertFalse(r.ok)
        self.assertIn("RuntimeError", r.error)

    def test_result_to_dict(self):
        d = ToolResult(True, "t", "abc", text="ok").to_dict()
        self.assertEqual(d["tool"], "t")
        self.assertTrue(d["ok"])


class BlockProbeValidationTest(unittest.TestCase):
    """block_probe 坐标校验（2026-10-17 补墙事故的修复）。

    模型曾喂进「50-54 y48 z-191」这种区间/带字母的畸形坐标，
    /execute if block 静默报错 → 回执变成「命中数偏低」→ 模型信以为真
    去「修」本来没坏的地方。畸形坐标必须在敲命令之前被拒。
    """

    def _call(self, pts, block="cobblestone"):
        with mock.patch.object(builtin.cc, "cmd") as m_cmd:
            r = builtin.REGISTRY.call("block_probe",
                                      {"points": pts, "block": block},
                                      CallContext())
        return r, m_cmd

    def test_valid_point_unit(self):
        self.assertTrue(builtin._valid_point("52 49 -191"))
        self.assertTrue(builtin._valid_point("~ ~1 ~"))       # 相对坐标
        self.assertTrue(builtin._valid_point("-35.5 -56 -78"))
        # 逗号写法由 _t_block_probe 先归一化成空格再进这里，所以这里按未归一试
        self.assertFalse(builtin._valid_point("52,49,-191"))
        self.assertFalse(builtin._valid_point("50-54 y48 z-191"))
        self.assertFalse(builtin._valid_point("50 y49 z-191"))
        self.assertFalse(builtin._valid_point("52 49"))       # 少一个数
        self.assertFalse(builtin._valid_point("52 49 -191 7"))  # 多一个数
        self.assertFalse(builtin._valid_point("abc 49 -191"))

    def test_range_coords_rejected_before_any_command(self):
        r, m_cmd = self._call(["50-54 y48 z-191"])
        self.assertFalse(r.ok)
        self.assertIn("坐标格式不对", r.error)
        self.assertIn("50-54", r.error)
        m_cmd.assert_not_called()

    def test_letter_coords_rejected(self):
        r, m_cmd = self._call(["50 y49 z-191"])
        self.assertFalse(r.ok)
        self.assertIn("坐标格式不对", r.error)
        m_cmd.assert_not_called()

    def test_mixed_batch_rejected_wholesale(self):
        r, m_cmd = self._call(["52 49 -191", "50-54 y48 z-191"])
        self.assertFalse(r.ok)
        self.assertIn("坐标格式不对", r.error)
        m_cmd.assert_not_called()  # 一个坏点都不敲，别制造误导性计数

    def test_good_coords_flow_to_probe(self):
        with mock.patch.object(builtin, "_probe_points",
                               return_value={"total": 1, "hit": 1,
                                             "matched": ["52 49 -191"],
                                             "missed": [],
                                             "chat_seen": []}) as m_probe:
            r, _ = self._call(["52,49,-191"])
        self.assertTrue(r.ok)
        m_probe.assert_called_once()
        self.assertEqual(m_probe.call_args[0][0], ["52 49 -191"])  # 逗号已归一化


if __name__ == "__main__":
    unittest.main(verbosity=2)
