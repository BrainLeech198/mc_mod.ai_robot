#!/usr/bin/env python3
"""brain_llm L1 大脑单测。运行：python tools/test_brain_llm.py

两组：
1. 假 transport：解析/安全校验/提示词/历史裁剪（不起网络）。
2. 本地 stub HTTP：真 urllib 全链路 e2e（endpoint 指到 localhost 临时端口）。
"""
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcrobot.brain import protocol as br
from mcrobot.brain import llm_brain as bl
from mcrobot import memory as mem_mod


def make_cfg(tmp: Path, **over):
    cfg = {
        "llm_api": {
            "endpoint": "http://127.0.0.1:9/v1",  # 默认不可达，单测里都换掉
            "model": "deepsink-chat",
            "timeout_seconds": 5,
            "history_turns": 4,
            "max_tokens": 300,
            "memory_events": 8,
            "memory_notes": 3,
        }
    }
    cfg["llm_api"].update(over)
    return cfg


def make_bot(cfg, reply="默认回复"):
    seen = {}

    def transport(messages):
        seen["messages"] = messages
        return reply

    bot = bl.Brain(cfg, transport=transport)
    return bot, seen


class ParseReplyTest(unittest.TestCase):
    def setUp(self):
        self.bot, _ = make_bot(make_cfg(Path(".")))

    def test_say_only(self):
        acts = self.bot.parse_reply("@oahnuy198 在的，听候吩咐。")
        self.assertEqual(acts, [{"say": "@oahnuy198 在的，听候吩咐。"}])

    def test_safe_cmd(self):
        acts = self.bot.parse_reply("我看看在线列表\n[[cmd]]/list")
        self.assertEqual(acts, [{"say": "我看看在线列表"}, {"cmd": "/list"}])

    def test_we_safe_cmd(self):
        acts = self.bot.parse_reply("[[cmd]]//size")
        self.assertEqual(acts, [{"cmd": "//size"}])

    def test_dangerous_cmd_refused(self):
        acts = self.bot.parse_reply("好的\n[[cmd]]/op oahnuy198")
        self.assertEqual(acts, [{"say": "好的"}])  # 危险指令被丢弃，文字照发

    def test_we_dangerous_refused(self):
        acts = self.bot.parse_reply("[[cmd]]//set stone")
        self.assertEqual(acts, [])

    def test_multi_cmd_order(self):
        acts = self.bot.parse_reply("先看选区\n[[cmd]]//size\n[[cmd]]/list")
        self.assertEqual(acts[0], {"say": "先看选区"})
        self.assertEqual(acts[1:], [{"cmd": "//size"}, {"cmd": "/list"}])

    def test_empty(self):
        self.assertEqual(self.bot.parse_reply("   \n  "), [])


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        mem_mod.MEM_PATH = Path(self.tmp.name) / "robot_memory.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_system_and_history(self):
        bot, seen = make_bot(make_cfg(Path(".")))
        bot._hist["oahnuy198"] = [
            {"role": "user", "content": "<oahnuy198> 你好"},
            {"role": "assistant", "content": "@oahnuy198 在的"},
        ]
        bot.handle({"from": "oahnuy198", "text": "在吗", "summon": True})
        msgs = seen["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1], {"role": "user", "content": "<oahnuy198> 你好"})
        self.assertEqual(msgs[-1], {"role": "user", "content": "<oahnuy198> 在吗"})

    def test_env_injected_as_player_state(self):
        """env（位置/在线/背包）应作为「你现在的状态」拼进用户消息。"""
        bot, seen = make_bot(make_cfg(Path(".")))
        bot.handle({"from": "oahnuy198", "text": "在吗", "summon": True,
                    "env": {"pos": "multiworld:ai (0.5, -56.0, 0.3)",
                            "online": ["DeepSeek", "oahnuy198"], "inv": "wooden_axe x4"}})
        last = seen["messages"][-1]["content"]
        self.assertIn("multiworld:ai", last)
        self.assertIn("wooden_axe x4", last)
        self.assertIn("oahnuy198", last)

    def test_memory_injected_into_system(self):
        mem_mod.add("event", "oahnuy198 说：「建学校」→ 我回：「好」")
        mem_mod.add("thought", "打算用模组方块，别建呆方盒")
        bot, seen = make_bot(make_cfg(Path(".")))
        bot.handle({"from": "oahnuy198", "text": "在吗", "summon": True})
        sys_content = seen["messages"][0]["content"]
        self.assertIn("建学校", sys_content)
        self.assertIn("呆方盒", sys_content)

    def test_history_trim_keeps_pairs(self):
        cfg = make_cfg(Path("."), history_turns=4)
        bot, _ = make_bot(cfg)
        for i in range(6):
            bot._remember("p", f"q{i}", f"a{i}")
        h = bot._hist["p"]
        # 不超过 history_turns+2；且头一条必须是 user（不成半轮）
        self.assertLessEqual(len(h), cfg["llm_api"]["history_turns"] + 2)
        self.assertEqual(h[0]["role"], "user")
        self.assertEqual(h[-1], {"role": "assistant", "content": "a5"})


class HandleFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        br.INBOX = td / "inbox.jsonl"
        br.OUTBOX = td / "outbox.jsonl"
        br.PTR = td / "inbox.ptr"
        mem_mod.MEM_PATH = td / "robot_memory.jsonl"  # 记忆也写临时目录

    def tearDown(self):
        self.tmp.cleanup()

    def _inbox(self, recs):
        br.INBOX.parent.mkdir(parents=True, exist_ok=True)
        with br.INBOX.open("a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_drain_only_summon(self):
        bot, _ = make_bot(make_cfg(Path(".")), reply="@a 好的")
        self._inbox([
            {"from": "a", "text": "你好", "summon": True},
            {"from": "b", "text": "闲聊一句", "summon": False},
        ])
        n = bot.drain()
        self.assertEqual(n, 1)
        out = br.OUTBOX.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(out), 1)
        # ts 是给控制台画时间线用的附加字段，协议本身只认 say/cmd
        self.assertEqual(json.loads(out[0])["say"], "@a 好的")

    def test_transport_error_no_outbox(self):
        def boom(messages):
            raise bl.BrainError("endpoint 挂了")

        bot = bl.Brain(make_cfg(Path(".")), transport=boom)
        self._inbox([{"from": "a", "text": "在吗", "summon": True}])
        self.assertEqual(bot.drain(), 0)
        self.assertFalse(br.OUTBOX.exists())  # 不写 → robot.py 45s 兜底接话

    def test_empty_reply_no_outbox(self):
        bot, _ = make_bot(make_cfg(Path(".")), reply="  ")
        self._inbox([{"from": "a", "text": "在吗", "summon": True}])
        self.assertEqual(bot.drain(), 0)
        self.assertFalse(br.OUTBOX.exists())

    def test_reply_writes_memory_event(self):
        bot, _ = make_bot(make_cfg(Path(".")), reply="@a 好的")
        self._inbox([{"from": "a", "text": "在吗", "summon": True}])
        self.assertEqual(bot.drain(), 1)
        evs = mem_mod.recent(5, "event")
        self.assertEqual(len(evs), 1)
        self.assertIn("在吗", evs[0]["text"])
        self.assertIn("@a 好的", evs[0]["text"])


class WatchLoopTest(unittest.TestCase):
    """watch 必须驻留：一批处理完继续等下一批（修 2026-10-16：旧版处理完即退出）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        br.INBOX = td / "inbox.jsonl"
        br.OUTBOX = td / "outbox.jsonl"
        br.PTR = td / "inbox.ptr"
        mem_mod.MEM_PATH = td / "robot_memory.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_batches_in_one_watch(self):
        bot, _ = make_bot(make_cfg(Path(".")), reply="@a 好")
        br.INBOX.parent.mkdir(parents=True, exist_ok=True)
        with br.INBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"from": "a", "text": "第一声", "summon": True}) + "\n")
        # 1.2s 后补第二声；watch 总共 4s 超时——两批都应被处理，最后返回 2
        import threading

        def later():
            time.sleep(1.2)
            with br.INBOX.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"from": "a", "text": "第二声", "summon": True}) + "\n")

        threading.Thread(target=later, daemon=True).start()
        rc = bot.watch(4.0)
        self.assertEqual(rc, 2)
        out = [json.loads(l) for l in br.OUTBOX.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([o["say"] for o in out], ["@a 好", "@a 好"])


# ---------------- 本地 stub HTTP：真 urllib 全链路 ----------------

class _Stub(BaseHTTPRequestHandler):
    replies = []       # 队列：每次 POST 弹出一条
    payloads = []      # 记录收到的 payload

    def log_message(self, *a):
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._json({"data": [{"id": "deepsink-chat"}, {"id": "deepsink-reasoner"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Stub.payloads.append(body)
        text = _Stub.replies.pop(0) if _Stub.replies else "(stub 空了)"
        self._json({"choices": [{"message": {"role": "assistant", "content": text}}]})


class HttpE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        td = Path(self.tmp.name)
        br.INBOX = td / "inbox.jsonl"
        br.OUTBOX = td / "outbox.jsonl"
        br.PTR = td / "inbox.ptr"
        _Stub.replies = ["@oahnuy198 好，我看看选区。\n[[cmd]]//size"]
        _Stub.payloads = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def test_full_chain(self):
        cfg = make_cfg(Path("."), endpoint=f"http://127.0.0.1:{self.port}/v1")
        bot = bl.Brain(cfg)  # 真 _http_chat
        br.INBOX.parent.mkdir(parents=True, exist_ok=True)
        with br.INBOX.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"from": "oahnuy198", "text": "看看选区", "summon": True}) + "\n")
        self.assertEqual(bot.drain(), 1)
        out = [json.loads(l) for l in br.OUTBOX.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([{k: v for k, v in o.items() if k != "ts"} for o in out],
                         [{"say": "@oahnuy198 好，我看看选区。"}, {"cmd": "//size"}])
        # 请求体形状：非流式 + messages + max_tokens
        p = _Stub.payloads[0]
        self.assertFalse(p["stream"])
        self.assertEqual(p["model"], "deepsink-chat")
        self.assertEqual(p["max_tokens"], 300)
        self.assertEqual(p["messages"][0]["role"], "system")

    def test_endpoint_down_raises(self):
        cfg = make_cfg(Path("."), endpoint="http://127.0.0.1:9/v1", timeout_seconds=2)
        bot = bl.Brain(cfg)
        with self.assertRaises(bl.BrainError):
            bot.ask("在吗")

    def test_probe_ok(self):
        cfg = make_cfg(Path("."), endpoint=f"http://127.0.0.1:{self.port}/v1")
        bot = bl.Brain(cfg)
        self.assertTrue(bot.probe().startswith("ok"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
