"""编排层单测：图走向、自检闭环、重试上限、行动预算、失败止损、可观测。"""
from __future__ import annotations

import unittest

from mcrobot.brain.agent_loop import AgentLoop, AgentState, next_node
from mcrobot.tools.registry import CallContext, Registry, Tool, ToolResult


def _reg(*names: str) -> Registry:
    r = Registry()
    for n in names:
        r.register(Tool(n, f"工具 {n}", {"type": "object", "properties": {}},
                        "safe", (lambda nn: (lambda a, c: ToolResult(True, nn, "", text="ok")))(n)))
    return r


class GraphTest(unittest.TestCase):
    def test_linear_path(self):
        s = AgentState(goal="g")
        self.assertEqual(next_node(s), "perceive")
        s.log("perceive")
        self.assertEqual(next_node(s), "decide")
        s.log("decide")
        s.plan = [{"name": "t"}]
        self.assertEqual(next_node(s), "act")
        s.log("act")
        self.assertEqual(next_node(s), "verify")
        s.log("verify")
        s.verdict = "ok"
        self.assertIsNone(next_node(s))          # END

    def test_retry_until_max_rounds(self):
        s = AgentState(goal="g", max_rounds=2)
        s.log("verify"); s.verdict = "fail"; s.round = 1
        self.assertEqual(next_node(s), "decide")   # 还有余量 → 重试
        s.round = 2
        self.assertIsNone(next_node(s))            # 用尽 → 结束

    def test_no_plan_goes_straight_to_verify(self):
        s = AgentState(goal="g")
        s.log("decide")                             # 没计划出动作
        self.assertEqual(next_node(s), "verify")

    def test_abort_ends(self):
        s = AgentState(goal="g", aborted="预算用尽")
        self.assertIsNone(next_node(s))


class LoopTest(unittest.TestCase):
    def test_verify_ok_first_round(self):
        loop = AgentLoop(registry=_reg("say"),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=lambda s: (True, ""))
        s = loop.run("你好")
        self.assertEqual(s.verdict, "ok")
        self.assertEqual(s.round, 1)
        self.assertEqual([t["node"] for t in s.trace],
                         ["perceive", "decide", "act", "verify"])

    def test_receipts_carry_action_id(self):
        loop = AgentLoop(registry=_reg("say"),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=lambda s: (True, ""))
        s = loop.run("你好")
        self.assertTrue(s.receipts)
        self.assertTrue(all(len(r.action_id) == 8 for r in s.receipts))

    def test_retry_then_succeed(self):
        calls = {"n": 0}

        def verify(s):
            calls["n"] += 1
            return (calls["n"] >= 3, "还没成" if calls["n"] < 3 else "")

        loop = AgentLoop(registry=_reg("say"),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=verify, max_rounds=4)
        s = loop.run("盖个房子")
        self.assertEqual(s.verdict, "ok")
        self.assertEqual(s.round, 3)      # 第 3 轮判成
        self.assertEqual(calls["n"], 3)

    def test_gives_up_after_max_rounds(self):
        loop = AgentLoop(registry=_reg("say"),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=lambda s: (False, "一直不成"), max_rounds=2)
        s = loop.run("盖个房子")
        self.assertEqual(s.verdict, "fail")
        self.assertEqual(s.round, 2)      # 不空转：到 2 轮就停
        self.assertIn("一直不成", s.critique)

    def test_action_budget_guard(self):
        loop = AgentLoop(registry=_reg("say"),
                         decide=lambda s: [{"name": "say", "arguments": {}}] * 10,
                         verify=lambda s: (True, ""), max_rounds=4,
                         action_budget=3)
        s = loop.run("刷屏测试")
        self.assertEqual(s.tool_calls, 3)
        self.assertIn("行动预算用尽", s.aborted)

    def test_stops_on_first_failure(self):
        reg = _reg("say")
        reg.register(Tool("boom", "会失败", {"type": "object", "properties": {}},
                          "safe", lambda a, c: ToolResult(False, "boom", "", error="炸了")))
        loop = AgentLoop(registry=reg,
                         decide=lambda s: [{"name": "say", "arguments": {}},
                                           {"name": "boom", "arguments": {}},
                                           {"name": "say", "arguments": {}}],
                         verify=lambda s: (False, "有失败"), max_rounds=1,
                         lessons_enabled=False)
        s = loop.run("试试")
        self.assertEqual([r.tool for r in s.receipts], ["say", "boom"])

    def test_dangerous_refused_for_stranger(self):
        reg = Registry()
        reg.register(Tool("danger", "危险", {"type": "object", "properties": {}},
                          "dangerous",
                          lambda a, c: ToolResult(True, "danger", "", text="做了")))
        loop = AgentLoop(registry=reg, tools_for_observation=(),
                         decide=lambda s: [{"name": "danger", "arguments": {}}],
                         verify=lambda s: (True, ""))
        s = loop.run("拆掉它", actor="stranger", dangerous_allowed=("oahnuy198",))
        self.assertFalse(s.receipts[0].ok)
        self.assertIn("未授权", s.receipts[0].error)

    def test_state_is_serializable(self):
        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=lambda s: (True, ""))
        d = loop.run("你好").to_dict()
        self.assertEqual(d["verdict"], "ok")
        self.assertIsInstance(d["trace"], list)
        self.assertIsInstance(d["receipts"], list)


class FallbackTest(unittest.TestCase):
    def test_fallback_decide_chat(self):
        loop = AgentLoop(registry=_reg("say"), tools_for_observation=())
        s = loop.run("你好呀")
        self.assertEqual(s.receipts[0].tool, "say")

    def test_fallback_decide_command(self):
        loop = AgentLoop(registry=_reg("run_command"), tools_for_observation=())
        s = loop.run("/list")
        self.assertEqual(s.receipts[0].tool, "run_command")


class LessonTest(unittest.TestCase):
    """mcagent 的反思：失败要沉淀成知识库里的「教训」，下次才不用重踩。"""

    def _loop(self, **kw):
        return AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=lambda s: [{"name": "say", "arguments": {}}], **kw)

    def test_failure_writes_lesson_with_task_id_as_source(self):
        from unittest import mock
        with mock.patch("mcrobot.knowledge.remember") as mem:
            s = self._loop(verify=lambda s: (False, "右墙少砌了两格"),
                           max_rounds=1).run("砌墙", task_id="t-1")
        self.assertEqual(s.verdict, "fail")
        mem.assert_called_once()
        topic, text = mem.call_args[0][0], mem.call_args[0][1]
        self.assertEqual(topic, "教训")
        self.assertIn("少砌了两格", text)
        self.assertIn("t-1", mem.call_args[1]["source"])   # 出处 = 这次闭环，不是模型瞎编

    def test_success_writes_no_lesson(self):
        from unittest import mock
        with mock.patch("mcrobot.knowledge.remember") as mem:
            self._loop(verify=lambda s: (True, "")).run("砌墙", task_id="t-2")
        mem.assert_not_called()      # 做成的路归技能库，不重复记

    def test_lesson_failure_does_not_break_task(self):
        from unittest import mock

        def boom(*a, **kw):
            raise OSError("磁盘满了")
        with mock.patch("mcrobot.knowledge.remember", boom):
            s = self._loop(verify=lambda s: (False, "差一点"), max_rounds=1).run("砌墙")
        self.assertEqual(s.verdict, "fail")     # 记忆挂了不影响结论
        self.assertIn("差一点", s.critique)

    def test_no_critique_writes_nothing(self):
        from unittest import mock
        with mock.patch("mcrobot.knowledge.remember") as mem:
            self._loop(verify=lambda s: (False, ""), max_rounds=1).run("砌墙")
        mem.assert_not_called()      # 什么都没说就别记空账


if __name__ == "__main__":
    unittest.main(verbosity=2)
