"""能力分级表（curriculum）单测：定级、记账、晋升、超纲提醒、闭环接入、工具。

和知识库/技能库测试一样：MCROBOT_CURRICULUM_FILE 指到临时文件，
绝不往 version/<版本>/memory/curriculum.jsonl 写测试数据。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcrobot import curriculum
from mcrobot.brain.agent_loop import AgentLoop
from mcrobot.tools.builtin import build_registry
from mcrobot.tools.registry import CallContext


class _TempFileTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.file = Path(self._td.name) / "curriculum.jsonl"
        self._env = mock.patch.dict("os.environ",
                                    {"MCROBOT_CURRICULUM_FILE": str(self.file)})
        self._env.start()
        self.addCleanup(self._env.stop)


class GradeTest(unittest.TestCase):
    def test_chat_is_l1(self):
        self.assertEqual(curriculum.grade("在吗"), 1)
        self.assertEqual(curriculum.grade("你好呀"), 1)
        self.assertEqual(curriculum.grade(""), 1)

    def test_observe_is_l2(self):
        self.assertEqual(curriculum.grade("看看你面前的画面"), 2)
        self.assertEqual(curriculum.grade("确认一下你在哪"), 2)
        self.assertEqual(curriculum.grade("周围有没有怪物"), 2)
        self.assertEqual(curriculum.grade("随便干点活"), 2)     # 认不出 → 保守当观察

    def test_small_edit_is_l3(self):
        self.assertEqual(curriculum.grade("把这堵墙补个洞"), 3)
        self.assertEqual(curriculum.grade("帮我挖三格"), 3)
        self.assertEqual(curriculum.grade("修围墙"), 3)

    def test_build_is_l4(self):
        self.assertEqual(curriculum.grade("建一个5×4的小石屋"), 4)
        self.assertEqual(curriculum.grade("砌一面墙"), 4)
        self.assertEqual(curriculum.grade("帮我盖一间10格大的木屋"), 4)   # 10格不算工程级

    def test_project_is_l5(self):
        self.assertEqual(curriculum.grade("建学校"), 5)
        self.assertEqual(curriculum.grade("盖一栋200平方米的教学楼"), 5)
        self.assertEqual(curriculum.grade("用 worldedit 填满选区"), 5)
        self.assertEqual(curriculum.grade("砌一堵100格长的墙"), 5)
        self.assertEqual(curriculum.grade("清理12间教室"), 5)

    def test_steps_only_bump_world_touching_goals(self):
        """把一件事拆成三步去看，不会比看一眼更难——只读目标不被子目标数抬级。"""
        obs = ["看清整体", "描述结构", "说给我听"]
        self.assertEqual(curriculum.grade("看看这栋没完工的建筑", obs), 2)
        build = ["清场地", "砌墙", "装门窗"]
        self.assertEqual(curriculum.grade("把活干完", build), 4)   # 步骤里的动词也参与匹配
        five = ["一", "二", "三", "四", "五"]
        self.assertEqual(curriculum.grade("修整这块地", five), 5)  # 5+ 有序步骤 = 工程级
        self.assertEqual(curriculum.grade("干点活", five), 2)      # 认不出动没动世界 → 不抬级

    def test_trivial_chain_stays_small(self):
        """真机踩过：放一格石+核对+报告被拆成 3 步，不能因此算小型建造。"""
        steps = ["放置圆石", "block_at 核对", "say 报告"]
        self.assertEqual(curriculum.grade("在你旁边放一格圆石再核对一下", steps), 3)

    def test_building_noun_is_not_build_verb(self):
        """「建筑/建设/建议」里的「建」是名词，别误判成建造词。"""
        self.assertEqual(curriculum.grade("看看这栋建筑的结构"), 2)
        self.assertEqual(curriculum.grade("我建议你先看看"), 2)
        self.assertEqual(curriculum.grade("我建议你建个房子"), 4)   # 真动词还是认得出


class RecordTest(_TempFileTest):
    def test_append_only_and_verdict_normalized(self):
        curriculum.record("在吗", "ok", level=1, task_id="t1")
        curriculum.record("看看周围", "fail", level=2, task_id="t2")
        curriculum.record("看看周围", None, level=2, task_id="t3")   # 熔断/跳过 → fail
        recs = curriculum.load()
        self.assertEqual(len(recs), 3)
        self.assertEqual([r["verdict"] for r in recs], ["ok", "fail", "fail"])
        self.assertEqual(recs[0]["goal"], "在吗")
        self.assertEqual(recs[0]["task_id"], "t1")
        self.assertIn("ts", recs[0])

    def test_level_optional_autograde(self):
        curriculum.record("建学校", "ok")
        self.assertEqual(curriculum.load()[0]["level"], 5)

    def test_load_tolerates_garbage_lines(self):
        self.file.write_text('{"level":1,"verdict":"ok"}\n不是json\n\n', encoding="utf-8")
        self.assertEqual(len(curriculum.load()), 1)


class StatsTest(_TempFileTest):
    def _fill(self, level: int, verdicts):
        for v in verdicts:
            curriculum.record("x", v, level=level)

    def test_empty_table_unlocks_nothing(self):
        rows = curriculum.stats()
        self.assertEqual(rows[0]["tries"], 0)
        self.assertFalse(any(r["unlocked"] for r in rows))
        self.assertEqual(curriculum.unlocked_level(), 0)

    def test_promote_after_threshold_successes(self):
        self._fill(1, ["ok", "ok"])
        self.assertEqual(curriculum.unlocked_level(), 0)
        self._fill(1, ["ok"])
        self.assertEqual(curriculum.unlocked_level(), 1)

    def test_low_success_rate_blocks_promotion(self):
        """成功次数够但成功率不过半，照样不解锁（防硬刷）。"""
        self._fill(1, ["ok"] * 3 + ["fail"] * 4)
        self.assertFalse(curriculum.stats()[0]["unlocked"])
        self.assertEqual(curriculum.unlocked_level(), 0)

    def test_levels_are_climbed_one_by_one(self):
        """L4 达标但 L3 没有 → 只解锁到 L2，跳不过去。"""
        self._fill(1, ["ok"] * 3)
        self._fill(2, ["ok"] * 3)
        self._fill(4, ["ok"] * 3)
        self.assertEqual(curriculum.unlocked_level(), 2)
        self._fill(3, ["ok"] * 3)
        self.assertEqual(curriculum.unlocked_level(), 4)

    def test_l5_needs_only_two(self):
        for lv in (1, 2, 3, 4):
            self._fill(lv, ["ok"] * 3)
        self.assertEqual(curriculum.unlocked_level(), 4)
        self._fill(5, ["ok"])
        self.assertEqual(curriculum.unlocked_level(), 4)
        self._fill(5, ["ok"])
        self.assertEqual(curriculum.unlocked_level(), 5)

    def test_custom_thresholds(self):
        self._fill(1, ["ok"])
        self.assertEqual(curriculum.unlocked_level(), 0)
        self.assertEqual(curriculum.unlocked_level(thresholds={1: 1}), 1)


class NoteTest(_TempFileTest):
    def test_no_note_within_verified_range(self):
        for _ in range(3):
            curriculum.record("在吗", "ok", level=1)
        self.assertEqual(curriculum.note(1), "")

    def test_note_when_stretching_beyond(self):
        for _ in range(3):
            curriculum.record("在吗", "ok", level=1)
        n = curriculum.note(3)
        self.assertIn("L3", n)
        self.assertIn("L1", n)
        self.assertIn("block_at", n)          # 提醒它拿世界证据验
        self.assertIn("别硬装", n)

    def test_note_when_table_empty(self):
        n = curriculum.note(2)
        self.assertIn("分级表还是空的", n)


class ReportTest(_TempFileTest):
    def test_report_lists_all_levels_and_verdict(self):
        for _ in range(3):
            curriculum.record("在吗", "ok", level=1)
        text = curriculum.report()
        for lv in range(1, 6):
            self.assertIn(f"L{lv}", text)
        self.assertIn("已验证到：L1", text)
        self.assertIn("下一级 L2", text)

    def test_report_with_data_but_nothing_unlocked(self):
        curriculum.record("看看周围", "ok", level=2)
        self.assertIn("还没达到任何一级的解锁条件", curriculum.report())

    def test_summary_line_says_nothing_when_empty(self):
        self.assertIn("还是空的", curriculum.summary_line())

    def test_summary_line_reports_gap(self):
        for _ in range(3):
            curriculum.record("在吗", "ok", level=1)
        self.assertIn("还差", curriculum.summary_line())


class RunPlanIntegrationTest(_TempFileTest):
    def _loop(self, plan=None, **kw):
        from mcrobot.tools.registry import Registry, Tool, ToolResult
        reg = Registry()
        reg.register(Tool("say", "说话", {"type": "object", "properties": {}}, "safe",
                          lambda a, c: ToolResult(True, "say", "", text="ok")))
        return AgentLoop(registry=reg, tools_for_observation=(),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=lambda s: (True, ""), plan_fn=plan, **kw)

    def test_records_once_at_goal_level(self):
        s = self._loop(plan=lambda g: ["一", "二", "三"]).run_plan("砌墙", task_id="t")
        recs = curriculum.load()
        self.assertEqual(len(recs), 1)                 # 只记目标级，不记子目标
        self.assertEqual(recs[0]["level"], 4)
        self.assertEqual(recs[0]["steps"], 3)
        self.assertEqual(recs[0]["verdict"], "ok")
        self.assertEqual(recs[0]["task_id"], "t")

    def test_failed_plan_records_fail(self):
        loop = self._loop(plan=lambda g: ["会挂"])
        loop.verify_fn = lambda s: (False, "差两格")
        self._loop(plan=lambda g: ["会挂"]).verify_fn = loop.verify_fn
        loop.run_plan("砌墙")
        self.assertEqual(curriculum.load()[0]["verdict"], "fail")

    def test_note_injected_into_first_subtask_only(self):
        seen: list[tuple[str, str]] = []
        loop = self._loop(plan=lambda g: ["一", "二"])
        loop.decide_fn = lambda s: (seen.append((s.goal, s.context))
                                    or [{"name": "say", "arguments": {}}])
        loop.run_plan("建学校")                        # 空表 → 必然超纲
        self.assertIn("能力分级", seen[0][1])           # 第一个子目标拿到提醒
        self.assertNotIn("能力分级", seen[1][1])        # 后面的只有前序结果

    def test_disabled_means_no_record_no_note(self):
        seen: list[str] = []
        loop = self._loop(plan=lambda g: ["一"], curriculum_enabled=False)
        loop.decide_fn = lambda s: (seen.append(s.context)
                                    or [{"name": "say", "arguments": {}}])
        loop.run_plan("建学校")
        self.assertEqual(curriculum.load(), [])
        self.assertEqual(seen, [""])


class ToolTest(_TempFileTest):
    def setUp(self):
        super().setUp()
        self.reg = build_registry()

    def test_tool_registered_and_count(self):
        self.assertIn("curriculum", self.reg.names())
        self.assertEqual(len(self.reg.names()), 29)

    def test_tool_returns_report(self):
        r = self.reg.call("curriculum", {}, CallContext(actor="tester"))
        self.assertTrue(r.ok)
        self.assertIn("L1", r.text)
        self.assertEqual(len(r.data["levels"]), 5)


if __name__ == "__main__":
    unittest.main()
