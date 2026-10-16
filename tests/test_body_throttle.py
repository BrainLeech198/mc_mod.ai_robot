#!/usr/bin/env python3
"""robot 去重/节流单测。运行：python tools/test_robot_throttle.py

不需要真实控制桥：把 robot.cc.chat 换成记录器即可。
"""
import json
import tempfile
import unittest
from pathlib import Path

from mcrobot.body import loop as robot


def make_bot(tmp: Path, *, dedup=6.0, interval=0.0, max_consec=2, chain_reset=10.0, conv_window=120.0, env_on=False,
             listen=None, allowed=("oahnuy198",)):
    cfg = {
        "nickname": "DeepSeek",
        "summon": {"mention": True, "prefixes": ["!ds"]},
        "auth": {"dangerous_allowed": list(allowed)},
        "ambient": {"enabled": False},
        "throttle": {
            "dedup_window_seconds": dedup,
            "min_send_interval_seconds": interval,
            "max_consecutive_auto_replies": max_consec,
            "chain_reset_seconds": chain_reset,
            "conversation_window_seconds": conv_window,
        },
        "paths": {
            "inbox": str(tmp / "inbox.jsonl"),
            "outbox": str(tmp / "outbox.jsonl"),
            "cursor": str(tmp / "cursor.json"),
            "log": str(tmp / "ops.jsonl"),
        },
        # 单测默认不开环境快照（否则会真去调控制桥跑 /list、/data）
        "env_snapshot": env_on,
        "replies": {"ack_pool": ["回执甲", "回执乙", "回执丙"],
                    "greet_pool": ["招呼甲", "招呼乙"]},
    }
    if listen is not None:
        cfg["listen"] = listen
    return robot.Robot(cfg, dry=False)


class ThrottleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent: list[str] = []
        robot.cc.chat = lambda text: self.sent.append(text)  # 拦截发送

    def test_dedup_same_message_once(self):
        bot = make_bot(self.tmp)
        for _ in range(4):
            bot.handle("oahnuy198", "@DeepSeek 你好")
        self.assertEqual(len(self.sent), 1, self.sent)

    def test_dedup_allows_different_text(self):
        bot = make_bot(self.tmp)
        bot.handle("oahnuy198", "@DeepSeek 你好")
        bot.handle("oahnuy198", "@DeepSeek 在吗")
        self.assertEqual(len(self.sent), 2, self.sent)

    def test_consecutive_cap(self):
        bot = make_bot(self.tmp, max_consec=2)
        bot.handle("oahnuy198", "@DeepSeek 你好")
        bot.handle("oahnuy198", "@DeepSeek 在吗")
        bot.handle("oahnuy198", "@DeepSeek hello")
        self.assertEqual(len(self.sent), 2, self.sent)  # 第 3 条被节流

    def test_chain_resets_on_other_speaker(self):
        bot = make_bot(self.tmp, max_consec=1)
        bot.handle("oahnuy198", "@DeepSeek 你好")
        bot.handle("woshinidiedongma", "@DeepSeek 你好")
        self.assertEqual(len(self.sent), 2, self.sent)

    def test_own_message_ignored(self):
        bot = make_bot(self.tmp)
        bot.handle("DeepSeek", "@DeepSeek 你好")
        self.assertEqual(self.sent, [])

    def test_tick_pipeline_dedup(self):
        """走完整 tick：一次回执里 4 条相同玩家消息 → 只回 1 条。"""
        bot = make_bot(self.tmp)
        buf = {"chats": [{"text": "<oahnuy198> @DeepSeek 你好"}] * 4, "seq": 10}
        robot.cc.chat_since = lambda since: buf
        bot.tick()
        self.assertEqual(len(self.sent), 1, self.sent)

    def test_tick_ignores_system_lines(self):
        """系统行（/list 输出等）不应触发任何回复。"""
        bot = make_bot(self.tmp)
        buf = {"chats": [{"text": "There are 2 of a max of 20 players online: oahnuy198, DeepSeek"}],
               "seq": 11}
        robot.cc.chat_since = lambda since: buf
        bot.tick()
        self.assertEqual(self.sent, [])

    def test_conversation_window_continuation(self):
        """@一次之后，窗口内不带 @ 的后续发言也算召唤。"""
        bot = make_bot(self.tmp)
        bot.handle("oahnuy198", "@DeepSeek 你好")
        bot.handle("oahnuy198", "那我们建个学校吧")
        self.assertEqual(len(self.sent), 2, self.sent)

    def test_no_window_means_no_continuation(self):
        """窗口=0 时，不带 @ 的发言不算召唤（只记 ambient，不回）。"""
        bot = make_bot(self.tmp, conv_window=0.0)
        bot.handle("oahnuy198", "@DeepSeek 你好")
        bot.handle("oahnuy198", "那我们建个学校吧")
        self.assertEqual(len(self.sent), 1, self.sent)


class AckPoolTest(unittest.TestCase):
    """即时回执：随机但不离池，避免每次同一句像机器人。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent: list[str] = []
        robot.cc.chat = lambda text: self.sent.append(text)

    def test_ack_from_pool(self):
        seen = set()
        for i in range(30):
            # 每次新建：连续回复上限(max_consec=2)会让同一个 bot 只发得出前两条，
            # 那样 seen 最多 2 个值，随机撞同一句就 flaky。
            bot = make_bot(self.tmp)
            bot.handle("oahnuy198", f"@DeepSeek 第{i}问")
            seen.add(self.sent[-1])
        for s in seen:
            self.assertTrue(s.startswith("@oahnuy198 "), s)
            self.assertIn(s[len("@oahnuy198 "):], ["回执甲", "回执乙", "回执丙"], s)
        self.assertGreater(len(seen), 1, "30 次回执全是同一句？池子没起作用")

    def test_ack_default_pool_when_cfg_missing(self):
        cfg = {
            "nickname": "DeepSeek", "summon": {"mention": True, "prefixes": ["!ds"]},
            "auth": {"dangerous_allowed": []}, "ambient": {"enabled": False},
            "throttle": {}, "paths": {
                "inbox": str(self.tmp / "i.jsonl"), "outbox": str(self.tmp / "o.jsonl"),
                "cursor": str(self.tmp / "c.json"), "log": str(self.tmp / "l.jsonl")},
            "env_snapshot": False}
        bot = robot.Robot(cfg)
        self.assertGreaterEqual(len(bot.ack_pool), 3)
        self.assertTrue(bot.ack("p").startswith("@p "))


class EnvSnapshotTest(unittest.TestCase):
    """环境快照：召唤入队时附位置/在线/背包。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent: list[str] = []
        self.cmds: list[str] = []
        robot.cc.chat = lambda text: self.sent.append(text)
        robot.cc.cmd = lambda c: self.cmds.append(c)
        robot.time.sleep = lambda s: None  # cmd_read 的等待免了
        robot.cc.state = lambda: {"player": {"dim": "multiworld:ai", "x": 1.0, "y": -56.0, "z": 2.0}}
        self.list_line = "There are 2 of a max of 20 players online: DeepSeek, oahnuy198"
        self.data_line = ('DeepSeek has the following entity data: {Inventory: [{Slot: 0b, id: '
                          '"minecraft:wooden_axe", Count: 1b}, {Slot: 1b, id: "minecraft:wooden_axe", '
                          'Count: 1b}, {Slot: 2b, id: "minecraft:wooden_axe", Count: 1b}, {Slot: 3b, '
                          'id: "minecraft:wooden_axe", Count: 1b}], foodTickTimer: 0}')
        self._seq_calls = 0

        def chat_since(since):
            self._seq_calls += 1
            if self._seq_calls == 1:  # _latest_seq：记基线
                return {"seq": 5, "chats": []}
            if self.cmds and self.cmds[-1] == "/list":
                return {"seq": 6, "chats": [{"text": self.list_line}]}
            return {"seq": 7, "chats": [{"text": self.data_line}]}

        robot.cc.chat_since = chat_since

    def test_summon_attaches_env(self):
        bot = make_bot(self.tmp, env_on=True)
        # 用不命中 L0 反射的消息（L0 命中的会秒回、不入队，见 ReflexFirstTest）
        bot.handle("oahnuy198", "@DeepSeek 帮我设计个学校布局")
        rec = [json.loads(l) for l in (self.tmp / "inbox.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rec), 1)
        env = rec[0].get("env")
        self.assertIsNotNone(env, "召唤入队应带 env")
        self.assertIn("multiworld:ai", env["pos"])
        self.assertEqual(env["online"], ["DeepSeek", "oahnuy198"])
        self.assertEqual(env["inv"], "wooden_axe x4")

    def test_no_env_when_disabled(self):
        bot = make_bot(self.tmp, env_on=False)
        bot.handle("oahnuy198", "@DeepSeek 帮我设计个学校布局")
        rec = [json.loads(l) for l in (self.tmp / "inbox.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertNotIn("env", rec[0])
        self.assertEqual(self.cmds, [])  # 不该调任何指令


class ReflexFirstTest(unittest.TestCase):
    """L0 反射优先：能秒回的先秒回，不必干等大脑（2026-10-16）。

    背景：原先 queue_all_summon=true 时一律入队等大脑，玩家问「在吗」也被挡在
    大脑后面——大脑一掉线就只剩兜底废话。现在 L0 先接，接不住才给 L1。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        self.sent = []
        # 打桩要还原：robot.cc 是模块级全局，污染了会连累后面的测试类
        self._orig_chat = robot.cc.chat
        self._orig_since = robot.cc.chat_since
        robot.cc.chat = lambda t: (self.sent.append(t), {"ok": True})[1]
        robot.cc.chat_since = lambda s=0: {"seq": 0, "chats": []}
        self.td = td

    def tearDown(self):
        robot.cc.chat = self._orig_chat
        robot.cc.chat_since = self._orig_since
        self.tmp.cleanup()

    def _bot(self):
        return make_bot(self.td)

    def _inbox(self):
        p = self.td / "inbox.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]

    def test_greet_is_answered_instantly_without_brain(self):
        """「在吗」这类招呼 L0 秒回，且**不入队**等大脑。"""
        bot = self._bot()
        bot.handle("oahnuy198", "@DeepSeek 在吗")
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn(self.sent[0], ["@oahnuy198 招呼甲", "@oahnuy198 招呼乙"])
        self.assertEqual(self._inbox(), [], "L0 命中的不该进大脑队列")

    def test_unknown_goes_to_brain(self):
        """L0 接不住的才应一声 + 入队给大脑。"""
        bot = self._bot()
        bot.handle("oahnuy198", "@DeepSeek 帮我设计个学校布局")
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertTrue(self.sent[0].startswith("@oahnuy198 回执"), self.sent)
        self.assertEqual(len(self._inbox()), 1, "L0 未命中应入队")

    def test_greet_varies(self):
        """同一句招呼多次，回复不总是同一句（不像复读机）。"""
        seen = set()
        for _ in range(30):
            bot = self._bot()  # 每次新建：绕开去重窗口
            bot.handle("oahnuy198", "@DeepSeek 在吗")
            seen.add(self.sent[-1])
        self.assertGreater(len(seen), 1, "招呼应从池子里换句")


class NearbyListenTest(unittest.TestCase):
    """附近聆听（2026-10-16）：不用 @，人在身边说话也当在叫它。

    背景：玩家不会每句话都 @DeepSeek，不 @ 就完全没反应，看上去像挂机。
    现在同维度 + 半径内的发言一律当召唤；更远的仍走 ambient（只记录）。
    边界：附近听到的话不算点名，其中的指令只认危险操作白名单里的人。
    """

    ME = {"dim": "multiworld:ai", "x": 80.0, "y": -56.0, "z": -60.0}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.sent: list[str] = []
        self.cmds: list[str] = []
        self._orig = {k: getattr(robot.cc, k) for k in ("chat", "cmd", "state", "chat_since")}
        robot.cc.chat = lambda t: self.sent.append(t)
        robot.cc.cmd = lambda c: self.cmds.append(c)
        robot.cc.state = lambda: {"player": dict(self.ME)}
        robot.cc.chat_since = lambda s=0: {"seq": 0, "chats": []}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(robot.cc, k, v)
        self.tmp.cleanup()

    def _bot(self, radius=24.0, same_dim=True, allowed=("oahnuy198",)):
        return make_bot(self.td, listen={"nearby_enabled": True, "radius_blocks": radius,
                                        "same_dim_only": same_dim, "cache_seconds": 0.0},
                        allowed=allowed)

    def _stub_entity(self, bot, pos, dim="multiworld:ai"):
        """把 /data get 的回执按真实格式打桩（cmd_read 会真发命令，这里只替换它）。"""
        def cmd_read(command, wait=2.0):
            self.cmds.append(command)
            if command.endswith(" Pos"):
                return [f"Someone has the following entity data: "
                        f"[{pos[0]}d, {pos[1]}d, {pos[2]}d]"]
            if command.endswith(" Dimension"):
                return [f'Someone has the following entity data: "{dim}"']
            return []
        bot.cmd_read = cmd_read

    def _inbox(self):
        p = self.td / "inbox.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()]

    def test_nearby_chat_is_heard_without_at(self):
        """没点名，但人就站在旁边 → 照样接话。"""
        bot = self._bot()
        self._stub_entity(bot, (83.0, -56.0, -60.0))
        bot.handle("oahnuy198", "在吗")
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn(self.sent[0], ["@oahnuy198 招呼甲", "@oahnuy198 招呼乙"])

    def test_nearby_unknown_question_goes_to_brain(self):
        """旁边的人问 L0 接不住的问题 → 应一声 + 入队给大脑。"""
        bot = self._bot()
        self._stub_entity(bot, (83.0, -56.0, -60.0))
        bot.handle("oahnuy198", "帮我设计个学校布局")
        self.assertTrue(self.sent[0].startswith("@oahnuy198 回执"), self.sent)
        self.assertEqual(len(self._inbox()), 1)

    def test_far_chat_is_ignored(self):
        """隔了老远说话 → 不接话（只记录，本单测 ambient 关闭）。"""
        bot = self._bot(radius=24.0)
        self._stub_entity(bot, (200.0, -56.0, -60.0))
        bot.handle("oahnuy198", "在吗")
        self.assertEqual(self.sent, [])
        self.assertEqual(self._inbox(), [])

    def test_far_chat_skips_dimension_query(self):
        """远处的人只查一次位置就否掉，不多花一次 /data（每次查询都要等回执、阻塞主循环）。"""
        bot = self._bot()
        self._stub_entity(bot, (200.0, -56.0, -60.0))
        bot.handle("oahnuy198", "在吗")
        self.assertTrue(all(c.endswith(" Pos") for c in self.cmds), self.cmds)

    def test_other_dimension_is_ignored(self):
        """在别的维度喊，坐标再近也不算身边。"""
        bot = self._bot()
        self._stub_entity(bot, (81.0, -56.0, -60.0), dim="minecraft:overworld")
        bot.handle("oahnuy198", "在吗")
        self.assertEqual(self.sent, [])

    def test_unknown_dimension_is_ignored(self):
        """拿不到对方维度 → 保守按不在附近处理，别跨维度自言自语。"""
        bot = self._bot()
        def cmd_read(command, wait=2.0):
            self.cmds.append(command)
            if command.endswith(" Pos"):
                return ["Someone has the following entity data: [81.0d, -56.0d, -60.0d]"]
            return []
        bot.cmd_read = cmd_read
        bot.handle("oahnuy198", "在吗")
        self.assertEqual(self.sent, [])

    def test_disabled_by_config(self):
        """listen.nearby_enabled=false → 回到「必须 @」的老行为。"""
        bot = make_bot(self.td, listen={"nearby_enabled": False, "radius_blocks": 24.0})
        self._stub_entity(bot, (81.0, -56.0, -60.0))
        bot.handle("oahnuy198", "在吗")
        self.assertEqual(self.sent, [])

    def test_nearby_command_requires_whitelist(self):
        """附近听到的话不算点名：非白名单的指令一律婉拒，不执行。"""
        bot = self._bot(allowed=("oahnuy198",))
        self._stub_entity(bot, (81.0, -56.0, -60.0))
        bot.handle("stranger", "//size")
        # 只发了 /data 查询，指令本身没执行
        self.assertTrue(all(c.startswith("/data") for c in self.cmds), self.cmds)
        self.assertTrue(any("服主" in s for s in self.sent), self.sent)

    def test_nearby_command_allowed_for_whitelist(self):
        """白名单里的人在旁边下只读指令 → 正常执行。"""
        bot = self._bot(allowed=("oahnuy198",))
        self._stub_entity(bot, (81.0, -56.0, -60.0))
        bot.handle("oahnuy198", "//size")
        self.assertIn("//size", self.cmds)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
