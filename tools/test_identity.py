#!/usr/bin/env python3
"""版本文件夹即身份 —— 单测。

锁住四件事：
1. identity.md 能解析出元信息/人格/世界常识/后端。
2. 提示词渲染填好占位符，并拼进**自动生成的**世界常识（来自 mods.md / 方块目录.md）
   与边界纪律（来自 workspace.json / undo-policy.json）——改文档即改 AI 的认知。
3. 查方块能用，且查不到时提示「显示名是英文」。
4. 保存按分节原地替换，不冲掉文件里的说明表格。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import identity as idy


class ParseTest(unittest.TestCase):
    def test_sections_parsed(self):
        i = idy.load_identity()
        self.assertEqual(i["version"], "1.20.1-forge")
        self.assertTrue(i["persona"])
        self.assertTrue(i["world_facts"])
        self.assertIn("模板", i["backend"])

    def test_meta_keys(self):
        self.assertIn("服务器名", idy.load_identity()["meta"])

    def test_active_version_switchable(self):
        old = idy.active_version()
        try:
            idy.set_active(old)
            self.assertEqual(idy.active_version(), old)
        finally:
            idy.set_active(old)


class ContextTest(unittest.TestCase):
    """占位符的值必须来自版本文件夹里的文件，不能在别处重复写。"""

    def test_bot_name_from_config_json(self):
        self.assertEqual(idy.context()["bot_name"], "DeepSeek")

    def test_server_host_from_config_json(self):
        cfg = idy._json_in(idy.version_dir(), "config.json")
        self.assertEqual(idy.context()["server_host"],
                         f"{cfg['server']['host']}:{cfg['server']['port']}")

    def test_dangerous_allowed_from_robot_json(self):
        robot = idy._json_in(idy.version_dir(), "robot.json")
        self.assertEqual(idy.context()["dangerous_allowed"],
                         "、".join(robot["auth"]["dangerous_allowed"]))

    def test_server_note_uses_runtime_json(self):
        rt = idy._json_in(idy.version_dir(), "runtime.json")
        self.assertIn(rt["client"]["forge"], idy.context()["server_note"])


class AutoWorldFactsTest(unittest.TestCase):
    def test_mods_read_from_mods_md(self):
        m = idy.mods_summary()
        self.assertGreater(m["count"], 10)
        self.assertTrue(any("创世神" in n for n in m["names"]))

    def test_catalog_read_from_block_catalog(self):
        cats = idy.catalog_summary()
        self.assertTrue(cats)
        self.assertEqual(sum(c["count"] for c in cats), 1426)

    def test_samples_are_spread_not_only_first(self):
        """只取前 N 条会全是同前缀（acacia_/anti_）方块，必须均匀抽样。"""
        cats = idy.catalog_summary(sample=12)
        for c in cats:
            first = [i.split(":")[1][:4] for i, _ in c["samples"]]
            self.assertGreater(len(set(first)), 3, f"{c['label']} 抽样太集中：{first}")

    def test_auto_facts_mention_catalog_and_how_to_query(self):
        facts = "\n".join(idy.auto_world_facts())
        self.assertIn("可用方块共", facts)
        self.assertIn("[[block]]", facts)

    def test_boundaries_from_workspace_and_undo_policy(self):
        bd = "\n".join(idy.boundaries())
        self.assertIn("建造区", bd)
        self.assertIn("撤回纪律", bd)


class BlocksTest(unittest.TestCase):
    def test_english_keyword_hits(self):
        hits = idy.search_blocks("desk", 5)
        self.assertTrue(hits)
        self.assertTrue(all("desk" in h["id"].lower() for h in hits))

    def test_chinese_keyword_misses_but_documented(self):
        """目录显示名是英文，中文查不到——这点必须写进身份文件的常识里。"""
        self.assertEqual(idy.search_blocks("课桌", 5), [])
        facts = "\n".join(idy.load_identity()["world_facts"])
        self.assertIn("英文", facts)

    def test_limit_respected(self):
        self.assertLessEqual(len(idy.search_blocks("acacia", 7)), 7)


class RenderTest(unittest.TestCase):
    def test_no_placeholder_left(self):
        s = idy.render_system()
        for ph in ("{bot_name}", "{server_name}", "{server_host}", "{home}",
                   "{max_chars}", "{dangerous_allowed}", "{server_note}"):
            self.assertNotIn(ph, s)

    def test_contains_persona_and_auto_and_boundaries(self):
        s = idy.render_system()
        self.assertIn("你是 DeepSeek", s)          # 人格
        self.assertIn("【这个世界里有这些】", s)     # 自动世界常识
        self.assertIn("【活动边界与纪律】", s)       # workspace/undo-policy

    def test_memory_appended(self):
        s = idy.render_system(memory_block="【最近发生的事】\n- 建了学校")
        self.assertIn("建了学校", s)


class SaveSectionTest(unittest.TestCase):
    """保存必须原地替换分节，文件里的说明表格不能被冲掉。"""

    def test_preserves_other_content(self):
        vdir = idy.version_dir()
        path = vdir / "identity.md"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "identity.md"
            tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            backup = path.read_text(encoding="utf-8")
            try:
                path.write_text("# 标题\n\n> 说明表格别删\n\n## 人格\n\n旧人格\n\n## 后端\n\n模板: x\n",
                                encoding="utf-8")
                idy.save_sections({"人格": "新人格"})
                out = path.read_text(encoding="utf-8")
                self.assertIn("新人格", out)
                self.assertNotIn("旧人格", out)
                self.assertIn("> 说明表格别删", out)   # 其余内容保留
                self.assertIn("模板: x", out)          # 别的分节不受影响
            finally:
                path.write_text(backup, encoding="utf-8")

    def test_appends_missing_section(self):
        vdir = idy.version_dir()
        path = vdir / "identity.md"
        backup = path.read_text(encoding="utf-8")
        try:
            path.write_text("# t\n\n## 人格\n\nP\n", encoding="utf-8")
            idy.save_sections({"新分节": "内容"})
            self.assertIn("## 新分节", path.read_text(encoding="utf-8"))
        finally:
            path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
