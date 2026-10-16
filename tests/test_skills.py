"""技能库单测：检索、渲染、入库、去重、统计，以及与编排层的联动。

用 MCROBOT_SKILLS_FILE 指到临时文件，别污染 version/<版本>/skills.jsonl。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from mcrobot.skills import library as skl
from mcrobot.tools.registry import CallContext, Registry, Tool, ToolResult


def _reg(*names: str) -> Registry:
    r = Registry()
    for n in names:
        r.register(Tool(n, f"工具 {n}", {"type": "object", "properties": {}},
                        "safe", (lambda nn: (lambda a, c: ToolResult(True, nn, "", text="ok")))(n)))
    return r


class SkillFileTest(unittest.TestCase):
    """每个用例都用独立临时文件，跑完还原环境变量。"""

    def setUp(self):
        self._old = os.environ.get("MCROBOT_SKILLS_FILE")
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                               encoding="utf-8")
        self.tmp.close()
        os.environ["MCROBOT_SKILLS_FILE"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MCROBOT_SKILLS_FILE", None)
        else:
            os.environ["MCROBOT_SKILLS_FILE"] = self._old
        os.unlink(self.tmp.name)


class PresetTest(SkillFileTest):
    def test_install_and_get(self):
        n = skl.install_presets()
        self.assertEqual(n, 6)
        self.assertIsNotNone(skl.get("build_wall"))
        self.assertEqual(len(skl.load()), 6)

    def test_install_twice_is_idempotent(self):
        skl.install_presets()
        self.assertEqual(skl.install_presets(), 0)


class SearchTest(SkillFileTest):
    def setUp(self):
        super().setUp()
        skl.install_presets()

    def test_chinese_query(self):
        hits = skl.search("帮我砌一圈墙")
        self.assertEqual(hits[0].name, "build_wall")

    def test_english_query(self):
        self.assertEqual(skl.search("build a room")[0].name, "build_room")

    def test_irrelevant_query_returns_nothing(self):
        self.assertEqual(skl.search("今天天气怎么样"), [])

    def test_top_k(self):
        self.assertLessEqual(len(skl.search("墙", top_k=1)), 1)


class RenderTest(SkillFileTest):
    def setUp(self):
        super().setUp()
        skl.install_presets()

    def test_params_substituted(self):
        calls = skl.get("build_wall").render(pos1="0,0,0", pos2="10,4,0", block="bricks")
        self.assertEqual(calls[0]["arguments"]["pos1"], "0,0,0")
        self.assertEqual(calls[1]["arguments"]["block"], "bricks")

    def test_default_used_when_not_given(self):
        calls = skl.get("build_wall").render(pos1="0,0,0", pos2="10,4,0")
        self.assertEqual(calls[1]["arguments"]["block"], "stone_bricks")

    def test_missing_param_raises(self):
        with self.assertRaises(ValueError):
            skl.get("build_wall").render(pos1="0,0,0")

    def test_replace_uses_two_blocks(self):
        calls = skl.get("replace_blocks").render(pos1="0,0,0", pos2="1,1,1")
        self.assertEqual(calls[1]["arguments"], {"block": "dirt", "to": "grass_block"})


class LearnTest(SkillFileTest):
    def test_learn_from_actions_skips_chat(self):
        sk = skl.learn_from_actions("刷个墙", [
            {"name": "say", "arguments": {"text": "好"}},
            {"name": "run_command", "arguments": {"command": "//walls stone"}},
        ])
        self.assertIsNotNone(sk)
        self.assertEqual(sk.steps, [{"tool": "run_command",
                                     "arguments": {"command": "//walls stone"}}])
        self.assertEqual(sk.source, "learned")

    def test_only_chat_learns_nothing(self):
        self.assertIsNone(skl.learn_from_actions("聊两句",
                                                 [{"name": "say", "arguments": {}}]))

    def test_duplicate_sequence_only_records_use(self):
        acts = [{"name": "run_command", "arguments": {"command": "//set air"}}]
        a = skl.learn_from_actions("清一块地", acts)
        b = skl.learn_from_actions("把这块填平", acts)
        self.assertEqual(a.name, b.name)
        self.assertEqual(skl.get(a.name).uses, 1)

    def test_preset_is_not_overwritten(self):
        skl.install_presets()
        fake = skl.Skill(name="build_wall", description="假的", steps=[], source="learned")
        skl.save(fake)
        self.assertEqual(skl.get("build_wall").description,
                         "在两个对角点之间砌一圈墙（选区边界）。")

    def test_stats(self):
        sk = skl.learn_from_actions("挖个坑", [{"name": "run_command", "arguments": {}}])
        skl.record(sk.name, True)
        skl.record(sk.name, False)
        self.assertEqual((skl.get(sk.name).uses, skl.get(sk.name).success), (2, 1))


class LoopSkillTest(SkillFileTest):
    """技能库与 AgentLoop 的联动：规划前检索、做成后入库。"""

    def setUp(self):
        super().setUp()
        skl.install_presets()

    def test_perceive_injects_matching_skills(self):
        from mcrobot.brain.agent_loop import AgentLoop
        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         decide=lambda s: [{"name": "say", "arguments": {}}],
                         verify=lambda s: (True, ""))
        s = loop.run("帮我砌一圈墙")
        self.assertIn("skills", s.observation)
        self.assertEqual(s.observation["skills"][0]["name"], "build_wall")

    def test_success_is_learned(self):
        from mcrobot.brain.agent_loop import AgentLoop
        loop = AgentLoop(registry=_reg("run_command"), tools_for_observation=(),
                         decide=lambda s: [{"name": "run_command",
                                            "arguments": {"command": "//walls stone"}}],
                         verify=lambda s: (True, ""))
        s = loop.run("砌墙")
        self.assertEqual(s.verdict, "ok")
        self.assertTrue(any(x.name.startswith("learned-") for x in skl.load()))

    def test_failure_is_not_learned(self):
        from mcrobot.brain.agent_loop import AgentLoop
        loop = AgentLoop(registry=_reg("run_command"), tools_for_observation=(),
                         decide=lambda s: [{"name": "run_command",
                                            "arguments": {"command": "//walls stone"}}],
                         verify=lambda s: (False, "没成"), max_rounds=1)
        loop.run("砌墙")
        self.assertFalse(any(x.name.startswith("learned-") for x in skl.load()))


class SkillToolTest(SkillFileTest):
    """skill_search / skill_run / skill_save 三个工具的门控行为。"""

    def setUp(self):
        super().setUp()
        skl.install_presets()
        from mcrobot.tools import builtin
        self.reg = builtin.build_registry()
        self.ctx = CallContext(actor="oahnuy198", dangerous_allowed=("oahnuy198",))

    def test_search_tool(self):
        r = self.reg.call("skill_search", {"query": "砌墙"}, self.ctx)
        self.assertTrue(r.ok)
        self.assertEqual(r.data["hits"][0]["name"], "build_wall")

    def test_run_tool_reports_missing_param(self):
        r = self.reg.call("skill_run", {"name": "build_wall", "params": {}}, self.ctx)
        self.assertFalse(r.ok)
        self.assertIn("缺参数", r.error)

    def test_unknown_skill(self):
        r = self.reg.call("skill_run", {"name": "nope"}, self.ctx)
        self.assertFalse(r.ok)

    def test_save_rejects_unknown_tool(self):
        r = self.reg.call("skill_save", {"name": "bad",
                                         "steps": [{"tool": "不存在的工具"}]}, self.ctx)
        self.assertFalse(r.ok)

    def test_save_roundtrip(self):
        r = self.reg.call("skill_save", {
            "name": "my_thing", "description": "我的套路",
            "steps": [{"tool": "worldedit_select",
                       "arguments": {"pos1": "{a}", "pos2": "{b}"}}],
            "params": {"a": "0,0,0", "b": "1,1,1"}}, self.ctx)
        self.assertTrue(r.ok)
        self.assertEqual(skl.get("my_thing").steps[0]["arguments"]["pos1"], "{a}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
