"""断点续跑单测：状态落盘/恢复、resume 标记不打断图、续跑不重跑已完成的节点。"""
from __future__ import annotations

import os
import tempfile
import unittest

from mcrobot.brain.agent_loop import AgentLoop, AgentState, next_node
from mcrobot.brain.checkpoint import Checkpointer
from mcrobot.tools.registry import Tool, ToolResult


def _reg(*names: str):
    from mcrobot.tools.registry import Registry
    r = Registry()
    for n in names:
        r.register(Tool(n, f"工具 {n}", {"type": "object", "properties": {}},
                        "safe", (lambda nn: (lambda a, c: ToolResult(True, nn, "", text="ok")))(n)))
    return r


class StateRoundTripTest(unittest.TestCase):
    def test_dict_roundtrip_keeps_everything(self):
        s = AgentState(goal="盖个房子", task_id="t1", actor="oahnuy198", round=2,
                       max_rounds=4, tool_calls=3, verdict="fail", critique="还差墙",
                       actions=[{"name": "run_command", "arguments": {"command": "//set"}}])
        s.receipts.append(ToolResult(True, "run_command", "abcd1234", text="done"))
        s.log("perceive")
        d = s.to_dict()
        back = AgentState.from_dict(d)
        self.assertEqual(back.goal, "盖个房子")
        self.assertEqual(back.task_id, "t1")
        self.assertEqual(back.round, 2)
        self.assertEqual(back.actions, s.actions)
        self.assertEqual(back.receipts[0].action_id, "abcd1234")
        self.assertEqual([t["node"] for t in back.trace], ["perceive"])


class CheckpointerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ck = Checkpointer(self.dir)

    def test_save_load(self):
        s = AgentState(goal="g", task_id="t1", round=1)
        self.ck.save(s)
        back = self.ck.load("t1")
        self.assertIsNotNone(back)
        self.assertEqual(back.goal, "g")
        self.assertEqual(back.round, 1)

    def test_missing_returns_none(self):
        self.assertIsNone(self.ck.load("nope"))
        self.assertIsNone(self.ck.latest())

    def test_ids_and_latest(self):
        self.ck.save(AgentState(goal="a", task_id="t1"))
        self.ck.save(AgentState(goal="b", task_id="t2"))
        self.assertEqual(self.ck.ids(), ["t1", "t2"])
        self.assertEqual(self.ck.latest().goal, "b")


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ck = Checkpointer(self.dir)
        self._old = os.environ.get("MCROBOT_SKILLS_FILE")
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                          encoding="utf-8")
        tmp.close()
        os.environ["MCROBOT_SKILLS_FILE"] = tmp.name
        self._tmp = tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MCROBOT_SKILLS_FILE", None)
        else:
            os.environ["MCROBOT_SKILLS_FILE"] = self._old
        os.unlink(self._tmp)

    def _loop(self, **kw):
        kw.setdefault("registry", _reg("say"))
        kw.setdefault("tools_for_observation", ())
        kw.setdefault("decide", lambda s: [{"name": "say", "arguments": {}}])
        kw.setdefault("verify", lambda s: (True, ""))
        return AgentLoop(**kw)

    def test_resume_continues_instead_of_restarting(self):
        s0 = AgentState(goal="g", task_id="t1")
        s0.log("perceive")
        self.ck.save(s0)
        s = self._loop().run("g", task_id="t1", checkpoint=self.ck)
        nodes = [t["node"] for t in s.trace]
        self.assertEqual(nodes.count("perceive"), 1)      # 不重跑感知
        self.assertEqual(nodes[-1], "verify")
        self.assertEqual(s.verdict, "ok")

    def test_resume_marker_does_not_break_graph(self):
        s = AgentState(goal="g")
        s.log("perceive")
        s.log("resume")
        self.assertEqual(next_node(s), "decide")

    def test_state_is_persisted_each_step(self):
        s = self._loop().run("g", task_id="t2", checkpoint=self.ck)
        saved = self.ck.load("t2")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.verdict, s.verdict)
        self.assertEqual(saved.round, s.round)

    def test_fresh_task_id_starts_new(self):
        self._loop().run("g", task_id="t3", checkpoint=self.ck)
        s = self._loop().run("g", task_id="t4", checkpoint=self.ck)
        self.assertNotIn("resume", [t["node"] for t in s.trace])


if __name__ == "__main__":
    unittest.main(verbosity=2)
