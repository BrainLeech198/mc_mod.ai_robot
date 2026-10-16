"""LLM 策略（decide/verify）单测：用假 ask，不碰网络。"""
from __future__ import annotations

import unittest

from mcrobot.brain.agent_loop import AgentLoop
from mcrobot.brain.llm_policy import (make_decide, make_verify,
                                      _extract_json_array, _extract_json_object)
from mcrobot.tools.builtin import build_registry


class ParseTest(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(_extract_json_array('[{"name":"say"}]'), [{"name": "say"}])

    def test_fenced_array(self):
        raw = '好的：\n```json\n[{"name":"say","arguments":{"text":"hi"}}]\n```'
        self.assertEqual(_extract_json_array(raw),
                         [{"name": "say", "arguments": {"text": "hi"}}])

    def test_array_with_surrounding_text(self):
        raw = '我打算这么做：\n[{"name":"get_state"}]\n希望可以。'
        self.assertEqual(_extract_json_array(raw), [{"name": "get_state"}])

    def test_garbage_returns_none(self):
        self.assertIsNone(_extract_json_array("我没想好"))
        self.assertIsNone(_extract_json_array(""))

    def test_object(self):
        self.assertEqual(_extract_json_object('{"ok": false, "critique": "没建"}'),
                         {"ok": False, "critique": "没建"})

    def test_object_fenced(self):
        self.assertEqual(_extract_json_object('```json\n{"ok":true}\n```'), {"ok": True})


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry()
        self.seen: list[list[dict]] = []

    def _ask(self, reply: str):
        def ask(msgs):
            self.seen.append(msgs)
            return reply
        return ask

    def test_decide_parses_tool_calls(self):
        decide = make_decide(self._ask('[{"name":"say","arguments":{"text":"好"}}]'), self.reg)
        calls = decide(_FakeState())
        self.assertEqual(calls, [{"name": "say", "arguments": {"text": "好"}}])

    def test_decide_garbage_yields_empty_plan(self):
        decide = make_decide(self._ask("我不知道"), self.reg)
        self.assertEqual(decide(_FakeState()), [])

    def test_decide_drops_malformed_items(self):
        decide = make_decide(self._ask('[{"name":"say"},{"nope":1},"x"]'), self.reg)
        self.assertEqual(decide(_FakeState()), [{"name": "say", "arguments": {}}])

    def test_decide_prompt_includes_goal_and_tools(self):
        decide = make_decide(self._ask("[]"), self.reg)
        decide(_FakeState(goal="盖个房子"))
        body = self.seen[0][-1]["content"]
        self.assertIn("盖个房子", body)
        self.assertIn("say", body)

    def test_verify_ok(self):
        verify = make_verify(self._ask('{"ok": true}'))
        ok, critique = verify(_FakeState(receipts=[_r(True)]))
        self.assertTrue(ok)
        self.assertEqual(critique, "")

    def test_verify_fail_with_critique(self):
        verify = make_verify(self._ask('{"ok": false, "critique": "墙没封顶"}'))
        ok, critique = verify(_FakeState(receipts=[_r(False)]))
        self.assertFalse(ok)
        self.assertIn("墙没封顶", critique)

    def test_verify_garbage_falls_back_to_receipts(self):
        verify = make_verify(self._ask("说不清楚"))
        ok, _ = verify(_FakeState(receipts=[_r(True), _r(False)]))
        self.assertFalse(ok)          # 有失败回执 → 硬判失败

    def test_verify_no_receipts_is_ok(self):
        verify = make_verify(self._ask("unused"))
        self.assertTrue(verify(_FakeState())[0])


class ClosedLoopTest(unittest.TestCase):
    """decide → act → verify → critique → decide ... 真跑一遍。"""

    def test_critic_drives_a_second_round(self):
        seq = [
            '[{"name":"run_batch","arguments":{"commands":["//set stone"]}}]',
            '{"ok": false, "critique": "只铺了地面，墙没起来"}',
            '[{"name":"run_batch","arguments":{"commands":["//walls stone"]}}]',
            '{"ok": true}',
        ]
        it = iter(seq)
        loop = AgentLoop(registry=build_registry(), tools_for_observation=(),
                         decide=make_decide(lambda m: next(it), build_registry()),
                         verify=make_verify(lambda m: next(it)), max_rounds=4)
        s = loop.run("盖个小屋", actor="oahnuy198", dry=True,
                     dangerous_allowed=("oahnuy198",))
        self.assertEqual(s.verdict, "ok")
        self.assertEqual(s.round, 2)
        self.assertIn("//walls", s.receipts[-1].data["results"][0]["command"])

    def test_gives_up_and_reports_critique(self):
        def ask(m):
            if m[0]["content"].startswith("你是 Minecraft 里一个真玩家 AI 的自检裁判"):
                return '{"ok": false, "critique": "一直建不成"}'
            return '[{"name":"say","arguments":{"text":"再试"}}]'
        loop = AgentLoop(registry=build_registry(), tools_for_observation=(),
                         decide=make_decide(ask, build_registry()),
                         verify=make_verify(ask), max_rounds=2)
        s = loop.run("盖个城堡", dry=True)
        self.assertEqual(s.verdict, "fail")
        self.assertEqual(s.round, 2)
        self.assertIn("一直建不成", s.critique)


def _r(ok: bool):
    from mcrobot.tools.registry import ToolResult
    return ToolResult(ok, "t", "id", text="ok" if ok else "", error="" if ok else "炸了")


class _FakeState:
    def __init__(self, goal="目标", receipts=None, actor="oahnuy198"):
        self.goal = goal
        self.receipts = receipts or []
        self.actor = actor
        self.observation = {}
        self.critique = ""
        self.round = 1
        self.max_rounds = 4
        self.dangerous_allowed = ()

    def log(self, *a, **k):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
