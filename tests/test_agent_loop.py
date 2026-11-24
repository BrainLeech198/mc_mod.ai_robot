"""编排层单测：图走向、自检闭环、重试上限、行动预算、失败止损、可观测。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from mcrobot.brain.agent_loop import (AgentLoop, AgentState, is_verification_goal,
                                      next_node)
from mcrobot.brain.llm_brain import BrainError
from mcrobot.brain.llm_policy import make_decide, make_verify
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


class ReadOnlyVerifyTest(unittest.TestCase):
    """核对轮只读（2026-10-17 补墙事故的硬约束）：验收子目标不许改世界。

    事故原型：验收步「block_probe核对该格确认」失败后，模型下一轮发起
    run_batch 把 6 格好石头覆盖成圆石。现在这类调用必须在 registry 之前被拒。
    """

    def _spy_reg(self):
        """任何工具被真正调用都留痕（用于证明改世界工具没被执行）。"""
        reg, calls = Registry(), []

        def fn(n):
            return lambda a, c: (calls.append(n), ToolResult(True, n, "", text="ok"))[1]

        for n in ("block_probe", "block_at", "run_batch", "run_command", "say"):
            reg.register(Tool(n, f"工具 {n}", {"type": "object", "properties": {}},
                              "safe", fn(n)))
        return reg, calls

    def test_is_verification_goal(self):
        self.assertTrue(is_verification_goal("block_probe核对该格确认"))
        self.assertTrue(is_verification_goal("检查墙上还有没有缺口"))
        self.assertTrue(is_verification_goal("确认补丁在不在原位"))
        # 保守方向：带动作词的一律不锁——「核对并补上」就该正常跑；
        # 疑问式「放没放上」也含动作词，会漏锁，但锁错的代价（挡合法修复）
        # 比漏锁大，认不出来就不锁。
        self.assertFalse(is_verification_goal("找到缺的那格用圆石补上"))
        self.assertFalse(is_verification_goal("核对并补上没砌到的地方"))
        self.assertFalse(is_verification_goal("看看补丁到底放没放上"))
        self.assertFalse(is_verification_goal("砌一面墙"))                  # 无核对词
        self.assertFalse(is_verification_goal(""))

    def test_read_only_round_refuses_world_tools(self):
        reg, calls = self._spy_reg()
        loop = AgentLoop(registry=reg, tools_for_observation=(),
                         decide=lambda s: [{"name": "block_probe", "arguments": {}},
                                           {"name": "run_batch", "arguments": {}}],
                         verify=lambda s: (False, "还没核对"), max_rounds=2,
                         lessons_enabled=False, curriculum_enabled=False)
        s = loop.run("核对该格确认", read_only=True, task_id="t")
        self.assertNotIn("run_batch", calls)          # registry 从没被叫过它
        refused = [r for r in s.receipts if r.tool == "run_batch"]
        self.assertEqual(len(refused), 2)             # 两轮各拒一次
        self.assertFalse(refused[0].ok)
        self.assertIn("核对轮只读", refused[0].error)
        self.assertNotIn("run_batch", [a["name"] for a in s.actions])  # 不配学技能
        self.assertEqual(s.tool_calls, 2)             # 被拒的不烧预算
        self.assertEqual(s.verdict, "fail")

    def test_read_only_allows_reads_and_say(self):
        reg, calls = self._spy_reg()
        loop = AgentLoop(registry=reg, tools_for_observation=(),
                         decide=lambda s: [{"name": "block_probe", "arguments": {}},
                                           {"name": "say", "arguments": {}}],
                         verify=lambda s: (True, ""))
        s = loop.run("核对该格确认", read_only=True)
        self.assertEqual(calls, ["block_probe", "say"])
        self.assertEqual(s.verdict, "ok")

    def test_not_read_only_still_executes_world_tools(self):
        reg, calls = self._spy_reg()
        loop = AgentLoop(registry=reg, tools_for_observation=(),
                         decide=lambda s: [{"name": "run_batch", "arguments": {}}],
                         verify=lambda s: (True, ""))
        loop.run("把缺的那格补上", actor="oahnuy198",
                 dangerous_allowed=("oahnuy198",))
        self.assertEqual(calls, ["run_batch"])        # 非核对目标照常改世界

    def test_plan_marks_verification_subtask_read_only(self):
        seen: dict[str, bool] = {}

        def decide(s):
            seen[s.goal] = s.read_only
            return [{"name": "say", "arguments": {}}]

        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=decide, verify=lambda s: (True, ""),
                         plan_fn=lambda g: ["找到缺的那格用圆石补上",
                                            "block_probe核对该格确认"],
                         lessons_enabled=False, curriculum_enabled=False)
        s = loop.run_plan("补墙")
        self.assertEqual(seen, {"找到缺的那格用圆石补上": False,
                                "block_probe核对该格确认": True})
        self.assertEqual(s.verdict, "ok")

    def test_incident_replay_verify_escalation_is_blocked(self):
        """事故重放：2026-10-17 补墙任务第三步的原样行为，必须被挡住。

        原事故：核对轮里模型被误导性计数带偏，写出「还需对 6 格补放
        cobblestone」的批评，下一轮 run_batch 把 6 格好石头覆盖成圆石。
        现在：子目标是纯核对 → 只读 → run_batch 在 registry 之前被拒，
        世界一块砖都没被碰，计划如实失败停下。
        """
        reg, placed = Registry(), []
        reg.register(Tool("block_probe", "探点", {"type": "object", "properties": {}},
                          "safe", lambda a, c: ToolResult(
                              False, "block_probe", "",
                              text="1/15 个点是 cobblestone")))
        reg.register(Tool("run_batch", "批量指令", {"type": "object", "properties": {}},
                          "safe", lambda a, c: (placed.append(a),
                                                ToolResult(True, "run_batch", "",
                                                           text="done"))[1]))

        def decide(s):
            if s.round >= 2:      # 被批评带偏，想去「修」那 6 格
                return [{"name": "run_batch",
                         "arguments": {"commands": ["/setblock 50 48 -191 cobblestone"]}}]
            return [{"name": "block_probe",
                     "arguments": {"points": ["50-54 y48 z-191"], "block": "cobblestone"}}]

        def verify(s):
            if any(r.tool == "run_batch" and not r.ok for r in s.receipts):
                return False, "想改世界但被只读拒收，如实报失败"
            return False, ("15 个点只 1 个是 cobblestone；还需对 50-54 y48 z-191 "
                           "等 6 格补放 cobblestone 后重新 probe")

        loop = AgentLoop(registry=reg, tools_for_observation=(),
                         decide=decide, verify=verify, max_rounds=3,
                         lessons_enabled=False, curriculum_enabled=False)
        s = loop.run("block_probe核对该格确认", read_only=True, task_id="t3")
        self.assertEqual(placed, [])                 # 一块石头都没被碰
        self.assertEqual(s.verdict, "fail")
        self.assertTrue(any(r.tool == "run_batch" and "核对轮只读" in r.error
                            for r in s.receipts))

    def test_read_only_survives_serialization(self):
        s = AgentState(goal="核对", read_only=True)
        self.assertTrue(AgentState.from_dict(s.to_dict()).read_only)
        d = s.to_dict()
        d["read_only"] = False
        self.assertFalse(AgentState.from_dict(d).read_only)


class InfraOutageTest(unittest.TestCase):
    """考场停电：大脑端点/自检请求传输层故障，不算考生答不出题。

    2026-10-17 真机踩过：端点抖动时 decide 的异常被 _ask_with_retry 吞掉、
    静默退回空计划，闭环判 fail，一次停电被记进 curriculum 能力账
    （还沉淀成「教训」）。现在：异常穿透到 loop → 标 infra → 整单不记账。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cur_file = os.path.join(self.tmp.name, "curriculum.jsonl")

    def _env(self):
        return mock.patch.dict(os.environ,
                               {"MCROBOT_CURRICULUM_FILE": self.cur_file})

    def test_decide_transport_error_marks_infra(self):
        def boom(s):
            raise BrainError("网络错误: [WinError 10061] 目标计算机积极拒绝")

        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=boom, verify=lambda s: (True, ""))
        s = loop.run("补墙")
        self.assertIn("10061", s.infra_error)
        self.assertTrue(s.aborted)
        self.assertIsNone(s.verdict)          # 没判成也没判败——没考成
        self.assertEqual(s.tool_calls, 0)
        self.assertTrue(any(t["node"] == "decide_infra" for t in s.trace))

    def test_verify_transport_error_marks_infra(self):
        def boom(s):
            raise BrainError("HTTP 502")

        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=boom)
        s = loop.run("说句话")
        self.assertIn("502", s.infra_error)
        self.assertTrue(s.aborted)

    def test_infra_skips_lesson_recording(self):
        # 停电不沉淀教训：端点挂了没什么可学的
        from unittest import mock

        def boom(s):
            raise BrainError("网络错误: timeout")

        with mock.patch("mcrobot.knowledge.remember") as mem:
            loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                             decide=boom, verify=lambda s: (True, ""),
                             lessons_enabled=True)
            s = loop.run("补墙")
        self.assertTrue(s.infra_error)
        mem.assert_not_called()

    def test_plan_infra_subtask_skips_rest_and_curriculum(self):
        calls = {"n": 0}

        def decide(s):
            calls["n"] += 1
            if calls["n"] == 1:               # 第一步就撞上端点故障
                raise BrainError("网络错误: [WinError 10061]")
            return [{"name": "say", "arguments": {}}]

        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=decide, verify=lambda s: (True, ""),
                         plan_fn=lambda g: ["找到缺的那格用圆石补上",
                                            "block_probe核对该格确认",
                                            "say 报告"],
                         lessons_enabled=False, curriculum_enabled=True)
        with self._env():
            s = loop.run_plan("补墙")
        self.assertEqual([t["verdict"] for t in s.subtasks],
                         ["infra", "skipped", "skipped"])
        self.assertEqual(s.verdict, "infra")
        self.assertIn("10061", s.critique)
        self.assertIn("基础设施故障", s.aborted)
        self.assertEqual(calls["n"], 1)       # 停电后不继续考后面的
        self.assertFalse(os.path.exists(self.cur_file))   # 账上一片空白

    def test_plan_genuine_fail_still_records(self):
        # 对照：真失败照记——闸只放行停电，不放水
        def boom(s):
            raise BrainError("网络错误: timeout")

        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=boom, verify=lambda s: (True, ""),
                         plan_fn=lambda g: ["找到缺的那格用圆石补上"],
                         lessons_enabled=False, curriculum_enabled=True)
        with self._env():
            s = loop.run_plan("在你旁边放一格圆石并核对")
        self.assertEqual(s.verdict, "infra")

        loop2 = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                          decide=lambda s: [{"name": "say", "arguments": {}}],
                          verify=lambda s: (False, "没放上"),
                          plan_fn=lambda g: ["找到缺的那格用圆石补上"],
                          lessons_enabled=False, curriculum_enabled=True)
        with self._env():
            s2 = loop2.run_plan("在你旁边放一格圆石并核对")
        self.assertEqual(s2.verdict, "fail")
        recs = [json.loads(l) for l in
                open(self.cur_file, encoding="utf-8").readlines()]
        self.assertEqual(len(recs), 1)        # 只记了真失败那回
        self.assertEqual(recs[0]["verdict"], "fail")

    def test_incident_replay_outage_is_not_capability_fail(self):
        """事故重放：2026-10-17 21:37 那次「0 次调用判 fail」的原样场景。

        当时 block_probe 修复后重派任务，偏偏撞上端点+客户端双抖：
        decide 两次拿到空回复（异常被吞），闭环以「没有执行任何工具调用」
        判 fail 并记进 L3 账。现在同样的输入必须得到 infra，且不碰账本。
        """
        def ask(msgs):
            raise BrainError("网络错误: [WinError 10061] 由于目标计算机积极拒绝")

        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=make_decide(ask, _reg("say")),
                         verify=make_verify(ask),
                         plan_fn=lambda g: [g],      # planner 也退化单步
                         lessons_enabled=True, curriculum_enabled=True,
                         max_rounds=4)
        with self._env():
            s = loop.run_plan("你去 x=-41 到 -37 那面石墙看看，找到缺的那格"
                              "用圆石补上，再 block_probe 核对那一格")
        self.assertEqual(s.verdict, "infra")
        self.assertFalse(os.path.exists(self.cur_file))

    def test_infra_survives_serialization(self):
        s = AgentState(goal="补墙", infra_error="大脑端点不可达：timeout")
        self.assertEqual(AgentState.from_dict(s.to_dict()).infra_error,
                         "大脑端点不可达：timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
