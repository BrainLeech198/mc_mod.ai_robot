#!/usr/bin/env python3
"""robot_chat 解析/分级单测。运行：python tools/test_robot_chat.py"""
import unittest

import robot_chat as rc


class ParseTest(unittest.TestCase):
    def test_player_line(self):
        self.assertEqual(rc.parse("<oahnuy198> 你好啊"), ("player", "oahnuy198", "你好啊"))

    def test_player_no_space(self):
        self.assertEqual(rc.parse("<DeepSeek>hi"), ("player", "DeepSeek", "hi"))

    def test_system_line(self):
        self.assertEqual(rc.parse("[oahnuy198: Teleported]")[0], "system")

    def test_system_yzt(self):
        self.assertEqual(rc.parse("DeepSeek 暂时没有了动静")[0], "system")

    def test_empty(self):
        self.assertEqual(rc.parse("")[0], "empty")

    def test_not_mistake_bracket(self):
        # 系统提示里的方括号不能被当玩家
        self.assertEqual(rc.parse("欢迎 DeepSeek 第一次来到服务器！")[0], "system")


class AnalyzeTest(unittest.TestCase):
    def test_we_safe(self):
        i = rc.analyze("//size")
        self.assertEqual((i.kind, i.dangerous), ("we", False))

    def test_we_wand_safe(self):
        self.assertFalse(rc.analyze("//wand").dangerous)

    def test_we_set_dangerous(self):
        i = rc.analyze("//set stone")
        self.assertEqual((i.kind, i.dangerous), ("we", True))

    def test_we_walls_dangerous(self):
        self.assertTrue(rc.analyze("//walls oak_log").dangerous)

    def test_we_unknown_conservative(self):
        self.assertTrue(rc.analyze("//frobnicate").dangerous)

    def test_op_dangerous(self):
        self.assertTrue(rc.analyze("/op someone").dangerous)

    def test_lp_dangerous(self):
        self.assertTrue(rc.analyze("/lp user x permission set y").dangerous)

    def test_list_safe(self):
        self.assertFalse(rc.analyze("/list").dangerous)

    def test_natural_danger_words(self):
        self.assertTrue(rc.analyze("帮我把那个建筑删掉").dangerous)

    def test_natural_safe(self):
        i = rc.analyze("你好，帮我看看位置")
        self.assertEqual((i.kind, i.dangerous), ("chat", False))


class SummonTest(unittest.TestCase):
    def test_mention(self):
        self.assertTrue(rc.is_summon("DeepSeek 在吗", "DeepSeek", ["!ds"]))

    def test_mention_case_insensitive(self):
        self.assertTrue(rc.is_summon("deepseek 过来", "DeepSeek", ["!ds"]))

    def test_prefix(self):
        self.assertTrue(rc.is_summon("!ds 报位置", "DeepSeek", ["!ds"]))

    def test_no_summon(self):
        self.assertFalse(rc.is_summon("今天天气不错", "DeepSeek", ["!ds"]))

    def test_strip_prefix(self):
        self.assertEqual(rc.strip_summon("!ds 报位置", "DeepSeek", ["!ds"]), "报位置")

    def test_strip_mention(self):
        self.assertEqual(rc.strip_summon("DeepSeek: 你好", "DeepSeek", ["!ds"]), "你好")


if __name__ == "__main__":
    unittest.main(verbosity=2)
