"""视觉层单测：请求形状、错误提示、探活、look 工具。

用本地 stub HTTP（和 test_brain_llm 一样），不去真的连多模态后端。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mcrobot import vision
from mcrobot.body import bridge as cc
from mcrobot.tools.registry import CallContext


class _Stub(BaseHTTPRequestHandler):
    reply = "画面里是一面石墙，右边缺两格。"
    status = 200
    raw = None          # 非空则原样当响应体（测推理模型只给 reasoning 的情况）
    payloads: list[dict] = []
    auth: list[str] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Stub.payloads.append(body)
        _Stub.auth.append(self.headers.get("Authorization") or "")
        if _Stub.status != 200:
            data = b'{"error":{"message":"image input is not supported"}}'
            self.send_response(_Stub.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if _Stub.raw is not None:
            data = json.dumps(_Stub.raw, ensure_ascii=False).encode()
        else:
            data = json.dumps({"choices": [{"message": {"content": _Stub.reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _cfg(endpoint: str, **over) -> dict:
    c = {"enabled": True, "endpoint": endpoint, "model": "vlm-test",
         "key_env": "MCROBOT_VISION_KEY", "max_width": 854, "detail": "auto",
         "timeout": 5.0, "max_tokens": 300}
    c.update(over)
    return c


class VisionHttpTest(unittest.TestCase):
    def setUp(self):
        _Stub.reply = "画面里是一面石墙，右边缺两格。"
        _Stub.status = 200
        _Stub.raw = None
        _Stub.payloads = []
        _Stub.auth = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cfg_patch = mock.patch.object(
            vision, "cfg", lambda version=None: _cfg(f"http://127.0.0.1:{self.port}/v1"))
        self.cfg_patch.start()
        self.addCleanup(self.cfg_patch.stop)
        key_patch = mock.patch.dict(os.environ, {"MCROBOT_VISION_KEY": "test-key"})
        key_patch.start()
        self.addCleanup(key_patch.stop)

    def tearDown(self):
        self.srv.shutdown()

    def test_request_is_multimodal(self):
        out = vision.describe_bytes(vision.TINY_PNG, "这墙直吗")
        self.assertIn("石墙", out["text"])
        p = _Stub.payloads[0]
        self.assertEqual(p["model"], "vlm-test")
        self.assertFalse(p["stream"])
        parts = p["messages"][0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "这墙直吗"})
        img = parts[1]["image_url"]
        self.assertTrue(img["url"].startswith("data:image/png;base64,"))
        self.assertEqual(img["detail"], "auto")
        self.assertTrue(_Stub.auth[0].startswith("Bearer "))

    def test_default_question_used(self):
        vision.describe_bytes(vision.TINY_PNG)
        text = _Stub.payloads[0]["messages"][0]["content"][0]["text"]
        self.assertIn("Minecraft", text)

    def test_http_400_gives_actionable_hint(self):
        _Stub.status = 400
        with self.assertRaises(vision.VisionError) as cm:
            vision.describe_bytes(vision.TINY_PNG)
        msg = str(cm.exception)
        self.assertIn("HTTP 400", msg)
        self.assertIn("不支持图片", msg)

    def test_check_ok(self):
        out = vision.check()
        self.assertIn("石墙", out["text"])
        self.assertIn("total_ms", out)

    def test_reasoning_only_answer_is_explained(self):
        """MiMo 这类推理模型会把 max_tokens 花在 reasoning_content 上，content 空。"""
        _Stub.raw = {"choices": [{"finish_reason": "length",
                                  "message": {"content": "",
                                              "reasoning_content": "让我看看……"}}]}
        with self.assertRaises(vision.VisionError) as cm:
            vision.describe_bytes(vision.TINY_PNG)
        self.assertIn("max_tokens", str(cm.exception))


class ConfigTest(unittest.TestCase):
    def test_disabled_refuses(self):
        with mock.patch.object(vision, "cfg", lambda version=None: _cfg("http://x/v1", enabled=False)):
            with self.assertRaises(vision.VisionError) as cm:
                vision.describe_bytes(vision.TINY_PNG)
        self.assertIn("未启用", str(cm.exception))

    def test_check_runs_even_when_disabled(self):
        """探活要先于开关：还没启用时就得能问上游吃不吃图。"""
        _Stub.status = 200
        _Stub.raw = None
        _Stub.reply = "收到"
        _Stub.payloads = []
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        with mock.patch.object(vision, "cfg",
                               lambda version=None: _cfg(f"http://127.0.0.1:{port}/v1",
                                                         enabled=False)), \
             mock.patch.dict(os.environ, {"MCROBOT_VISION_KEY": "test-key"}):
            out = vision.check()
        self.assertIn("收到", out["text"])
        self.assertTrue(_Stub.payloads)

    def test_missing_endpoint_or_model(self):
        with mock.patch.object(vision, "cfg",
                               lambda version=None: _cfg("", model="")):
            with self.assertRaises(vision.VisionError) as cm:
                vision.describe_bytes(vision.TINY_PNG)
        self.assertIn("endpoint", str(cm.exception))

    def test_missing_key(self):
        with mock.patch.object(vision, "cfg", lambda version=None: _cfg("http://127.0.0.1:9/v1")):
            with mock.patch.dict(os.environ, {"MCROBOT_VISION_KEY": "", "MCROBOT_LLM_KEY": ""}):
                with self.assertRaises(vision.VisionError) as cm:
                    vision.describe_bytes(vision.TINY_PNG)
        self.assertIn("key", str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(vision.VisionError) as cm:
            vision.describe("/nonexistent/shot.png")
        self.assertIn("不存在", str(cm.exception))

    def test_too_large(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"x" * 32)
            path = f.name
        self.addCleanup(os.unlink, path)
        with mock.patch.object(vision, "MAX_IMAGE_BYTES", 16):
            with self.assertRaises(vision.VisionError) as cm:
                vision.describe(path)
        self.assertIn("太大", str(cm.exception))


class LookToolTest(unittest.TestCase):
    def setUp(self):
        from mcrobot.tools.builtin import build_registry
        self.reg = build_registry()
        self.shot = {"path": "instance/robot/shots/x.png", "abs_path": "/tmp/x.png",
                     "width": 854, "height": 480, "bytes": 1234, "capture_ms": 60}

    def test_tool_registered(self):
        self.assertIn("look", self.reg.names())
        self.assertEqual(len(self.reg.names()), 27)

    def test_look_returns_description(self):
        with mock.patch.object(cc, "screenshot", lambda max_width=854: self.shot), \
             mock.patch.object(vision, "describe",
                               lambda path, q="", version=None: {"text": "墙是直的，但少一格",
                                                                 "model": "vlm", "ms": 900,
                                                                 "path": path}):
            r = self.reg.call("look", {"question": "墙直吗"}, CallContext(actor="tester"))
        self.assertTrue(r.ok, r.error)
        self.assertIn("少一格", r.text)
        self.assertEqual(r.data["width"], 854)
        self.assertEqual(r.data["model"], "vlm")

    def test_look_not_configured_is_clear(self):
        def boom(path, q="", version=None):
            raise vision.VisionError("vision 未启用")
        with mock.patch.object(cc, "screenshot", lambda max_width=854: self.shot), \
             mock.patch.object(vision, "describe", boom):
            r = self.reg.call("look", {}, CallContext(actor="tester"))
        self.assertFalse(r.ok)
        self.assertIn("未启用", r.error)
        self.assertIn("x.png", r.error)      # 图还是存下来了，人能自己看

    def test_look_screenshot_failure(self):
        def boom(max_width=854):
            raise cc.ControlError("客户端没连上")
        with mock.patch.object(cc, "screenshot", boom):
            r = self.reg.call("look", {}, CallContext(actor="tester"))
        self.assertFalse(r.ok)
        self.assertIn("没连上", r.error)


if __name__ == "__main__":
    unittest.main()
