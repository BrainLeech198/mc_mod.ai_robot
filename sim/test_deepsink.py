#!/usr/bin/env python3
"""deepsink_server / deepsink_store / deepsink_brain 单测（纯标准库）。运行：python test_deepsink.py

覆盖：
  - 协议：/v1/chat/completions（含流式）、/v1/messages（Anthropic，含流式）、/v1/models、HEAD、/v1/v1 容错
  - 账户：注册 / 登录 / 登出 / /api/me；管理接口必须登录；多用户数据隔离
  - 计费：扣减、流水、余额不足 402
  - 安全：Host 白名单（DNS rebinding）、跨域写请求拒答（CSRF）、页面不内嵌 key
  - 静态页：/ /chat /platform /docs /login
  - bridge：收/回单、超时兜底
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import http.client
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deepsink_server  # noqa: E402
import deepsink_brain  # noqa: E402
import deepsink_bridge  # noqa: E402


def make_cfg(tmp: Path, backend: str, *, timeout: float = 5.0, balance: float = 10.0,
             billing: bool = True) -> dict:
    return {
        "host": "127.0.0.1", "port": 0,
        "data_dir": str(tmp / "DeepSink"),
        "legacy_import_dir": None,
        "allowed_hosts": ["127.0.0.1", "localhost", "::1"],
        "session": {"cookie": "deepsink_session", "ttl_seconds": 3600},
        "model_name": "deepsink-chat", "backend": backend,
        "models": [{"id": "deepsink-chat", "owned_by": "deepsink"},
                   {"id": "deepsink-reasoner", "owned_by": "deepsink"}],
        "bridge": {"req_dir": str(tmp / "req"), "res_dir": str(tmp / "res"),
                   "timeout_seconds": timeout, "poll_interval": 0.1,
                   "msg_file": str(tmp / "msgs.jsonl"),
                   "workers_file": str(tmp / "workers.json"),
                   "fallback_text": "FALLBACK"},
        "openai": {"base_url": "", "api_key_env": "MCROBOT_LLM_KEY", "model": ""},
        "billing": {"enabled": billing, "currency": "CNY", "initial_balance": balance,
                    "price_per_1k_input": 0.002, "price_per_1k_output": 0.008,
                    "chars_per_token": 1.6},
    }


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()

    # ---- 基础 ----
    def start(self, cfg: dict) -> int:
        self.srv = deepsink_server.build_server(cfg)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self.srv.server_address[1]

    def _req(self, port, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers or {})
        if body is not None:
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def _app(self, port, path, body=None, token=None, cookie=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        if cookie:
            h["Cookie"] = cookie
        return self._req(port, path, body, h)

    # ---- 造数据 ----
    def seed(self, email="a@b.c", name="测试用户", pw="secret123", **kw):
        st = self.srv.store
        u = st.create_user(email, name, pw, **kw)
        k = st.create_key(u["id"], "默认密钥")
        return u, k["key"]

    def login(self, port, email="a@b.c", pw="secret123"):
        code, raw, hdrs = self._req(port, "/api/auth/login",
                                    {"email": email, "password": pw})
        self.assertEqual(code, 200, raw)
        return hdrs.get("Set-Cookie", "").split(";")[0]

    def post(self, port, body, token, path="/v1/chat/completions"):
        code, raw, _ = self._app(port, path, body, token=token)
        return code, raw


class ProtocolCase(ServerCase):
    def test_models_and_auth(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        _, key = self.seed()
        code, raw, _ = self._req(port, "/v1/models")
        self.assertEqual(code, 401)
        code, raw, _ = self._app(port, "/v1/models", token=key)
        self.assertEqual(json.loads(raw)["data"][0]["id"], "deepsink-chat")

    def test_bad_key_rejected(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed()
        code, raw = self.post(port, {"messages": [{"role": "user", "content": "hi"}]}, "wrong")
        self.assertEqual(code, 401)

    def test_echo_backend(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        _, key = self.seed()
        code, raw = self.post(port, {"messages": [{"role": "user", "content": "你好世界"}]}, key)
        j = json.loads(raw)
        self.assertEqual(j["choices"][0]["message"]["content"], "你好世界")
        self.assertIn("_billing", j)

    def test_stream_format(self):
        port = self.start(make_cfg(self.tmp, "echo", billing=False))
        _, key = self.seed()
        code, raw, _ = self._app(port, "/v1/chat/completions",
                                 {"stream": True, "messages": [{"role": "user", "content": "流"}]}, token=key)
        self.assertIn("chat.completion.chunk", raw)
        self.assertIn("data: [DONE]", raw)

    def test_anthropic_messages(self):
        port = self.start(make_cfg(self.tmp, "echo", billing=False))
        _, key = self.seed()
        body = {"model": "deepsink-chat", "max_tokens": 64, "system": "你是 DeepSink",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]}
        code, raw, _ = self._req(port, "/v1/messages", body, {"x-api-key": key})
        j = json.loads(raw)
        self.assertEqual(j["type"], "message")
        self.assertEqual(j["content"][0]["text"], "你好")
        self.assertIn("input_tokens", j["usage"])

    def test_anthropic_stream(self):
        port = self.start(make_cfg(self.tmp, "echo", billing=False))
        _, key = self.seed()
        code, raw, _ = self._req(port, "/v1/messages",
                                 {"stream": True, "messages": [{"role": "user", "content": "流"}]},
                                 {"x-api-key": key, "anthropic-version": "2023-06-01"})
        for marker in ("event: message_start", "content_block_delta", "message_stop", "text_delta"):
            self.assertIn(marker, raw)

    def test_v1v1_tolerance_and_head(self):
        port = self.start(make_cfg(self.tmp, "echo", billing=False))
        _, key = self.seed()
        code, raw, _ = self._app(port, "/v1/v1/chat/completions",
                                 {"messages": [{"role": "user", "content": "hi"}]}, token=key)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "hi")
        code, _, _ = self._req(port, "/v1", method="HEAD")
        self.assertEqual(code, 200)


class AccountCase(ServerCase):
    def test_register_login_logout_me(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        code, raw, hdrs = self._req(port, "/api/auth/register",
                                    {"email": "New@Example.com", "name": "小明", "password": "abc12345"})
        self.assertEqual(code, 200, raw)
        j = json.loads(raw)
        self.assertEqual(j["user"]["email"], "new@example.com")   # 归一化小写
        self.assertFalse("pw_hash" in j["user"])                  # 不泄露哈希
        self.assertEqual(j["account"]["balance"], 10.0)           # 注册赠送
        cookie = hdrs["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

        code, raw, _ = self._app(port, "/api/me", cookie=cookie.split(";")[0])
        self.assertEqual(json.loads(raw)["user"]["name"], "小明")

        # 登出后 /api/me 401
        code, raw, hdrs2 = self._req(port, "/api/auth/logout", {}, {"Cookie": cookie.split(";")[0]})
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", hdrs2.get("Set-Cookie", ""))

        code, raw, _ = self._req(port, "/api/auth/login", {"email": "new@example.com", "password": "bad"})
        self.assertEqual(code, 401)
        code, raw, _ = self._req(port, "/api/auth/login", {"email": "new@example.com", "password": "abc12345"})
        self.assertEqual(code, 200)

    def test_register_validation(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        code, _, _ = self._req(port, "/api/auth/register", {"email": "bad", "password": "abc12345"})
        self.assertEqual(code, 400)
        code, _, _ = self._req(port, "/api/auth/register", {"email": "x@y.z", "password": "短"})
        self.assertEqual(code, 400)
        self._req(port, "/api/auth/register", {"email": "x@y.z", "password": "abc12345"})
        code, _, _ = self._req(port, "/api/auth/register", {"email": "x@y.z", "password": "abc12345"})
        self.assertEqual(code, 400)   # 重复注册

    def test_management_requires_login(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed()
        for path in ("/api/account", "/api/keys", "/api/usage", "/api/ledger"):
            code, _, _ = self._req(port, path)
            self.assertEqual(code, 401, f"{path} 应要求登录")

    def test_management_api_with_session(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed("u1@x.y", pw="pw123456")
        ck = self.login(port, "u1@x.y", "pw123456")
        # 创建 key
        code, raw, _ = self._app(port, "/api/keys", {"name": "测试密钥"}, cookie=ck)
        j = json.loads(raw)
        newkey = j["key"]["key"]
        self.assertTrue(newkey.startswith("sk-deepsink-"))
        self.assertEqual(len(j["keys"]), 2)
        # 列表不泄露完整 key
        code, raw, _ = self._app(port, "/api/keys", cookie=ck)
        self.assertNotIn("key\"", raw.split("key_masked")[0])
        # 新 key 可鉴权
        code, raw = self.post(port, {"messages": [{"role": "user", "content": "hi"}]}, newkey)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "hi")
        # 充值 + 余额
        code, raw, _ = self._app(port, "/api/recharge", {"amount": 5}, cookie=ck)
        self.assertGreater(json.loads(raw)["account"]["balance"], 10.0)
        # 流水有记录
        code, raw, _ = self._app(port, "/api/ledger", cookie=ck)
        self.assertTrue(json.loads(raw)["ledger"])

    def test_multi_user_isolation(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed("a@a.a", pw="pw123456")
        self.seed("b@b.b", pw="pw123456")
        ca = self.login(port, "a@a.a", "pw123456")
        cb = self.login(port, "b@b.b", "pw123456")
        _, raw, _ = self._app(port, "/api/keys", cookie=ca)
        self.assertEqual(len(json.loads(raw)["keys"]), 1)
        _, raw, _ = self._app(port, "/api/keys", {"name": "B 的密钥"}, cookie=cb)
        kid = json.loads(raw)["key"]["id"]
        _, raw, _ = self._app(port, "/api/keys", cookie=ca)
        self.assertEqual(len(json.loads(raw)["keys"]), 1, "A 不应看到 B 的密钥")
        # A 不能删 B 的 key
        code, raw, _ = self._app(port, "/api/keys/delete", {"id": kid}, cookie=ca)
        self.assertFalse(json.loads(raw)["ok"])

    def test_api_chat_uses_session(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed("c@c.c", pw="pw123456")
        code, _, _ = self._app(port, "/api/chat", {"messages": [{"role": "user", "content": "喂"}]})
        self.assertEqual(code, 401)          # 未登录
        ck = self.login(port, "c@c.c", "pw123456")
        code, raw, _ = self._app(port, "/api/chat", {"messages": [{"role": "user", "content": "喂"}]}, cookie=ck)
        j = json.loads(raw)
        self.assertEqual(j["reply"], "喂")
        self.assertLess(j["billing"]["balance"], 10.0)


class SecurityCase(ServerCase):
    def test_bad_host_rejected(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        code, _, _ = self._req(port, "/health", headers={"Host": "evil.example.com"})
        self.assertEqual(code, 403)
        code, _, _ = self._req(port, "/health", headers={"Host": "localhost"})
        self.assertEqual(code, 200)

    def test_cross_origin_write_rejected(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed("d@d.d", pw="pw123456")
        ck = self.login(port, "d@d.d", "pw123456")
        code, _, _ = self._app(port, "/api/recharge", {"amount": 1},
                               headers={"Origin": "http://evil.example.com"})
        self.assertEqual(code, 403, "跨域写请求应被拒")
        code, _, _ = self._app(port, "/api/recharge", {"amount": 1}, cookie=ck,
                               headers={"Origin": f"http://127.0.0.1:{port}"})
        self.assertEqual(code, 200, "同源请求应放行")

    def test_options_no_cors(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        code, _, hdrs = self._req(port, "/api/recharge", method="OPTIONS")
        self.assertEqual(code, 204)
        self.assertNotIn("Access-Control-Allow-Origin", hdrs)

    def test_pages_do_not_embed_key(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        _, key = self.seed()
        for path, marker in [("/", "DeepSink"), ("/chat", "对话"), ("/platform", "开放平台"),
                             ("/docs", "快速开始"), ("/login", "注册")]:
            code, html, _ = self._req(port, path)
            self.assertEqual(code, 200, path)
            self.assertIn(marker, html, f"{path} 缺少 {marker}")
            self.assertNotIn("sk-deepsink-", html, f"{path} 不应内嵌任何密钥")
            self.assertNotIn("__API_KEY__", html, f"{path} 占位符未替换")
        code, css, _ = self._req(port, "/assets/style.css")
        self.assertIn("--brand", css)

    def test_health_reports_accounts(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed()
        code, raw, _ = self._req(port, "/health")
        j = json.loads(raw)
        self.assertTrue(j["ok"])
        self.assertEqual(j["accounts"], 1)
        self.assertIn("openai", j["protocols"])

    def test_status_page(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        code, html, _ = self._req(port, "/status")
        self.assertEqual(code, 200)
        self.assertIn("服务状态", html)
        self.assertNotIn("__HOST_PORT__", html, "占位符未替换")
        self.assertNotIn("__BASE_URL__", html, "占位符未替换")

    def test_rate_limit_headers_on_v1(self):
        cfg = make_cfg(self.tmp, "echo")
        cfg["rate_limit"] = {"rpm": 600, "burst": 60}
        port = self.start(cfg)
        _, key = self.seed()
        code, _, hdrs = self._app(port, "/v1/chat/completions",
                                  {"messages": [{"role": "user", "content": "hi"}]}, token=key)
        self.assertEqual(code, 200)
        self.assertEqual(hdrs.get("X-RateLimit-Limit-Requests"), "60")
        self.assertIn("X-RateLimit-Remaining-Requests", hdrs)
        self.assertIn("X-RateLimit-Limit-Requests", hdrs.get("Access-Control-Expose-Headers", ""))


class BillingCase(ServerCase):
    def test_billing_deducts_and_records(self):
        port = self.start(make_cfg(self.tmp, "echo", balance=10.0))
        u, key = self.seed()
        self.post(port, {"messages": [{"role": "user", "content": "计费测试"}]}, key)
        ck = self.login(port, u["email"], "secret123")
        _, raw, _ = self._app(port, "/api/account", cookie=ck)
        d = json.loads(raw)
        self.assertLess(d["account"]["balance"], 10.0)
        self.assertEqual(d["stats"]["requests"], 1)
        self.assertGreater(d["stats"]["tokens"], 0)

    def test_billing_402_when_broke(self):
        port = self.start(make_cfg(self.tmp, "echo", balance=0.0))
        _, key = self.seed()
        code, raw = self.post(port, {"messages": [{"role": "user", "content": "hi"}]}, key)
        self.assertEqual(code, 402)
        self.assertIn("insufficient", raw)


class BridgeCase(ServerCase):
    def test_bridge_roundtrip(self):
        cfg = make_cfg(self.tmp, "bridge", timeout=8.0)
        port = self.start(cfg)
        _, key = self.seed()
        req_dir = Path(cfg["bridge"]["req_dir"]); res_dir = Path(cfg["bridge"]["res_dir"])

        def brain():
            for _ in range(80):
                reqs = list(req_dir.glob("req_*.json")) if req_dir.exists() else []
                if reqs:
                    time.sleep(0.3)
                    rid = reqs[0].stem
                    res_dir.mkdir(parents=True, exist_ok=True)
                    (res_dir / f"{rid}.json").write_text(json.dumps({"text": "AI 的回复"}))
                    return
                time.sleep(0.1)

        threading.Thread(target=brain, daemon=True).start()
        _, raw = self.post(port, {"messages": [{"role": "user", "content": "在吗"}]}, key)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "AI 的回复")

    def test_bridge_timeout_fallback(self):
        cfg = make_cfg(self.tmp, "bridge", timeout=1.0)
        port = self.start(cfg)
        _, key = self.seed()
        _, raw = self.post(port, {"messages": [{"role": "user", "content": "喂"}]}, key)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "FALLBACK")


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = deepsink_server.deepsink_store.Store(self.tmp / "x.db", make_cfg(self.tmp, "echo"))

    def test_password_hashing_roundtrip(self):
        u = self.store.create_user("h@h.h", "H", "topsecret")
        row = self.store._q("SELECT pw_hash,pw_salt,pw_iters FROM users WHERE id=?", (u["id"],))[0]
        self.assertNotIn("topsecret", row["pw_hash"])
        self.assertTrue(self.store.authenticate("h@h.h", "topsecret"))
        self.assertFalse(self.store.authenticate("h@h.h", "wrong"))

    def test_legacy_import(self):
        legacy = self.tmp / "legacy"; legacy.mkdir()
        (legacy / "account.json").write_text(json.dumps(
            {"balance": 7.5, "currency": "CNY", "total_recharged": 7.5, "created": 1}))
        (legacy / "keys.json").write_text(json.dumps(
            [{"id": "key_old", "name": "旧密钥", "key": "sk-deepsink-oldoldold",
              "created": 1, "enabled": True, "requests": 3, "tokens": 9, "cost": 0.1}]))
        got = self.store.import_legacy(legacy)
        self.assertIsNotNone(got)
        self.assertEqual(got["balance"], 7.5)
        self.assertEqual(got["migrated_keys"], 1)
        self.assertTrue(self.store.find_key("sk-deepsink-oldoldold"))
        self.assertEqual(self.store.count_users(), 1)
        # 再导一次不应重复
        self.assertIsNone(self.store.import_legacy(legacy))


class BrainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = {"bridge": {"req_dir": str(self.tmp / "req"), "res_dir": str(self.tmp / "res"),
                          "msg_file": str(self.tmp / "msgs.jsonl"),
                          "workers_file": str(self.tmp / "workers.json")}}
        (self.tmp / "deepsink.json").write_text(json.dumps(cfg))
        self._old_base, self._old_cfg = deepsink_brain.BASE, deepsink_brain.CFG_PATH
        deepsink_brain.BASE = self.tmp
        deepsink_brain.CFG_PATH = self.tmp / "deepsink.json"

    def tearDown(self):
        deepsink_brain.BASE, deepsink_brain.CFG_PATH = self._old_base, self._old_cfg

    def test_pending_then_reply_clears(self):
        req_dir = self.tmp / "req"; req_dir.mkdir(parents=True, exist_ok=True)
        (req_dir / "req_abc.json").write_text(json.dumps({"id": "req_abc", "messages": []}))
        self.assertEqual(len(deepsink_brain._pending(req_dir, self.tmp / "res")), 1)
        deepsink_brain.cmd_reply("req_abc", "好的", None)
        self.assertEqual(len(deepsink_brain._pending(req_dir, self.tmp / "res")), 0)

    def test_reply_json_passthrough(self):
        req_dir = self.tmp / "req"; req_dir.mkdir(parents=True, exist_ok=True)
        (req_dir / "req_x1.json").write_text(json.dumps({"id": "req_x1", "messages": []}))
        deepsink_brain.cmd_reply("x1", None, '{"message":{"role":"assistant","content":"hi"}}')
        saved = json.loads((self.tmp / "res" / "req_x1.json").read_text())
        self.assertEqual(saved["message"]["content"], "hi")

    def test_watch_claims_then_blocks(self):
        req_dir = self.tmp / "req"; req_dir.mkdir(parents=True, exist_ok=True)
        (req_dir / "req_w1.json").write_text(json.dumps({"id": "req_w1", "messages": []}))
        self.assertEqual(deepsink_brain.cmd_watch(2.0, block=True, worker="bw"), 0)
        self.assertTrue((req_dir / "req_w1.lock").exists(), "watch 应认领请求")
        # 唯一请求已被 bw 认领 → 另一个 worker 等待后超时
        self.assertEqual(deepsink_brain.cmd_watch(0.2, block=True, worker="bw2"), 2)


class ConformanceCase(ServerCase):
    """对照主流 AI 平台的协议/账户行为。"""

    def test_request_id_and_security_headers(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        code, _, hdrs = self._req(port, "/health")
        self.assertTrue(hdrs.get("X-Request-Id", "").startswith("req_"))
        code, _, hdrs = self._req(port, "/")
        self.assertIn("Content-Security-Policy", hdrs)
        self.assertEqual(hdrs.get("X-Frame-Options"), "DENY")
        self.assertIn("X-Request-Id", hdrs)

    def test_models_catalog_and_get_by_id(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        _, key = self.seed()
        code, raw, _ = self._app(port, "/v1/models", token=key)
        self.assertIn("deepsink-reasoner", [m["id"] for m in json.loads(raw)["data"]])
        code, raw, _ = self._app(port, "/v1/models/deepsink-reasoner", token=key)
        self.assertEqual(json.loads(raw)["id"], "deepsink-reasoner")
        code, raw, _ = self._app(port, "/v1/models/nope", token=key)
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(raw)["error"]["code"], "model_not_found")

    def test_error_envelope_fields(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed()
        code, raw = self.post(port, {"messages": [{"role": "user", "content": "hi"}]}, "wrong")
        err = json.loads(raw)["error"]
        self.assertEqual(err["type"], "authentication_error")
        self.assertIn("param", err)
        self.assertIn("code", err)

    def test_cors_only_on_v1(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        _, key = self.seed()
        code, _, hdrs = self._req(port, "/v1/chat/completions", method="OPTIONS")
        self.assertEqual(code, 204)
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("Authorization", hdrs.get("Access-Control-Allow-Headers", ""))
        code, _, hdrs = self._app(port, "/v1/chat/completions",
                                  {"messages": [{"role": "user", "content": "hi"}]}, token=key)
        self.assertEqual(hdrs.get("Access-Control-Allow-Origin"), "*")
        code, _, hdrs = self._req(port, "/api/recharge", method="OPTIONS")
        self.assertNotIn("Access-Control-Allow-Origin", hdrs)

    def test_v1_allows_cross_origin_browser_call(self):
        """/v1 靠 key 鉴权、无 Cookie，浏览器跨域调用应放行（与主流平台一致）。"""
        port = self.start(make_cfg(self.tmp, "echo"))
        _, key = self.seed()
        code, raw, _ = self._app(port, "/v1/chat/completions",
                                 {"messages": [{"role": "user", "content": "hi"}]},
                                 token=key, headers={"Origin": "https://app.example.com"})
        self.assertEqual(code, 200, raw)

    def test_body_too_large_rejected(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        _, key = self.seed()
        huge = {"messages": [{"role": "user", "content": "x" * (deepsink_server.MAX_BODY + 100)}]}
        code, raw, _ = self._app(port, "/v1/chat/completions", huge, token=key)
        self.assertEqual(code, 400, raw)

    def test_usage_limit_tolerates_bad_value(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed("sg@x.y", pw="pw123456")
        ck = self.login(port, "sg@x.y", "pw123456")
        for q in ("?limit=abc", "?limit=", "?limit=9999999", ""):
            code, raw, _ = self._app(port, "/api/usage" + q, cookie=ck)
            self.assertEqual(code, 200, f"/api/usage{q} -> {raw}")
            self.assertIn("usage", json.loads(raw))

    def test_rate_limit_429(self):
        cfg = make_cfg(self.tmp, "echo", billing=False)
        cfg["rate_limit"] = {"rpm": 1, "burst": 1}
        port = self.start(cfg)
        _, key = self.seed()
        c1, _ = self.post(port, {"messages": [{"role": "user", "content": "a"}]}, key)
        c2, raw = self.post(port, {"messages": [{"role": "user", "content": "b"}]}, key)
        self.assertEqual(c1, 200)
        self.assertEqual(c2, 429)
        self.assertEqual(json.loads(raw)["error"]["type"], "rate_limit_error")

    def test_stream_multi_chunk(self):
        port = self.start(make_cfg(self.tmp, "echo", billing=False))
        _, key = self.seed()
        code, raw, _ = self._app(port, "/v1/chat/completions",
                                 {"stream": True, "messages": [{"role": "user", "content": "abcdefghijkl"}]},
                                 token=key)
        self.assertGreaterEqual(raw.count('"content"'), 2, "应分多片流式输出")

    def test_max_tokens_truncation(self):
        port = self.start(make_cfg(self.tmp, "echo", billing=False))
        _, key = self.seed()
        long_text = "一" * 200
        code, raw = self.post(port, {"max_tokens": 5, "messages": [{"role": "user", "content": long_text}]}, key)
        j = json.loads(raw)
        self.assertEqual(j["choices"][0]["finish_reason"], "length")
        self.assertLess(len(j["choices"][0]["message"]["content"]), 200)

    def test_anthropic_stop_reason_max_tokens(self):
        port = self.start(make_cfg(self.tmp, "echo", billing=False))
        _, key = self.seed()
        code, raw, _ = self._req(port, "/v1/messages",
                                 {"max_tokens": 5, "messages": [{"role": "user", "content": "一" * 200}]},
                                 {"x-api-key": key})
        self.assertEqual(json.loads(raw)["stop_reason"], "max_tokens")

    def test_change_password_revokes_other_sessions(self):
        port = self.start(make_cfg(self.tmp, "echo"))
        self.seed("pw@x.y", pw="oldpass1")
        ck1 = self.login(port, "pw@x.y", "oldpass1")
        ck2 = self.login(port, "pw@x.y", "oldpass1")
        _, raw, _ = self._app(port, "/api/sessions", cookie=ck1)
        self.assertEqual(len(json.loads(raw)["sessions"]), 2)
        code, raw, _ = self._app(port, "/api/account/password",
                                 {"old": "oldpass1", "new": "newpass2"}, cookie=ck1)
        self.assertEqual(code, 200, raw)
        code, _, _ = self._app(port, "/api/me", cookie=ck2)
        self.assertEqual(code, 401, "改密后其它会话应被踢出")
        code, _, _ = self._app(port, "/api/me", cookie=ck1)
        self.assertEqual(code, 200, "当前会话应保留")
        code, _, _ = self._req(port, "/api/auth/login", {"email": "pw@x.y", "password": "oldpass1"})
        self.assertEqual(code, 401)
        code, _, _ = self._req(port, "/api/auth/login", {"email": "pw@x.y", "password": "newpass2"})
        self.assertEqual(code, 200)

    def test_login_throttle(self):
        cfg = make_cfg(self.tmp, "echo")
        cfg["login_throttle"] = {"max_fails": 3, "window_seconds": 300}
        port = self.start(cfg)
        self.seed("t@x.y", pw="rightpw1")
        for _ in range(3):
            self._req(port, "/api/auth/login", {"email": "t@x.y", "password": "bad"})
        code, _, hdrs = self._req(port, "/api/auth/login", {"email": "t@x.y", "password": "bad"})
        self.assertEqual(code, 429)
        self.assertIn("Retry-After", hdrs)

    def test_models_capability_fields(self):
        cfg = make_cfg(self.tmp, "echo")
        cfg["models"][0].update({"context_window": 128000, "max_output_tokens": 8192,
                                 "capabilities": ["chat", "stream"]})
        port = self.start(cfg)
        _, key = self.seed()
        code, raw, _ = self._app(port, "/v1/models", token=key)
        m = [x for x in json.loads(raw)["data"] if x["id"] == "deepsink-chat"][0]
        self.assertEqual(m["context_window"], 128000)
        self.assertIn("chat", m["capabilities"])
        code, raw, _ = self._app(port, "/v1/models/deepsink-chat", token=key)
        self.assertEqual(json.loads(raw)["max_output_tokens"], 8192)

    def test_api_bridge_snapshot(self):
        port = self.start(make_cfg(self.tmp, "bridge", timeout=5.0))
        self.seed("br@x.y", pw="pw123456")
        code, _, _ = self._req(port, "/api/bridge")
        self.assertEqual(code, 401, "队列观测需登录")
        ck = self.login(port, "br@x.y", "pw123456")
        code, raw, _ = self._app(port, "/api/bridge", cookie=ck)
        self.assertEqual(code, 200, raw)
        b = json.loads(raw)["bridge"]
        self.assertTrue(b["active"])
        self.assertEqual(b["backend"], "bridge")
        for k in ("pending", "claimed", "answered", "workers"):
            self.assertIn(k, b)


class BridgeWorkerCase(unittest.TestCase):
    """多 worker 认领：原子锁、抢占、回单清锁、快照。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.req = self.tmp / "req"
        self.res = self.tmp / "res"
        self.req.mkdir()
        self.res.mkdir()

    def _mk(self, rid, last_user="hi"):
        (self.req / f"{rid}.json").write_text(
            json.dumps({"id": rid, "last_user": last_user}, ensure_ascii=False))

    def test_claim_is_exclusive(self):
        self._mk("req_a")
        d1 = deepsink_bridge.claim_next(self.req, self.res, "w1", 60)
        self.assertTrue(d1["_claimed"])
        self.assertEqual(d1["_worker"], "w1")
        # 已被 w1 认领 → w2 拿不到同一条
        self.assertIsNone(deepsink_bridge.claim_next(self.req, self.res, "w2", 60))

    def test_each_worker_gets_distinct(self):
        self._mk("req_a"); time.sleep(0.02); self._mk("req_b")
        a = deepsink_bridge.claim_next(self.req, self.res, "w1", 60)
        b = deepsink_bridge.claim_next(self.req, self.res, "w2", 60)
        self.assertEqual({a["id"], b["id"]}, {"req_a", "req_b"})
        self.assertEqual(a["_worker"], "w1")
        self.assertEqual(b["_worker"], "w2")

    def test_stale_lock_reclaimed(self):
        self._mk("req_a")
        (self.req / "req_a.lock").write_text(
            json.dumps({"worker": "dead", "ts": time.time() - 9999}))
        d = deepsink_bridge.claim_next(self.req, self.res, "w2", 60)
        self.assertIsNotNone(d, "过期锁应可被抢占")
        self.assertEqual(d["_worker"], "w2")

    def test_snapshot_and_reply_clears_lock(self):
        self._mk("req_a")
        deepsink_bridge.claim_next(self.req, self.res, "w1", 60)
        snap = deepsink_bridge.snapshot(self.req, self.res, 60)
        self.assertEqual(len(snap["claimed"]), 1)
        self.assertEqual(snap["workers"][0]["worker"], "w1")
        self.assertEqual(snap["answered"], 0)
        deepsink_bridge.write_reply(self.req, self.res, "req_a", {"text": "你好"})
        self.assertFalse((self.req / "req_a.lock").exists(), "回单后应清锁")
        self.assertTrue((self.res / "req_a.json").exists())
        snap = deepsink_bridge.snapshot(self.req, self.res, 60)
        self.assertEqual(snap["answered"], 1)
        self.assertEqual(len(snap["claimed"]), 0)

    def test_release_frees_claim(self):
        self._mk("req_a")
        deepsink_bridge.claim_next(self.req, self.res, "w1", 60)
        self.assertTrue(deepsink_bridge.release(self.req, "req_a"))
        # 释放后 w2 可再认领
        d = deepsink_bridge.claim_next(self.req, self.res, "w2", 60)
        self.assertEqual(d["_worker"], "w2")

    def test_answered_not_reclaimed(self):
        self._mk("req_a")
        (self.res / "req_a.json").write_text(json.dumps({"text": "done"}))
        self.assertIsNone(deepsink_bridge.claim_next(self.req, self.res, "w1", 60))


class GatewayCase(ServerCase):
    """网关行为：先开流发首片、等待期间保活、超耐心兜底并清请求文件。"""

    def _gw(self, cfg, **kw):
        g = {"ttft_ms": 0, "ping_ms": 3000, "patience_ms": 30000}
        g.update(kw)
        cfg["gateway"] = g
        return cfg

    def _patch_complete(self, fake):
        old = deepsink_server.complete
        deepsink_server.complete = fake
        self.addCleanup(setattr, deepsink_server, "complete", old)

    def _sse(self, port, path, body, headers=None, stop_marker=b"data: [DONE]",
             total_timeout=25):
        """逐行读 SSE；返回 (status, [(相对秒数, 行字节), ...])。"""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=total_timeout)
        t0 = time.monotonic()
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        status = r.status
        lines = []
        try:
            while True:
                raw = r.readline()
                if not raw:
                    break
                lines.append((time.monotonic() - t0, raw))
                if stop_marker and stop_marker in raw:
                    break
        except Exception:
            pass
        finally:
            conn.close()
        return status, lines

    @staticmethod
    def _oai_content(lines):
        """从 SSE 行解析 OpenAI 流式：返回 (完整内容, 首片(秒数, 首片对象))。"""
        parts, first = [], None
        for t, raw in lines:
            if not raw.startswith(b"data: ") or raw.startswith(b"data: [DONE]"):
                continue
            try:
                obj = json.loads(raw[6:].strip())
            except Exception:
                continue
            if first is None:
                first = (t, obj)
            delta = obj.get("choices", [{}])[0].get("delta", {})
            if isinstance(delta.get("content"), str):
                parts.append(delta["content"])
        return "".join(parts), first

    def test_first_chunk_arrives_before_brain(self):
        cfg = self._gw(make_cfg(self.tmp, "echo", billing=False), patience_ms=8000)
        port = self.start(cfg)
        _, key = self.seed()

        def slow(_cfg, _messages, _model, on_enqueue=None):
            time.sleep(1.5)
            return {"role": "assistant", "content": "大脑迟到的回复"}

        self._patch_complete(slow)
        status, lines = self._sse(port, "/v1/chat/completions",
                                  {"stream": True, "messages": [{"role": "user", "content": "hi"}]},
                                  {"Authorization": f"Bearer {key}"})
        self.assertEqual(status, 200)
        content, first = self._oai_content(lines)
        self.assertIsNotNone(first)
        self.assertEqual(first[1]["choices"][0]["delta"], {"role": "assistant"})  # 首片就是 role
        self.assertLess(first[0], 1.0)                       # 大脑(1.5s)没回就到了
        self.assertEqual(content, "大脑迟到的回复")
        self.assertIn(b"data: [DONE]", b"".join(l for _, l in lines))

    def test_keepalive_comment_before_content(self):
        cfg = self._gw(make_cfg(self.tmp, "echo", billing=False),
                       ping_ms=300, patience_ms=8000)
        port = self.start(cfg)
        _, key = self.seed()

        def slow(_cfg, _messages, _model, on_enqueue=None):
            time.sleep(1.2)
            return {"role": "assistant", "content": "终于来了"}

        self._patch_complete(slow)
        _, lines = self._sse(port, "/v1/chat/completions",
                             {"stream": True, "messages": [{"role": "user", "content": "hi"}]},
                             {"Authorization": f"Bearer {key}"})
        ka_at = next(i for i, (_, l) in enumerate(lines) if b": ka" in l)
        content_at = next(i for i, (_, l) in enumerate(lines) if "终于来了".encode() in l)
        self.assertLess(ka_at, content_at, "保活注释应早于内容")

    def test_stream_patience_fallback(self):
        cfg = self._gw(make_cfg(self.tmp, "echo", billing=False), patience_ms=400)
        port = self.start(cfg)
        _, key = self.seed()

        def stuck(_cfg, _messages, _model, on_enqueue=None):
            time.sleep(5)
            return {"role": "assistant", "content": "太晚了"}

        self._patch_complete(stuck)
        t0 = time.monotonic()
        _, lines = self._sse(port, "/v1/chat/completions",
                             {"stream": True, "messages": [{"role": "user", "content": "hi"}]},
                             {"Authorization": f"Bearer {key}"})
        elapsed = time.monotonic() - t0
        content, _first = self._oai_content(lines)
        self.assertEqual(content, "FALLBACK")
        self.assertIn(b"data: [DONE]", b"".join(l for _, l in lines))
        self.assertLess(elapsed, 3.0, "不该干等大脑那 5 秒")

    def test_patience_removes_request_file(self):
        cfg = self._gw(make_cfg(self.tmp, "bridge", timeout=30.0, billing=False),
                       patience_ms=400)
        port = self.start(cfg)
        _, key = self.seed()
        req_dir = Path(cfg["bridge"]["req_dir"])
        req_dir.mkdir(parents=True, exist_ok=True)
        marker = req_dir / "req_fake.json"
        marker.write_text(json.dumps({"id": "req_fake", "ts": time.time(), "messages": []}))

        def stuck(_cfg, _messages, _model, on_enqueue=None):
            if on_enqueue:
                on_enqueue(marker)          # 模拟入队后失联
            time.sleep(5)
            return {"role": "assistant", "content": "late"}

        self._patch_complete(stuck)
        code, raw = self.post(port, {"messages": [{"role": "user", "content": "hi"}]}, key)
        self.assertEqual(code, 200)
        self.assertIn("FALLBACK", raw)
        self.assertFalse(marker.exists(), "超时应删掉请求文件，防止迟到的回单写成孤儿")

    def test_nonstream_patience_fallback(self):
        cfg = self._gw(make_cfg(self.tmp, "echo", billing=False), patience_ms=400)
        port = self.start(cfg)
        _, key = self.seed()

        def stuck(_cfg, _messages, _model, on_enqueue=None):
            time.sleep(5)
            return {"role": "assistant", "content": "太晚了"}

        self._patch_complete(stuck)
        t0 = time.monotonic()
        code, raw = self.post(port, {"messages": [{"role": "user", "content": "hi"}]}, key)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "FALLBACK")
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_anthropic_stream_event_order(self):
        cfg = self._gw(make_cfg(self.tmp, "echo", billing=False))
        port = self.start(cfg)
        _, key = self.seed()
        _, lines = self._sse(port, "/v1/messages",
                             {"stream": True, "messages": [{"role": "user", "content": "你好啊"}]},
                             {"x-api-key": key}, stop_marker=b"event: message_stop")
        events = [l.split(b"event: ", 1)[1].strip().decode()
                  for _, l in lines if l.startswith(b"event: ")]
        self.assertEqual(events[0], "message_start")
        self.assertEqual(events[1], "content_block_start")
        self.assertIn("ping", events[:4])
        self.assertIn("content_block_delta", events)
        self.assertEqual(events[-3], "content_block_stop")
        self.assertEqual(events[-2], "message_delta")
        self.assertEqual(events[-1], "message_stop")
        self.assertIn("你好啊".encode(), b"".join(l for _, l in lines))


class BrainExpiryCase(unittest.TestCase):
    """过期请求拒回：客户端等待有上限，超龄请求回它只会产生孤儿文件。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = make_cfg(self.tmp, "echo", timeout=5.0)
        cfg["gateway"] = {"patience_ms": 45000}
        (self.tmp / "deepsink.json").write_text(json.dumps(cfg))
        (self.tmp / "req").mkdir(parents=True, exist_ok=True)
        self._old_base, self._old_cfg = deepsink_brain.BASE, deepsink_brain.CFG_PATH
        deepsink_brain.BASE = self.tmp
        deepsink_brain.CFG_PATH = self.tmp / "deepsink.json"

    def tearDown(self):
        deepsink_brain.BASE, deepsink_brain.CFG_PATH = self._old_base, self._old_cfg

    def test_stale_request_refused(self):
        (self.tmp / "req" / "req_old.json").write_text(json.dumps(
            {"id": "req_old", "ts": time.time() - 1000, "messages": []}))
        self.assertEqual(deepsink_brain.cmd_reply("req_old", "迟到的回复", None), 1)
        self.assertFalse((self.tmp / "res" / "req_old.json").exists())

    def test_fresh_request_allowed(self):
        (self.tmp / "req" / "req_new.json").write_text(json.dumps(
            {"id": "req_new", "ts": time.time(), "messages": []}))
        self.assertEqual(deepsink_brain.cmd_reply("req_new", "及时的回复", None), 0)
        self.assertTrue((self.tmp / "res" / "req_new.json").exists())

    def test_missing_ts_still_allowed(self):
        # 历史/测试文件可能没有 ts：不知年龄 → 不拒
        (self.tmp / "req" / "req_nots.json").write_text(json.dumps(
            {"id": "req_nots", "messages": []}))
        self.assertEqual(deepsink_brain.cmd_reply("req_nots", "回复", None), 0)


class CoordCase(unittest.TestCase):
    """多窗口协作设施：黑板留言（say/msgs）+ 心跳在线（ping/online）+ 归属留痕。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"bridge": {"req_dir": str(self.tmp / "req"),
                               "res_dir": str(self.tmp / "res"),
                               "msg_file": str(self.tmp / "msgs.jsonl"),
                               "workers_file": str(self.tmp / "workers.json"),
                               "heartbeat_ttl_seconds": 90}}
        (self.tmp / "deepsink.json").write_text(json.dumps(self.cfg))
        self._old_base, self._old_cfg = deepsink_brain.BASE, deepsink_brain.CFG_PATH
        deepsink_brain.BASE = self.tmp
        deepsink_brain.CFG_PATH = self.tmp / "deepsink.json"

    def tearDown(self):
        deepsink_brain.BASE, deepsink_brain.CFG_PATH = self._old_base, self._old_cfg

    def test_say_and_read_msgs(self):
        deepsink_brain.cmd_say("req_9 我在处理，是长对话", worker="codebuddy-main")
        deepsink_brain.cmd_say("收到，前面的我不管", worker="doubao-web-1")
        msgs = deepsink_bridge.read_msgs(self.cfg, 20)
        self.assertEqual([m["worker"] for m in msgs], ["codebuddy-main", "doubao-web-1"])
        self.assertEqual(msgs[-1]["text"], "收到，前面的我不管")
        self.assertEqual(len(deepsink_bridge.read_msgs(self.cfg, 1)), 1, "tail 应只取最近 N 条")
        # 文件不存在 → 空列表，不炸
        missing = {"bridge": {"msg_file": str(self.tmp / "nope.jsonl")}}
        self.assertEqual(deepsink_bridge.read_msgs(missing, 20), [])

    def test_ping_and_online(self):
        deepsink_bridge.ping(self.cfg, "codebuddy-main")
        self.assertEqual([w["worker"] for w in deepsink_bridge.online_workers(self.cfg)],
                         ["codebuddy-main"])
        wp = deepsink_bridge.workers_path(self.cfg)
        data = json.loads(wp.read_text(encoding="utf-8"))
        data["codebuddy-main"]["last_seen"] = time.time() - 1000   # 伪造失联
        wp.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(deepsink_bridge.online_workers(self.cfg), [])

    def test_snapshot_includes_online_and_pid(self):
        req_dir = self.tmp / "req"
        req_dir.mkdir(parents=True)
        (req_dir / "req_c1.json").write_text(json.dumps({"id": "req_c1", "messages": []}))
        self.assertIsNotNone(deepsink_bridge.claim_next(req_dir, self.tmp / "res",
                                               "codebuddy-main", 180))
        deepsink_bridge.ping(self.cfg, "codebuddy-main")
        snap = deepsink_bridge.snapshot(req_dir, self.tmp / "res", 180,
                               workers_file=deepsink_bridge.workers_path(self.cfg),
                               hb_cfg=self.cfg)
        self.assertEqual(snap["claimed"][0]["worker"], "codebuddy-main")
        self.assertIn("pid", snap["claimed"][0], "锁应记录 pid 做活体排查")
        self.assertEqual([w["worker"] for w in snap["online"]], ["codebuddy-main"])

    def test_watch_and_reply_auto_ping_and_attribution(self):
        req_dir = self.tmp / "req"
        req_dir.mkdir(parents=True, exist_ok=True)
        (req_dir / "req_h1.json").write_text(json.dumps(
            {"id": "req_h1", "messages": [], "ts": time.time()}))
        self.assertEqual(deepsink_brain.cmd_watch(1.0, worker="workbuddy-1"), 0)
        wp = deepsink_bridge.workers_path(self.cfg)
        self.assertIn("workbuddy-1", json.loads(wp.read_text(encoding="utf-8")),
                      "watch 启动应自动登记在线")
        deepsink_brain.cmd_reply("req_h1", "好的", None, worker="workbuddy-1")
        saved = json.loads((self.tmp / "res" / "req_h1.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["_worker"], "workbuddy-1", "回单应带归属留痕")


class QueueHygieneCase(unittest.TestCase):
    """队列卫生：没人会读答案时（complete 超时 / 客户端断开），请求文件与认领锁都必须清掉。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"backend": "bridge",
                    "bridge": {"req_dir": str(self.tmp / "req"),
                               "res_dir": str(self.tmp / "res"),
                               "timeout_seconds": 0.5, "poll_interval": 0.05},
                    "gateway": {"patience_ms": 300, "ping_ms": 150}}

    def _ghost_worker(self, stop):
        """模拟"接了单就消失"的 worker：请求一落盘就认领，永不回复。"""
        req_dir = self.tmp / "req"
        while not stop.is_set():
            for p in req_dir.glob("req_*.json"):
                deepsink_bridge.claim_one(req_dir, self.tmp / "res",
                                          p.stem, "ghost", 180)
            time.sleep(0.01)

    def _spawn_ghost(self):
        stop = threading.Event()
        t = threading.Thread(target=self._ghost_worker, args=(stop,), daemon=True)
        t.start()
        return stop, t

    def test_complete_timeout_cleans_req_and_lock(self):
        req_dir = self.tmp / "req"
        stop, t = self._spawn_ghost()
        try:
            out = deepsink_server.bridge_complete(
                self.cfg, [{"role": "user", "content": "hi"}], "deepsink-chat")
        finally:
            stop.set(); t.join(timeout=1)
        self.assertTrue(out.get("_timeout"), out)
        self.assertEqual(list(req_dir.glob("req_*.json")), [], "超时后不应留请求文件")
        self.assertEqual(list(req_dir.glob("*.lock")), [], "超时后不应留认领锁")

    def test_client_disconnect_cleans_req_and_lock(self):
        req_dir = self.tmp / "req"
        stop, t = self._spawn_ghost()

        def emit_ka():
            raise ConnectionError("client gone")     # 保活写失败 = 客户端已断开

        try:
            msg, timed_out, err = deepsink_server.Handler._await_brain(
                None, self.cfg, [{"role": "user", "content": "hi"}],
                "deepsink-chat", emit_ka=emit_ka)
        finally:
            stop.set(); t.join(timeout=1)
        self.assertTrue(timed_out)
        self.assertIsNone(msg)
        self.assertEqual(list(req_dir.glob("req_*.json")), [], "断开后不应留请求文件")
        # 竞竞态下 ghost 可能在清理瞬间又落一把锁——下一个 worker 的轮询应自愈扫掉
        self.assertIsNone(deepsink_bridge.claim_next(req_dir, self.tmp / "res", "sweeper", 180))
        self.assertEqual(list(req_dir.glob("*.lock")), [], "孤儿认领锁应被自愈清理")


class KeyHashCase(unittest.TestCase):
    """B2：API key 只存哈希 + 掩码；旧库（明文 key 列）启动时自动迁移。"""

    OLD_SCHEMA = """
    CREATE TABLE users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
      pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL, pw_iters INTEGER NOT NULL,
      created REAL NOT NULL, balance REAL NOT NULL DEFAULT 0,
      total_recharged REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'CNY',
      is_admin INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE api_keys (
      id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
      key TEXT UNIQUE NOT NULL, created REAL NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1, requests INTEGER NOT NULL DEFAULT 0,
      tokens INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0);
    """

    def _store(self, tmp: Path):
        st = deepsink_server.deepsink_store
        return st.Store(tmp / "DeepSink" / "deepsink.db", {"billing": {}})

    def test_db_stores_hash_not_plaintext(self):
        tmp = Path(tempfile.mkdtemp())
        s = self._store(tmp)
        u = s.create_user("a@b.c", "测试", "secret123")
        k = s.create_key(u["id"], "k1")
        row = s._q("SELECT key_hash, key_disp FROM api_keys")[0]
        st = deepsink_server.deepsink_store
        self.assertEqual(row["key_hash"], st.hash_key(k["key"]))
        self.assertEqual(row["key_disp"], st.mask_key(k["key"]))
        self.assertIn("•", row["key_disp"])
        # 明文只在创建时返回过一次，之后任何接口都拿不到
        self.assertTrue(k["key"].startswith("sk-deepsink-"))
        pub = s.public_keys(u["id"])[0]
        self.assertNotIn("key", pub)
        self.assertNotIn(k["key"], json.dumps(pub, ensure_ascii=False))
        # 校验走哈希命中
        self.assertIsNotNone(s.find_key(k["key"]))
        self.assertIsNone(s.find_key("sk-deepsink-wrong"))
        self.assertIsNone(s.find_key("not-a-key"))

    def test_legacy_plaintext_db_migrates_on_start(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "DeepSink" / "deepsink.db"
        db.parent.mkdir(parents=True)
        plain = "sk-deepsink-legacykey0001"
        conn = sqlite3.connect(str(db))
        conn.executescript(self.OLD_SCHEMA)
        conn.execute("INSERT INTO users(id,email,name,pw_salt,pw_hash,pw_iters,created,"
                     "balance,enabled) VALUES(1,'old@x.y','旧','s','h',1,1,10,1)")
        conn.execute("INSERT INTO api_keys(id,user_id,name,key,created,enabled,requests,"
                     "tokens,cost) VALUES('key_old',1,'旧密钥',?,1,1,3,9,0.1)", (plain,))
        conn.commit(); conn.close()
        # 新代码启动即迁移
        s = self._store(tmp)
        cols = {r["name"] for r in s._q("PRAGMA table_info(api_keys)")}
        self.assertIn("key_hash", cols)
        self.assertNotIn("key", cols, "明文列应被表重建替换掉")
        self.assertIsNotNone(s.find_key(plain), "迁移后旧 key 必须仍能校验")
        row = s._q("SELECT * FROM api_keys")[0]
        self.assertNotIn(plain, json.dumps(row, ensure_ascii=False))
        self.assertEqual(row["requests"], 3, "迁移应保留用量统计")
        # 幂等：再开一次不应炸
        s2 = self._store(tmp)
        self.assertIsNotNone(s2.find_key(plain))


if __name__ == "__main__":
    unittest.main(verbosity=2)
