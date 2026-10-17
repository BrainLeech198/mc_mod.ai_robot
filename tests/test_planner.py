"""任务分解单测：拆解、挡闲聊、拆失败退单步、子目标顺序执行与失败即停。

和闭环测试一样用注入的假 decide/verify，不碰真 LLM、不碰真游戏。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcrobot.brain.agent_loop import AgentLoop
from mcrobot.brain import planner
from mcrobot.tools.registry import CallContext, Registry, Tool, ToolResult


def _reg(*names: str) -> Registry:
    r = Registry()
    for n in names:
        r.register(Tool(n, f"工具 {n}", {"type": "object", "properties": {}},
                        "safe", (lambda nn: (lambda a, c: ToolResult(True, nn, "", text="ok")))(n)))
    return r


def _ask(raw: str):
    return lambda msgs: raw


class LooksSimpleTest(unittest.TestCase):
    def test_chat_is_simple(self):
        self.assertTrue(planner.looks_simple("在吗"))
        self.assertTrue(planner.looks_simple("你好呀"))
        self.assertTrue(planner.looks_simple("你叫什么名字"))

    def test_empty_is_simple(self):
        self.assertTrue(planner.looks_simple(""))

    def test_build_word_never_simple_even_if_short(self):
        self.assertFalse(planner.looks_simple("建房子"))
        self.assertFalse(planner.looks_simple("修墙"))
        self.assertFalse(planner.looks_simple("build a house"))

    def test_long_goal_needs_decompose(self):
        self.assertFalse(planner.looks_simple("看看前面那片地适不适合盖学校主楼"))


class DecomposeTest(unittest.TestCase):
    def test_simple_goal_skips_llm(self):
        ask = mock.Mock(side_effect=AssertionError("不该调用 LLM"))
        self.assertEqual(planner.decompose("在吗", ask), ["在吗"])
        ask.assert_not_called()

    def test_json_array_parsed(self):
        raw = '["清出场地", "砌主楼墙体", "装上门窗"]'
        self.assertEqual(planner.decompose("建学校主楼", _ask(raw)),
                         ["清出场地", "砌主楼墙体", "装上门窗"])

    def test_fenced_json_and_dedupe_and_cap(self):
        raw = "```json\n[\"砌墙\", \"砌墙\", \"装窗\", \"铺地\", \"清理\", \"第7步\", \"第8步\"]\n```"
        out = planner.decompose("建学校", _ask(raw), max_subtasks=4)
        self.assertEqual(out, ["砌墙", "装窗", "铺地", "清理"])   # 去重 + 截到 4

    def test_prose_with_quotes_is_tolerated(self):
        raw = "我先「清出场地」，然后「砌主楼」，最后「装门窗」。"
        self.assertEqual(planner.decompose("建学校", _ask(raw)),
                         ["清出场地", "砌主楼", "装门窗"])

    def test_garbage_falls_back_to_single_step(self):
        self.assertEqual(planner.decompose("建学校", _ask("我觉得应该这样那样")),
                         ["建学校"])
        self.assertEqual(planner.decompose("建学校", _ask("[]")), ["建学校"])

    def test_unparsable_answer_gets_one_nudge_then_falls_back(self):
        """真机踩过：推理模型有时只回 reasoning、content 为空，第一次拆解静默退化。"""
        ask = mock.Mock(side_effect=["让我想想……", "还是想不出来"])
        self.assertEqual(planner.decompose("建学校", ask), ["建学校"])
        self.assertEqual(ask.call_count, 2)               # 补问了一次
        self.assertIn("JSON 数组", ask.call_args[0][0][-1]["content"])

    def test_nudge_recovers_the_plan(self):
        ask = mock.Mock(side_effect=["（空）", '["清场地", "砌墙"]'])
        self.assertEqual(planner.decompose("建学校", ask), ["清场地", "砌墙"])
        self.assertEqual(ask.call_count, 2)

    def test_llm_error_falls_back(self):
        def boom(msgs):
            raise OSError("网络断了")
        self.assertEqual(planner.decompose("建学校", boom), ["建学校"])


class RunPlanTest(unittest.TestCase):
    def setUp(self):
        # 分级表记账默认开着——指到临时文件，别往版本里的真表写测试垃圾
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._env = mock.patch.dict("os.environ",
                                    {"MCROBOT_CURRICULUM_FILE":
                                     str(Path(self._td.name) / "cur.jsonl")})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _loop(self, plan=None, **kw):
        return AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=lambda s: (True, ""), plan_fn=plan, **kw)

    def test_no_planner_behaves_like_single_run(self):
        s = self._loop().run_plan("砌墙")
        self.assertEqual(s.steps, ["砌墙"])
        self.assertEqual(s.verdict, "ok")
        self.assertEqual(len(s.subtasks), 1)

    def test_steps_run_in_order_with_context(self):
        seen: list[str] = []
        loop = self._loop(plan=lambda g: ["第一步", "第二步", "第三步"])
        loop.decide_fn = lambda s: (seen.append((s.goal, s.context))
                                    or [{"name": "say", "arguments": {}}])
        s = loop.run_plan("建学校")
        self.assertEqual([g for g, _ in seen], ["第一步", "第二步", "第三步"])
        # 第一步的 context 只有分级提醒（空表 + L5 目标 = 超纲），没有前序结果
        self.assertIn("能力分级", seen[0][1])
        self.assertNotIn("→ok", seen[0][1])
        self.assertIn("第一步→ok", seen[1][1])                 # 第二步看得到第一步的结果
        self.assertIn("第二步→ok", seen[2][1])
        self.assertEqual(s.verdict, "ok")
        self.assertEqual([t["verdict"] for t in s.subtasks], ["ok"] * 3)
        self.assertEqual(s.tool_calls, 3)

    def test_first_failure_stops_rest(self):
        loop = self._loop(plan=lambda g: ["能成", "会挂", "不该跑"])
        seen: list[str] = []

        def verify(s):
            seen.append(s.goal)
            return (s.goal == "能成", "差两格" if s.goal == "会挂" else "")
        loop.verify_fn = verify
        s = loop.run_plan("建学校")
        self.assertEqual([t["verdict"] for t in s.subtasks],
                         ["ok", "fail", "skipped"])
        self.assertIn("会挂", s.critique)
        self.assertIn("跳过", s.aborted)
        self.assertNotIn("不该跑", seen)          # 第三步连闭环都没进
        self.assertEqual(seen.count("会挂"), 4)   # 但它自己把 4 轮重试用完了才停

    def test_continue_after_failure_when_configured(self):
        loop = self._loop(plan=lambda g: ["会挂", "也能过"], stop_on_fail=False)
        loop.verify_fn = lambda s: (s.goal == "也能过", "")
        s = loop.run_plan("建学校")
        self.assertEqual([t["verdict"] for t in s.subtasks], ["fail", "ok"])
        self.assertEqual(s.verdict, "fail")      # 有一个没成，整体就不算成

    def test_planner_exception_does_not_fail_task(self):
        def boom(g):
            raise RuntimeError("拆不动")
        s = self._loop(plan=boom).run_plan("砌墙")
        self.assertEqual(s.steps, ["砌墙"])
        self.assertEqual(s.verdict, "ok")        # 拆失败不许把任务搞失败

    def test_subtask_ids_are_distinct_for_checkpoint(self):
        ids = []
        loop = self._loop(plan=lambda g: ["一", "二"])

        def run(goal, **kw):
            ids.append(kw.get("task_id"))
            return AgentLoop.run(loop, goal, **kw)
        loop.run = run  # type: ignore[assignment]
        loop.run_plan("建学校", task_id="base")
        self.assertEqual(ids, ["base-1", "base-2"])

    def test_receipts_and_actions_merge_into_total(self):
        loop = self._loop(plan=lambda g: ["一", "二"])
        s = loop.run_plan("建学校")
        self.assertEqual(len(s.receipts), 2)
        self.assertEqual(len(s.actions), 2)

    def test_state_round_trips_with_steps(self):
        loop = self._loop(plan=lambda g: ["一", "二"])
        d = loop.run_plan("建学校", task_id="t").to_dict()
        self.assertEqual(d["steps"], ["一", "二"])
        self.assertEqual(len(d["subtasks"]), 2)
        from mcrobot.brain.agent_loop import AgentState
        s2 = AgentState.from_dict(d)
        self.assertEqual(s2.steps, ["一", "二"])
        self.assertEqual(s2.subtasks, d["subtasks"])


class RenderContextTest(unittest.TestCase):
    def test_context_shown_to_model(self):
        from mcrobot.brain.agent_loop import AgentState
        from mcrobot.brain.llm_policy import render_state
        s = AgentState(goal="装门窗", context="砌墙→ok；铺地→ok")
        text = render_state(s, [])
        self.assertIn("前序子目标的结果", text)
        self.assertIn("砌墙→ok", text)


if __name__ == "__main__":
    unittest.main()
