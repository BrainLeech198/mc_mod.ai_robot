#!/usr/bin/env python3
"""控制台单测：锁住「自动拼装提示词」「密钥不泄露」「状态探测不炸」三件事。

不真的起 HTTP 服务，直接调 API 函数——快且不占端口。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import console as cs
import identity as idy


class PreviewTest(unittest.TestCase):
    def test_messages_have_system_and_user(self):
        r = cs.api_preview({"text": "帮我在操场边种两棵树"})
        self.assertEqual(r["client"], idy.active_version())
        roles = [m["role"] for m in r["messages"]]
        self.assertEqual(roles[0], "system")
        self.assertEqual(roles[-1], "user")
        self.assertIn("种两棵树", r["messages"][-1]["content"])

    def test_system_carries_persona_and_world_facts(self):
        r = cs.api_preview({"text": "在吗"})
        sysmsg = r["messages"][0]["content"]
        self.assertIn("DeepSeek", sysmsg)          # 人格
        self.assertIn("方块目录", sysmsg)           # 自动世界常识（来自版本文档）
        self.assertGreater(sysmsg.count("\n"), 5)  # 多行，不是挤成一坨

    def test_env_snapshot_injected(self):
        r = cs.api_preview({"text": "在吗",
                            "env": {"pos": [1, 2, 3], "online": ["oahnuy198"]}})
        self.assertIn("你现在的状态", r["messages"][-1]["content"])


class SecretTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = cs.SECRETS
        cs.SECRETS = Path(self._tmp.name) / "secrets.env"

    def tearDown(self):
        cs.SECRETS = self._old
        self._tmp.cleanup()

    def test_save_and_mask(self):
        r = cs.api_key({"value": "sk-abcdefgh1234"})
        self.assertTrue(r["set"])
        self.assertTrue(r["masked"].startswith("sk-a"))
        self.assertNotIn("abcdefgh", r["masked"])  # 掩码不能泄露中间段
        self.assertIn("MCROBOT_LLM_KEY", cs.SECRETS.read_text(encoding="utf-8"))

    def test_custom_env_name(self):
        cs.api_key({"value": "xyz", "env": "MY_OWN_KEY"})
        self.assertEqual(os.environ["MY_OWN_KEY"], "xyz")
        os.environ.pop("MY_OWN_KEY", None)

    def test_mask_short_value(self):
        self.assertEqual(cs.mask("ab"), "**")
        self.assertEqual(cs.mask(""), "")


class StateTest(unittest.TestCase):
    def test_state_has_all_panels(self):
        s = cs.api_state()
        for k in ("client", "backend", "status", "queue"):
            self.assertIn(k, s)
        self.assertIn("list", s["client"])
        self.assertIn("probe", s["backend"])
        self.assertIn("body", s["status"])

    def test_key_value_never_in_state(self):
        os.environ["MCROBOT_LLM_KEY"] = "sk-shouldnotappear"
        try:
            s = json.dumps(cs.api_state(), ensure_ascii=False)
            self.assertNotIn("shouldnotappear", s)
        finally:
            os.environ.pop("MCROBOT_LLM_KEY", None)

    def test_logs_tail_does_not_crash(self):
        for name in ("brain_llm", "robot", "bridge"):
            self.assertIsInstance(cs.api_logs({"name": name})["lines"], list)


class ProcessDetectTest(unittest.TestCase):
    """进程探测的假阳性防护（踩过两次坑：自匹配、deepsink_brain 冒充 brain）。"""

    def setUp(self):
        self._old = cs._cmdlines

    def tearDown(self):
        cs._cmdlines = self._old

    def _fake(self, lines):
        cs._cmdlines = lambda: lines

    def test_sim_worker_is_not_bridge_brain(self):
        self._fake(["python deepsink_brain.py watch --worker claude-mc --timeout 300"])
        self.assertFalse(cs._running("brain.py watch"))   # 子串命中 = 假阳性
        self.assertTrue(cs._running("deepsink_brain.py"))

    def test_real_brain_detected(self):
        self._fake(["python brain.py watch --timeout 1800"])
        self.assertTrue(cs._running("brain.py watch"))

    def test_robot_and_llm(self):
        self._fake(["python -u tools/robot.py --interval 1.0",
                    "python -u brain_llm.py watch --timeout 7200"])
        self.assertTrue(cs._running("robot.py"))
        self.assertTrue(cs._running("brain_llm.py"))
        self.assertFalse(cs._running("supervisor.py"))

    def test_nothing_matches_garbage(self):
        self.assertFalse(cs._running("definitely_not_a_script.py"))


class RouteTest(unittest.TestCase):
    def test_every_route_callable(self):
        for (m, p), fn in cs.ROUTES.items():
            self.assertTrue(callable(fn), f"{m} {p}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
