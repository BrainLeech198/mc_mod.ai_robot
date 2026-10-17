"""知识层单测：记事实（必须带出处）、去重、检索、汇总不冲手写区、注入编排与提示词。

全部用临时版本目录（MCROBOT_KNOWLEDGE_DIR），别污染 version/1.20.1-forge/。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mcrobot import knowledge as kno
from mcrobot.brain.agent_loop import AgentLoop
from mcrobot.brain.llm_brain import Brain
from mcrobot.tools.builtin import build_registry
from mcrobot.tools.registry import CallContext, Registry, Tool, ToolResult


class _TmpVersion(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("MCROBOT_KNOWLEDGE_DIR")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MCROBOT_KNOWLEDGE_DIR"] = self.tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("MCROBOT_KNOWLEDGE_DIR", None)
        else:
            os.environ["MCROBOT_KNOWLEDGE_DIR"] = self._old
        self.tmp.cleanup()


class RememberTest(_TmpVersion):
    def test_requires_source(self):
        with self.assertRaises(ValueError):
            kno.remember("方块", "CFM 没有窗帘", source="")
        self.assertEqual(kno.load(), [])

    def test_blank_topic_or_text_rejected(self):
        with self.assertRaises(ValueError):
            kno.remember("  ", "x", source="手册")
        with self.assertRaises(ValueError):
            kno.remember("方块", "  ", source="手册")

    def test_dedupe_bumps_hits_instead_of_new_row(self):
        a, created_a = kno.remember("方块", "垃圾桶叫 rubbish_bin", source="方块目录.md")
        b, created_b = kno.remember("方块", "垃圾桶叫  rubbish_bin ", source="玩家原话")
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(kno.load()), 1)
        self.assertEqual(kno.load()[0]["hits"], 1)

    def test_same_text_different_topic_is_two_facts(self):
        kno.remember("方块", "rubbish_bin 是垃圾桶", source="方块目录.md")
        kno.remember("家具", "rubbish_bin 是垃圾桶", source="方块目录.md")
        self.assertEqual(len(kno.load()), 2)

    def test_conflict_kept_both(self):
        """冲突不自动删：两条都留，让人/模型按出处判断（红线 4）。"""
        kno.remember("皮肤", "换皮肤用 /skin set web slim <url>", source="手册 §4")
        kno.remember("皮肤", "/skin set <url> 就能换皮肤", source="旧笔记")
        self.assertEqual(len(kno.load()), 2)

    def test_forget(self):
        rec, _ = kno.remember("方块", "x 是方块", source="手册")
        self.assertTrue(kno.forget(rec["id"]))
        self.assertFalse(kno.forget(rec["id"]))
        self.assertEqual(kno.load(), [])


class SearchTest(_TmpVersion):
    def setUp(self):
        super().setUp()
        kno.remember("方块", "CFM 没有窗帘和数字时钟，垃圾桶叫 rubbish_bin",
                     tags=["家具", "cfm"], source="方块目录.md")
        kno.remember("指令", "WorldEdit 需要 OP，选区用 //pos1 和 //pos2",
                     tags=["worldedit"], source="手册 §6")
        kno.remember("玩家", "oahnuy198 讨厌呆方盒，要先用模组方块做细节",
                     tags=["偏好"], source="玩家原话")

    def test_relevant_hit(self):
        hits = kno.search("CFM 家具")
        self.assertEqual(len(hits), 1)
        self.assertIn("窗帘", hits[0]["text"])

    def test_unrelated_query_returns_nothing(self):
        self.assertEqual(kno.search("在吗"), [])   # 红线 3：不硬塞

    def test_empty_query_returns_nothing(self):
        self.assertEqual(kno.search(""), [])

    def test_fmt_for_prompt_empty_when_no_hit(self):
        self.assertEqual(kno.fmt_for_prompt("在吗"), "")

    def test_fmt_for_prompt_has_source(self):
        text = kno.fmt_for_prompt("换皮肤")
        self.assertEqual(text, "")               # 库里没有皮肤事实
        text = kno.fmt_for_prompt("窗帘")
        self.assertIn("方块目录.md", text)


class ConsolidateTest(_TmpVersion):
    def test_creates_doc_and_auto_block(self):
        kno.remember("方块", "垃圾桶叫 rubbish_bin", source="方块目录.md")
        p = kno.consolidate()
        text = p.read_text(encoding="utf-8")
        self.assertTrue(p.exists())
        self.assertIn("手写区", text)
        self.assertIn("垃圾桶叫 rubbish_bin", text)
        self.assertIn(kno.AUTO_BEGIN, text)

    def test_handwritten_area_preserved(self):
        p = kno.doc_path()
        p.write_text("# 我的表\n\n## 手写区\n\n- 这条是手写的，别动\n\n## 自动汇总\n\n"
                     + kno.AUTO_BEGIN + "\n旧内容\n" + kno.AUTO_END + "\n",
                     encoding="utf-8")
        kno.remember("方块", "新事实", source="手册")
        kno.consolidate()
        text = p.read_text(encoding="utf-8")
        self.assertIn("这条是手写的，别动", text)
        self.assertIn("新事实", text)
        self.assertNotIn("旧内容", text)          # 自动区被重写

    def test_no_marker_appends_instead_of_overwriting(self):
        p = kno.doc_path()
        p.write_text("# 手工文档\n\n别覆盖我\n", encoding="utf-8")
        kno.consolidate()
        text = p.read_text(encoding="utf-8")
        self.assertIn("别覆盖我", text)
        self.assertIn(kno.AUTO_BEGIN, text)

    def test_consolidate_refreshes_after_forget(self):
        rec, _ = kno.remember("方块", "会消失的事实", source="手册")
        kno.consolidate()
        kno.forget(rec["id"])
        kno.consolidate()
        self.assertNotIn("会消失的事实", kno.doc_path().read_text(encoding="utf-8"))


class ToolTest(_TmpVersion):
    def setUp(self):
        super().setUp()
        self.reg = build_registry()

    def ctx(self):
        return CallContext(actor="tester")

    def test_remember_tool_requires_source(self):
        r = self.reg.call("remember", {"topic": "方块", "text": "瞎猜的"}, self.ctx())
        self.assertFalse(r.ok)
        self.assertIn("source", r.error)

    def test_remember_then_recall(self):
        r = self.reg.call("remember", {"topic": "方块", "text": "CFM 没有窗帘",
                                       "source": "方块目录.md", "tags": ["家具"]}, self.ctx())
        self.assertTrue(r.ok, r.error)
        self.assertTrue(kno.doc_path().exists())      # 顺手刷新了文档
        r2 = self.reg.call("recall", {"query": "窗帘"}, self.ctx())
        self.assertTrue(r2.ok)
        self.assertEqual(len(r2.data["hits"]), 1)
        self.assertEqual(r2.data["hits"][0]["source"], "方块目录.md")

    def test_recall_missing_says_so(self):
        r = self.reg.call("recall", {"query": "量子力学"}, self.ctx())
        self.assertTrue(r.ok)
        self.assertEqual(r.data["hits"], [])
        self.assertIn("没有", r.text)

    def test_tool_count_grew_by_two(self):
        names = self.reg.names()
        self.assertIn("remember", names)
        self.assertIn("recall", names)


def _reg(*names: str) -> Registry:
    r = Registry()
    for n in names:
        r.register(Tool(n, f"工具 {n}", {"type": "object", "properties": {}},
                        "safe", (lambda nn: (lambda a, c: ToolResult(True, nn, "", text="ok")))(n)))
    return r


class LoopInjectionTest(_TmpVersion):
    def test_perceive_injects_relevant_knowledge(self):
        kno.remember("方块", "垃圾桶叫 rubbish_bin，没有 trash_can", source="方块目录.md")
        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         skills_enabled=False)
        s = loop.run("用垃圾桶这个方块砌墙")
        self.assertIn("knowledge", s.observation)
        self.assertIn("rubbish_bin", s.observation["knowledge"][0]["text"])

    def test_perceive_skips_unrelated(self):
        kno.remember("方块", "垃圾桶叫 rubbish_bin", source="方块目录.md")
        loop = AgentLoop(registry=_reg("say"), tools_for_observation=(),
                         skills_enabled=False)
        s = loop.run("在吗")
        self.assertNotIn("knowledge", s.observation)


class PromptInjectionTest(_TmpVersion):
    def test_relevant_fact_reaches_system_prompt(self):
        kno.remember("皮肤", "换皮肤用 /skin set web slim <url>", source="手册 §4")
        seen = {}

        def transport(messages):
            seen["messages"] = messages
            return "好"

        bot = Brain({"llm_api": {}}, transport=transport)
        bot.handle({"from": "oahnuy198", "text": "怎么换皮肤", "summon": True})
        self.assertIn("skin set web slim", seen["messages"][0]["content"])

    def test_unrelated_chat_not_polluted(self):
        kno.remember("皮肤", "换皮肤用 /skin set web slim <url>", source="手册 §4")
        seen = {}

        def transport(messages):
            seen["messages"] = messages
            return "在"

        bot = Brain({"llm_api": {}}, transport=transport)
        bot.handle({"from": "oahnuy198", "text": "在吗", "summon": True})
        self.assertNotIn("skin set web slim", seen["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
