#!/usr/bin/env python3
"""DeepSink — 本机部署的 AI 服务平台（OpenAI + Anthropic 双协议 + 账户系统）。

把本机变成一台完整的 AI 服务平台：官网 / 网页版对话 / 开放平台控制台 / API 文档 / 状态页，
外加注册登录、多用户 API key、额度计费充值；对外同时说两套协议，
任何只认其中之一的客户端（MC 模组、SDK）把 base_url 指过来就能获得 AI 能力：

  OpenAI 协议     POST /v1/chat/completions   GET /v1/models
  Anthropic 协议  POST /v1/messages           （含 SSE 流式）

账户系统
  - 控制台（网页）走「会话 Cookie」：POST /api/auth/register|login|logout，GET /api/me
  - /api/* 管理接口一律要求已登录，且按用户隔离（看不到别人的 key / 余额 / 用量）
  - /v1/* 走 API key 鉴权（Bearer 或 x-api-key），计费记在 key 的归属账户上
  - 数据落 SQLite：`<data_dir>/deepsink.db`，默认 `~/DeepSink/deepsink.db`

安全（本服务默认只绑 127.0.0.1，再做三层加固）
  - Host 白名单：只接受 127.0.0.1 / localhost / [::1]，挡 DNS rebinding
  - Origin 同源校验：跨域写请求一律 403，挡 CSRF
  - OPTIONS 返回 204 且不下发 CORS 许可；页面不再内嵌完整 key

后端（`deepsink.json:backend`）决定"谁来思考"：
  - bridge：把请求交给推理 worker（用 `deepsink_brain.py` 收单/回单）——默认
  - openai：透传到真正 OpenAI 兼容 API（DeepSeek 等，key 走环境变量）
  - echo  ：本地回显（自测）

用法（在 sim/ 目录下）：
  python deepsink_server.py serve [--backend X] [--port N]
  python deepsink_server.py info
  python deepsink_server.py client "你好" [--stream]
  python deepsink_server.py users                      # 列出账户
  python deepsink_server.py adduser --email a@b.c --password secret [--name 名] [--admin]
  python deepsink_server.py passwd --email a@b.c --password newsecret
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.request
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent          # = sim/
sys.path.insert(0, str(BASE))
import deepsink_bridge  # noqa: E402
import deepsink_store  # noqa: E402

CFG_PATH = BASE / "deepsink.json"
WEB_DIR = BASE / "web"

CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
          ".ico": "image/x-icon", ".png": "image/png", ".json": "application/json; charset=utf-8"}

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 1 << 20    # 请求体上限 1 MiB：本机服务足够，挡超大 body 撑爆内存
MAX_DRAIN = 16 << 20  # 超限时最多再丢弃这么多字节的 body，保证客户端能干净地收到错误响应

# 响应头：安全加固（页面）与 CORS（仅 /v1 API）。
CSP_HTML = ("default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")
CORS_METHODS = "GET, POST, OPTIONS"
CORS_HEADERS = "Authorization, Content-Type, x-api-key, anthropic-version, anthropic-beta"


def model_ids(cfg: dict) -> list[str]:
    """对外模型目录：优先读 cfg['models']，否则回落到单个 model_name。"""
    ms = cfg.get("models")
    if isinstance(ms, list) and ms:
        out = []
        for m in ms:
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if mid:
                out.append(str(mid))
        if out:
            return out
    return [cfg.get("model_name") or "deepsink-chat"]


# models 条目里可选的"能力"字段：有就透传，无则省略（对齐主流平台的 /v1/models 输出）
MODEL_META_FIELDS = ("context_window", "max_output_tokens", "capabilities", "description")


def model_obj(cfg: dict, mid: str, created: int = 1700000000) -> dict:
    """单个模型的对外描述对象。"""
    obj = {"id": mid, "object": "model", "created": created, "owned_by": "deepsink"}
    for m in cfg.get("models") or []:
        if isinstance(m, dict) and m.get("id") == mid:
            for k in MODEL_META_FIELDS:
                if k in m:
                    obj[k] = m[k]
            break
    return obj


def _chunks(text: str, size: int) -> list[str]:
    """把文本切成小片，用于逐 token 流式输出。"""
    if not text:
        return []
    size = max(1, int(size))
    return [text[i:i + size] for i in range(0, len(text), size)]


def _oai_chunk(cid: str, model: str, created: int, delta: dict,
               finish_r=None, extra: dict | None = None) -> bytes:
    """OpenAI 流式分片的 SSE 字节（chat.completion.chunk）。"""
    obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
           "choices": [{"index": 0, "delta": delta, "finish_reason": finish_r}]}
    if extra:
        obj.update(extra)
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def _anth_ev(event: str, data: dict) -> bytes:
    """Anthropic 流式事件的 SSE 字节（event: xxx + data: {...}）。"""
    return (f"event: {event}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")


class RateLimiter:
    """按 key 的令牌桶限流（RPM）。rpm<=0 表示不限流。"""

    def __init__(self, rpm: int = 0, burst: int = 0):
        self.rate = max(0.0, float(rpm or 0)) / 60.0    # 每秒补充多少令牌
        self.burst = max(1.0, float(burst or rpm or 1))
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """返回 (是否放行, 建议 Retry-After 秒)。"""
        if self.rate <= 0:
            return True, 0.0
        now = time.monotonic()
        with self._lock:
            tok, last = self._state.get(key, (self.burst, now))
            tok = min(self.burst, tok + (now - last) * self.rate)
            if tok < 1.0:
                self._state[key] = (tok, now)
                return False, max(1.0, (1.0 - tok) / self.rate)
            self._state[key] = (tok - 1.0, now)
            return True, 0.0

    def snapshot(self, key: str) -> tuple[int, int] | None:
        """返回 (限额, 剩余令牌) 供 X-RateLimit-* 响应头；不限流时返回 None。"""
        if self.rate <= 0:
            return None
        with self._lock:
            tok, _ = self._state.get(key, (self.burst, time.monotonic()))
        return int(self.burst), max(0, int(tok))


class LoginThrottle:
    """登录失败节流：同一邮箱连续失败 N 次后锁一段时间。"""

    def __init__(self, max_fails: int = 8, window: float = 300.0):
        self.max_fails = max(0, int(max_fails))
        self.window = float(window)
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ident: str) -> float:
        """返回还需等待的秒数（0 表示未锁）。"""
        if self.max_fails <= 0:
            return 0.0
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(ident.lower(), []) if now - t < self.window]
            self._fails[ident.lower()] = hits
            if len(hits) >= self.max_fails:
                return max(1.0, self.window - (now - hits[0]))
        return 0.0

    def record_fail(self, ident: str) -> None:
        if self.max_fails <= 0:
            return
        with self._lock:
            self._fails.setdefault(ident.lower(), []).append(time.time())

    def clear(self, ident: str) -> None:
        with self._lock:
            self._fails.pop(ident.lower(), None)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def data_root(cfg: dict) -> Path:
    """账本目录（默认 ~/DeepSink）。支持 ~ 与相对路径。"""
    raw = cfg.get("data_dir") or "~/DeepSink"
    p = Path(os.path.expanduser(str(raw)))
    return p if p.is_absolute() else (BASE / p)


def db_path(cfg: dict) -> Path:
    return data_root(cfg) / "deepsink.db"


# ----------------------- 消息归一化（两套协议 → 内部） -----------------------
def _text_of(content) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def _last_user(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return _text_of(m.get("content"))
    return ""


def _norm_anthropic(body: dict) -> tuple[list, str]:
    msgs = []
    sys_txt = _text_of(body.get("system"))
    if sys_txt:
        msgs.append({"role": "system", "content": sys_txt})
    for m in body.get("messages", []) or []:
        msgs.append({"role": m.get("role", "user"), "content": _text_of(m.get("content"))})
    return msgs, sys_txt


# ----------------------------- 页面渲染 -----------------------------
def render_page(name: str, cfg: dict, host: str | None = None, port: int | None = None) -> str:
    """渲染 web/<name>：替换运行时占位符。**绝不注入 API key。**"""
    tpl_path = WEB_DIR / name
    if not tpl_path.exists():
        return f"<h1>DeepSink</h1><p>未找到页面 web/{name}。</p>"
    html = tpl_path.read_text(encoding="utf-8")
    host = host or cfg["host"]
    port = port or cfg["port"]
    repl = {
        "__BASE_URL__": f"http://{host}:{port}/v1",
        "__A_BASE_URL__": f"http://{host}:{port}",
        "__HOST_PORT__": f"{host}:{port}",
        "__MODEL__": cfg["model_name"], "__BACKEND__": cfg["backend"],
        "__CURRENCY__": cfg.get("billing", {}).get("currency", "CNY"),
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


# ----------------------------- 后端 -----------------------------
def bridge_complete(cfg: dict, messages: list, model: str, on_enqueue=None) -> dict:
    """把请求交给推理 worker：写请求文件 → 轮询回复文件 → 返回 {"text"|"message"}。

    on_enqueue(req_path)：请求文件刚落盘时的回调，供处理器记住它——
    等待超时后删掉该文件，迟到的回单就会失败，而不会写成没人消费的孤儿文件。
    """
    b = cfg["bridge"]
    req_dir = BASE / b["req_dir"]
    res_dir = BASE / b["res_dir"]
    req_dir.mkdir(parents=True, exist_ok=True)
    res_dir.mkdir(parents=True, exist_ok=True)
    rid = "req_" + uuid.uuid4().hex[:12]
    req = {"id": rid, "ts": time.time(), "model": model,
           "messages": messages, "last_user": _last_user(messages)}
    req_path = req_dir / f"{rid}.json"
    req_path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[deepsink] bridge 请求入队 {rid}: {req['last_user'][:80]!r}", file=sys.stderr)
    if on_enqueue:
        try:
            on_enqueue(req_path)
        except Exception:
            pass
    res_path = res_dir / f"{rid}.json"
    deadline = time.monotonic() + float(b.get("timeout_seconds", 120))
    poll = float(b.get("poll_interval", 0.5))
    while time.monotonic() < deadline:
        if res_path.exists():
            try:
                data = json.loads(res_path.read_text(encoding="utf-8"))
            except Exception:
                data = {"text": ""}
            try:
                res_path.unlink()
            except Exception:
                pass
            # 闭环：删掉请求文件（含认领锁），已完成的请求不在队列里留影子
            for p in (req_path, req_dir / f"{rid}.lock"):
                try:
                    p.unlink()
                except OSError:
                    pass
            return data or {"text": ""}
        time.sleep(poll)
    # 超时仍无人回复：同样清掉请求文件与认领锁——客户端早已拿到兜底文案，
    # 死单不该在队列里留影子（迟到的回单会因找不到请求而失败，不会写成孤儿文件）
    for p in (req_path, req_dir / f"{rid}.lock"):
        try:
            p.unlink()
        except OSError:
            pass
    return {"text": b.get("fallback_text", "…"), "_timeout": True}


def openai_complete(cfg: dict, messages: list, model: str) -> dict:
    o = cfg.get("openai", {})
    base = (o.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("openai.base_url 未配置（请填好并把 backend 改成 openai）")
    key = os.environ.get(o.get("api_key_env", "MCROBOT_LLM_KEY"), "")
    payload = {"model": o.get("model") or model, "messages": messages, "stream": False}
    req = urllib.request.Request(
        base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    return {"message": data["choices"][0]["message"]}


def complete(cfg: dict, messages: list, model: str, on_enqueue=None) -> dict:
    """统一入口：按 backend 拿到 assistant message（{role,content[,tool_calls]}）。"""
    if cfg["backend"] == "echo":
        return {"role": "assistant", "content": _last_user(messages) or "(echo)"}
    if cfg["backend"] == "openai":
        return openai_complete(cfg, messages, model).get("message", {})
    data = bridge_complete(cfg, messages, model, on_enqueue=on_enqueue)
    return data.get("message") or {"role": "assistant", "content": data.get("text", "")}


class ApiError(Exception):
    def __init__(self, code: int, message: str, etype: str = "invalid_request_error"):
        super().__init__(message)
        self.code, self.message, self.etype = code, message, etype


# ----------------------------- HTTP 层 -----------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "DeepSink/0.4"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[deepsink] {self.address_string()} {fmt % args}", file=sys.stderr)

    # ---- 安全守卫 ----
    def _begin(self):
        """每个请求起始：生成 request id（响应头回带，便于排障）。"""
        self._rid = "req_" + secrets.token_hex(12)
        self._rl: dict = {}   # /v1 成功响应附带的限流余量头（由 _gate 填充）

    def _allowed_hosts(self) -> set:
        hs = self.server.cfg.get("allowed_hosts") or DEFAULT_ALLOWED_HOSTS
        return {str(h).strip().lower() for h in hs}

    def _host_ok(self) -> bool:
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return False
        host = raw.rsplit(":", 1)[0] if not raw.startswith("[") else raw.split("]")[0] + "]"
        host = host.strip("[]").lower()
        return host in {h.strip("[]") for h in self._allowed_hosts()}

    def _addr(self) -> tuple[str, int]:
        """实际监听的 (host, port)；port=0 时取真实绑定端口。"""
        try:
            h, p = self.server.server_address[:2]
            return str(h), int(p)
        except Exception:
            return self.server.cfg["host"], int(self.server.cfg["port"])

    def _origin_ok(self) -> bool:
        """跨域写请求一律拒（挡 CSRF）。无 Origin 视为非浏览器调用，放行。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host, port = self._addr()
        ok = {f"http://{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]", host)}
        return origin.rstrip("/") in ok

    def _wants_cors(self) -> bool:
        """仅 /v1 API 端点对浏览器开放 CORS；控制台 /api/* 维持同源锁定。"""
        return self._norm_path().startswith("/v1")

    def _guard(self) -> bool:
        """返回 True 表示放行；否则已回复错误。"""
        if not self._host_ok():
            self._oai_err(403, "forbidden host", "invalid_request_error")
            return False
        # Origin 同源校验只针对「靠 Cookie 鉴权」的写请求（挡 CSRF）。
        # /v1/* 靠 API key 鉴权、无 Cookie，没有 CSRF 面，浏览器跨域调用应放行
        # （与主流平台一致：API 端点对浏览器开放 CORS）。
        if (self.command in ("POST", "PUT", "PATCH", "DELETE")
                and not self._wants_cors() and not self._origin_ok()):
            self._oai_err(403, "cross-origin request rejected", "invalid_request_error")
            return False
        return True

    # ---- 错误信封（对齐 OpenAI / Anthropic 规范）----
    def _oai_err(self, code: int, message: str, etype: str = "invalid_request_error",
                 param=None, ec=None, head_only: bool = False, extra: dict | None = None):
        self._send_json(code, {"error": {"message": message, "type": etype,
                                         "param": param, "code": ec}}, head_only, extra=extra)

    def _anth_err(self, code: int, message: str, etype: str = "invalid_request_error",
                  extra: dict | None = None):
        self._send_json(code, {"type": "error",
                               "error": {"type": etype, "message": message}}, extra=extra)

    # ---- 结果按 max_tokens 截断（仅当客户端显式给了 max_tokens）----
    @staticmethod
    def _apply_max_tokens(store, content: str, max_tokens) -> tuple[str, bool]:
        try:
            mt = int(max_tokens)
        except (TypeError, ValueError):
            return content, False
        if mt <= 0:
            return content, False
        limit = int(mt * store.chars_per_token)
        return (content[:limit], True) if len(content) > limit else (content, False)

    # ---- bridge 队列观测（多 worker 用）----
    def _bridge_view(self, cfg) -> dict:
        b = cfg.get("bridge", {}) or {}
        req_dir, res_dir = deepsink_bridge.dirs(cfg)
        ttl = deepsink_bridge.default_ttl(cfg)
        view = deepsink_bridge.snapshot(req_dir, res_dir, ttl,
                                        workers_file=deepsink_bridge.workers_path(cfg),
                                        online_ttl=deepsink_bridge.heartbeat_ttl(cfg),
                                        hb_cfg=cfg)
        view.update({"backend": cfg.get("backend"),
                     "active": cfg.get("backend") == "bridge",
                     "timeout_seconds": b.get("timeout_seconds"),
                     "hint": "另开窗口跑 `python deepsink_brain.py watch --worker 名` 接单；可多个窗口并行，称呼约定 厂商-角色-序号。"})
        return view

    # ---- 基础输出 ----
    def _send(self, code: int, body: bytes, ctype: str, head_only: bool = False,
              cookies: list[str] | None = None, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._norm_path().startswith("/api"):
            # 控制台接口含账户/密钥/余额，禁止任何中间层或浏览器缓存
            self.send_header("Cache-Control", "no-store")
        if getattr(self, "_rid", ""):
            self.send_header("X-Request-Id", self._rid)
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP_HTML)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if self._wants_cors():
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers",
                             "X-Request-Id, X-RateLimit-Limit-Requests, "
                             "X-RateLimit-Remaining-Requests, Retry-After")
        for k, v in {**(getattr(self, "_rl", None) or {}), **(extra or {})}.items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, code: int, obj, head_only: bool = False,
                   cookies: list[str] | None = None, extra: dict | None = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", head_only, cookies, extra)

    def _send_html(self, code: int, html: str, head_only: bool = False):
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8", head_only)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        if n > MAX_BODY:
            # 先把 body 丢弃（有上限），避免客户端还在发送时我们就关闭连接、让它拿到
            # 连接错误而不是干净的 400；超过 MAX_DRAIN 才强制断开。
            remaining = min(n, MAX_DRAIN)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            if n > MAX_DRAIN:
                self.close_connection = True
            raise ValueError(f"request body too large ({n} > {MAX_BODY})")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _auth_token(self) -> str:
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return (self.headers.get("x-api-key", "") or "").strip()

    def _cookie_name(self) -> str:
        return self.server.cfg.get("session", {}).get("cookie", "deepsink_session")

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            c = SimpleCookie()
            c.load(raw)
            m = c.get(self._cookie_name())
            return m.value if m else ""
        except Exception:
            return ""

    def _user(self) -> dict | None:
        return self.server.store.user_by_session(self._session_token())

    def _set_session(self, token: str | None) -> list[str]:
        ttl = int(self.server.cfg.get("session", {}).get("ttl_seconds", 604800))
        name = self._cookie_name()
        if token:
            return [f"{name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ttl}"]
        return [f"{name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"]

    def _norm_path(self) -> str:
        p = self.path.split("?", 1)[0]
        while p.startswith("/v1/v1/"):
            p = "/v1/" + p[len("/v1/v1/"):]
        return p

    # ---- 方法分发 ----
    def do_OPTIONS(self):
        self._begin()
        if not self._host_ok():
            self._oai_err(403, "forbidden host"); return
        if self._wants_cors():
            # API 端点：放行预检（浏览器里的 MC 面板 / 网页工具可直接调）
            self._send(204, b"", "text/plain; charset=utf-8", extra={
                "Access-Control-Allow-Methods": CORS_METHODS,
                "Access-Control-Allow-Headers": CORS_HEADERS,
                "Access-Control-Max-Age": "600"})
            return
        # 控制台：不下发任何 CORS 许可 → 跨域预检失败
        self._send(204, b"", "text/plain; charset=utf-8")

    def do_HEAD(self):
        self._begin()
        if not self._host_ok():
            self._send(403, b"", "application/json; charset=utf-8", head_only=True); return
        p = self._norm_path()
        if p in ("/", "/index.html", "/home", "/chat", "/chat/", "/platform", "/platform/",
                 "/docs", "/docs/", "/status", "/status/", "/login", "/login/"):
            self._send(200, b"", "text/html; charset=utf-8", head_only=True); return
        if p in ("/v1", "/health", "/v1/health"):
            self._send_json(200, {"ok": True}, head_only=True); return
        self._send(404, b"", "application/json; charset=utf-8", head_only=True)

    def do_GET(self):
        self._begin()
        if not self._guard():
            return
        cfg, store = self.server.cfg, self.server.store
        path = self._norm_path()

        if path.startswith("/assets/"):
            self._serve_asset(path); return
        page = {"/": "index.html", "/index.html": "index.html", "/home": "index.html",
                "/chat": "chat.html", "/chat/": "chat.html",
                "/platform": "platform.html", "/platform/": "platform.html",
                "/docs": "docs.html", "/docs/": "docs.html",
                "/status": "status.html", "/status/": "status.html",
                "/login": "login.html", "/login/": "login.html"}.get(path)
        if page:
            host, port = self._addr()
            self._send_html(200, render_page(page, cfg, host, port)); return
        if path in ("/health", "/v1/health"):
            self._send_json(200, {"ok": True, "backend": cfg["backend"],
                                  "model": cfg["model_name"], "models": model_ids(cfg),
                                  "accounts": store.count_users(),
                                  "protocols": ["openai", "anthropic"]}); return
        if path == "/v1/models":
            if not store.find_key(self._auth_token()):
                self._oai_err(401, "invalid api key", "authentication_error"); return
            self._send_json(200, {"object": "list",
                                  "data": [model_obj(cfg, mid) for mid in model_ids(cfg)]}); return
        if path.startswith("/v1/models/"):
            if not store.find_key(self._auth_token()):
                self._oai_err(401, "invalid api key", "authentication_error"); return
            mid = path[len("/v1/models/"):]
            if mid not in model_ids(cfg):
                self._oai_err(404, f"model '{mid}' not found", "invalid_request_error", ec="model_not_found")
                return
            self._send_json(200, model_obj(cfg, mid)); return

        # ---- 账户（会话）----
        if path == "/api/me":
            u = self._user()
            if not u:
                self._send_json(401, {"error": {"message": "未登录", "type": "unauthorized"}}); return
            self._send_json(200, {"user": u, "account": store.account(u["id"])}); return

        # ---- 管理（需登录）----
        if path.startswith("/api/"):
            u = self._user()
            if not u:
                self._send_json(401, {"error": {"message": "未登录", "type": "unauthorized"}}); return
            if path == "/api/account":
                self._send_json(200, {"account": store.account(u["id"]), "stats": store.stats(u["id"])}); return
            if path == "/api/keys":
                self._send_json(200, {"keys": store.public_keys(u["id"])}); return
            if path == "/api/usage":
                q = self.path.split("limit=", 1)
                limit = 50
                if len(q) == 2:
                    raw_lim = q[1].split("&")[0]
                    if raw_lim.isdigit():
                        limit = max(1, min(int(raw_lim), 500))
                self._send_json(200, {"usage": store.usage(u["id"], limit)}); return
            if path == "/api/ledger":
                self._send_json(200, {"ledger": store.ledger(u["id"], 50)}); return
            if path == "/api/sessions":
                self._send_json(200, {"sessions": store.sessions_of(u["id"], self._session_token())}); return
            if path == "/api/stats":
                self._send_json(200, store.stats(u["id"])); return
            if path == "/api/bridge":
                self._send_json(200, {"bridge": self._bridge_view(cfg)}); return
            self._send_json(404, {"error": {"message": "not found"}}); return

        self._send_json(404, {"error": {"message": "not found"}})

    def _serve_asset(self, path: str):
        rel = path[len("/assets/"):]
        if ".." in rel or rel.startswith("/"):
            self._send_json(404, {"error": {"message": "bad path"}}); return
        fp = WEB_DIR / "assets" / rel
        if not fp.exists() or not fp.is_file():
            self._send_json(404, {"error": {"message": "asset not found"}}); return
        self._send(200, fp.read_bytes(), CTYPES.get(fp.suffix, "application/octet-stream"))

    def do_POST(self):
        self._begin()
        if not self._guard():
            return
        cfg, store = self.server.cfg, self.server.store
        path = self._norm_path()
        try:
            body = self._read_json()
        except Exception as e:
            self._oai_err(400, f"bad json: {e}"); return

        if path == "/v1/chat/completions":
            self._chat_openai(cfg, store, body); return
        if path == "/v1/messages":
            self._chat_anthropic(cfg, store, body); return

        # ---- 账户：注册 / 登录 / 登出 ----
        if path == "/api/auth/register":
            try:
                store.create_user(body.get("email", ""), body.get("name", ""),
                                  body.get("password", ""))
            except ValueError as e:
                self._send_json(400, {"error": {"message": str(e), "type": "invalid_request_error"}}); return
            token = store.login(body.get("email", ""), body.get("password", ""))
            u = store.user_by_session(token)
            self._send_json(200, {"ok": True, "user": u, "account": store.account(u["id"])},
                            cookies=self._set_session(token)); return
        if path == "/api/auth/login":
            email = (body.get("email", "") or "").strip().lower()
            wait = self.server.login_throttle.blocked(email)
            if wait > 0:
                self._send_json(429, {"error": {"message": "尝试过于频繁，请稍后再试",
                                                "type": "rate_limit_error"}},
                                extra={"Retry-After": str(int(wait) + 1)}); return
            token = store.login(email, body.get("password", ""))
            if not token:
                self.server.login_throttle.record_fail(email)
                self._send_json(401, {"error": {"message": "邮箱或密码不正确",
                                                "type": "authentication_error"}}); return
            self.server.login_throttle.clear(email)
            u = store.user_by_session(token)
            self._send_json(200, {"ok": True, "user": u, "account": store.account(u["id"])},
                            cookies=self._set_session(token)); return
        if path == "/api/auth/logout":
            store.logout(self._session_token())
            self._send_json(200, {"ok": True}, cookies=self._set_session(None)); return

        # ---- 管理（需登录）----
        if path.startswith("/api/"):
            u = self._user()
            if not u:
                self._send_json(401, {"error": {"message": "未登录", "type": "unauthorized"}}); return
            if path == "/api/chat":
                self._api_chat(cfg, store, u, body); return
            if path == "/api/recharge":
                try:
                    amt = float(body.get("amount", 0))
                except Exception:
                    amt = 0
                if amt <= 0:
                    self._send_json(400, {"error": {"message": "amount 必须为正数"}}); return
                a = store.recharge(u["id"], amt)
                self._send_json(200, {"ok": True, "account": a, "stats": store.stats(u["id"])}); return
            if path == "/api/account/password":
                try:
                    store.change_password(u["id"], body.get("old", ""), body.get("new", ""),
                                          keep_token=self._session_token())
                except ValueError as e:
                    self._send_json(400, {"error": {"message": str(e)}}); return
                self._send_json(200, {"ok": True, "sessions": store.sessions_of(u["id"], self._session_token())}); return
            if path == "/api/sessions/revoke":
                sid = body.get("id", "")
                if body.get("all"):
                    n = store.revoke_other_sessions(u["id"], self._session_token())
                    self._send_json(200, {"ok": True, "revoked": n,
                                          "sessions": store.sessions_of(u["id"], self._session_token())}); return
                ok = store.revoke_session(u["id"], sid)
                self._send_json(200, {"ok": ok,
                                      "sessions": store.sessions_of(u["id"], self._session_token())}); return
            if path == "/api/keys":
                k = store.create_key(u["id"], body.get("name") or "新密钥")
                self._send_json(200, {"ok": True, "key": k, "keys": store.public_keys(u["id"])}); return
            if path == "/api/keys/toggle":
                if not store.own_key(u["id"], body.get("id", "")):
                    self._send_json(404, {"error": {"message": "密钥不存在"}}); return
                store.toggle_key(u["id"], body.get("id", ""), bool(body.get("enabled", True)))
                self._send_json(200, {"ok": True, "keys": store.public_keys(u["id"])}); return
            if path == "/api/keys/delete":
                ok = store.delete_key(u["id"], body.get("id", ""))
                self._send_json(200, {"ok": ok, "keys": store.public_keys(u["id"])}); return
            self._send_json(404, {"error": {"message": "not found"}}); return

        self._send_json(404, {"error": {"message": "not found"}})

    # ---- /api/chat：网页版对话（会话鉴权，不暴露 key）----
    def _api_chat(self, cfg, store, user, req):
        uid = user["id"]
        messages = req.get("messages", [])
        model = req.get("model") or cfg["model_name"]
        stream = bool(req.get("stream"))
        if store.enabled and store.balance(uid) <= 0:
            self._send_json(402, {"error": {"message": "Insufficient Balance",
                                            "type": "insufficient_quota",
                                            "code": "insufficient_quota"}}); return
        gw = cfg.get("gateway", {}) or {}
        cid = "chat-" + uuid.uuid4().hex[:16]
        created = int(time.time())
        if stream:
            # 与 /v1 流式同一套戏：先开流发首片，再等大脑，期间保活
            self._sse_open()
            self._sse_ttft(gw)
            self._sse_raw(_oai_chunk(cid, model, created, {"role": "assistant"}))
            msg, timed_out, err = self._await_brain(
                cfg, messages, model, emit_ka=lambda: self._sse_raw(b": ka\n\n"))
        else:
            msg, timed_out, err = self._await_brain(cfg, messages, model)
        content, _tools, fatal = self._resolve_brain(cfg, stream, msg, timed_out, err)
        if fatal is not None:
            self._send_json(500, {"error": {"message": str(fatal), "type": "server_error"}})
            return
        ptok = store.estimate_messages(messages)
        ctok = store.estimate_tokens(content)
        rec = store.charge(uid, None, model, ptok, ctok, cfg["backend"])
        if stream:
            self._stream_openai(cid, model, created, {"role": "assistant", "content": content},
                                {"prompt_tokens": ptok, "completion_tokens": ctok,
                                 "total_tokens": ptok + ctok})
            return
        self._send_json(200, {"reply": content, "model": model,
                              "usage": {"prompt_tokens": ptok, "completion_tokens": ctok},
                              "billing": {"cost": rec["cost"], "currency": store.currency,
                                          "balance": store.balance(uid)}})

    # ---- 计费闸门：key 鉴权 + 限流 + 该账户余额 ----
    def _gate(self, store, anthropic: bool = False):
        def _fail(code, msg, etype, ec=None, extra=None):
            if anthropic:
                self._anth_err(code, msg, etype, extra=extra)
            else:
                self._oai_err(code, msg, etype, ec=ec, extra=extra)

        k = store.find_key(self._auth_token())
        if not k:
            _fail(401, "invalid api key", "authentication_error"); return None
        allowed, retry = self.server.limiter.check(k["id"])
        if not allowed:
            _fail(429, "rate limit exceeded", "rate_limit_error",
                  ec="rate_limit_exceeded", extra={"Retry-After": str(int(retry) + 1)})
            return None
        snap = self.server.limiter.snapshot(k["id"])
        if snap:
            self._rl = {"X-RateLimit-Limit-Requests": str(snap[0]),
                        "X-RateLimit-Remaining-Requests": str(snap[1])}
        if store.enabled and store.balance(k["user"]["id"]) <= 0:
            _fail(402, "Insufficient Balance", "insufficient_quota", ec="insufficient_quota")
            return None
        return k

    # ---- OpenAI：/v1/chat/completions ----
    def _chat_openai(self, cfg, store, req):
        k = self._gate(store)
        if not k:
            return
        uid = k["user"]["id"]
        messages = req.get("messages", [])
        model = req.get("model") or cfg["model_name"]
        stream = bool(req.get("stream"))
        gw = cfg.get("gateway", {}) or {}
        cid = "chatcmpl-" + uuid.uuid4().hex[:16]
        created = int(time.time())

        if stream:
            # 流式：先开流 + 发首片，再等大脑——客户端立刻看到"开始回了"，
            # 等待期间靠 SSE 注释保活；超 patience 改发兜底文案，绝不干等到超时。
            self._sse_open()
            self._sse_ttft(gw)
            self._sse_raw(_oai_chunk(cid, model, created, {"role": "assistant"}))
            msg, timed_out, err = self._await_brain(
                cfg, messages, model, emit_ka=lambda: self._sse_raw(b": ka\n\n"))
        else:
            msg, timed_out, err = self._await_brain(cfg, messages, model)

        content, tool_calls, fatal = self._resolve_brain(cfg, stream, msg, timed_out, err)
        if fatal is not None:
            self._oai_err(500, str(fatal), "server_error")
            return
        content, truncated = self._apply_max_tokens(store, content, req.get("max_tokens"))
        ctok = store.estimate_tokens(content)
        ptok = store.estimate_messages(messages)
        rec = store.charge(uid, k["id"], model, ptok, ctok, cfg["backend"])

        finish = "length" if truncated else "stop"
        usage = {"prompt_tokens": ptok, "completion_tokens": ctok, "total_tokens": ptok + ctok}
        out_msg = {"role": "assistant", "content": content}
        if tool_calls:
            out_msg["tool_calls"] = tool_calls
        if stream:
            self._stream_openai(cid, model, created, out_msg, usage, cfg, finish)
        else:
            self._send_json(200, {
                "id": cid, "object": "chat.completion", "created": created, "model": model,
                "system_fingerprint": "fp_deepsink",
                "choices": [{"index": 0, "message": out_msg, "finish_reason": finish}],
                "usage": usage, "_billing": {"cost": rec["cost"], "currency": store.currency,
                                             "balance": store.balance(uid)}})

    def _stream_openai(self, cid, model, created, msg, usage, cfg=None, finish="stop"):
        """只发内容分片 + 收尾（开流与首片由调用方负责）。"""
        sc = (cfg or {}).get("stream", {}) or {}
        size = int(sc.get("chunk_chars", 4) or 4)
        delay = float(sc.get("delay_ms", 0) or 0) / 1000.0
        try:
            if msg.get("tool_calls"):
                self._sse_raw(_oai_chunk(cid, model, created, {"tool_calls": msg["tool_calls"]}))
            else:
                for piece in _chunks(msg.get("content") or "", size):
                    self._sse_raw(_oai_chunk(cid, model, created, {"content": piece}))
                    if delay:
                        time.sleep(delay)
            self._sse_raw(_oai_chunk(cid, model, created, {}, finish, {"usage": usage}))
            self._sse_raw(b"data: [DONE]\n\n")
        except Exception:
            pass

    # ---- Anthropic：/v1/messages ----
    def _chat_anthropic(self, cfg, store, req):
        k = self._gate(store, anthropic=True)
        if not k:
            return
        uid = k["user"]["id"]
        messages, _sys = _norm_anthropic(req)
        model = req.get("model") or cfg["model_name"]
        stream = bool(req.get("stream"))
        gw = cfg.get("gateway", {}) or {}
        ptok = store.estimate_messages(messages)
        mid = "msg_" + uuid.uuid4().hex[:20]

        if stream:
            # 与 OpenAI 流式同一套戏；Anthropic 的保活用协议自带的 ping 事件
            self._sse_open()
            self._sse_ttft(gw)
            self._sse_raw(_anth_ev("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": ptok, "output_tokens": 0}}}))
            self._sse_raw(_anth_ev("content_block_start", {"type": "content_block_start",
                                 "index": 0, "content_block": {"type": "text", "text": ""}}))
            self._sse_raw(_anth_ev("ping", {"type": "ping"}))
            msg, timed_out, err = self._await_brain(
                cfg, messages, model,
                emit_ka=lambda: self._sse_raw(_anth_ev("ping", {"type": "ping"})))
        else:
            msg, timed_out, err = self._await_brain(cfg, messages, model)

        content, _tools, fatal = self._resolve_brain(cfg, stream, msg, timed_out, err)
        if fatal is not None:
            self._anth_err(500, str(fatal), "api_error")
            return
        content, truncated = self._apply_max_tokens(store, content, req.get("max_tokens"))
        ctok = store.estimate_tokens(content)
        store.charge(uid, k["id"], model, ptok, ctok, cfg["backend"])

        stop_reason = "max_tokens" if truncated else "end_turn"
        if stream:
            self._stream_anthropic(mid, model, content, ptok, ctok, cfg, stop_reason)
        else:
            self._send_json(200, {
                "id": mid, "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": content}],
                "stop_reason": stop_reason, "stop_sequence": None,
                "usage": {"input_tokens": ptok, "output_tokens": ctok}})

    def _stream_anthropic(self, mid, model, content, ptok, ctok, cfg=None, stop_reason="end_turn"):
        """只发内容分片 + 收尾（开流与首事件由调用方负责）。"""
        sc = (cfg or {}).get("stream", {}) or {}
        size = int(sc.get("chunk_chars", 4) or 4)
        delay = float(sc.get("delay_ms", 0) or 0) / 1000.0
        try:
            for piece in _chunks(content, size):
                self._sse_raw(_anth_ev("content_block_delta", {"type": "content_block_delta",
                                     "index": 0, "delta": {"type": "text_delta", "text": piece}}))
                if delay:
                    time.sleep(delay)
            self._sse_raw(_anth_ev("content_block_stop", {"type": "content_block_stop", "index": 0}))
            self._sse_raw(_anth_ev("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                 "usage": {"output_tokens": ctok}}))
            self._sse_raw(_anth_ev("message_stop", {"type": "message_stop"}))
        except Exception:
            pass

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "_rid", ""):
            self.send_header("X-Request-Id", self._rid)
        if self._wants_cors():
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    # ---- 网关：先开流，再等大脑 ----
    def _sse_raw(self, data: bytes):
        """写一段 SSE 字节并立即 flush（流式必须立刻到达客户端）。"""
        self.wfile.write(data)
        try:
            self.wfile.flush()
        except Exception:
            pass

    @staticmethod
    def _sse_ttft(gw: dict):
        """首片前的假 prefill 延迟——真模型也是先愣一下才开口。"""
        ttft = float(gw.get("ttft_ms", 400)) / 1000.0
        if ttft > 0:
            time.sleep(ttft)

    @staticmethod
    def _fallback_text(cfg) -> str:
        return (cfg.get("bridge", {}) or {}).get("fallback_text", "…")

    def _resolve_brain(self, cfg, stream: bool, msg, timed_out: bool, err):
        """把 _await_brain 的返回解析成 (content, tool_calls, fatal)。

        fatal 非 None 表示"该以 500 结束"——仅非流式场景做得到；
        流式已经开了流，只能记日志并降级成兜底文案。
        """
        if err is not None:
            print(f"[deepsink] 大脑异常：{err}", file=sys.stderr)
            if not stream:
                return None, None, err
            return self._fallback_text(cfg), None, None
        if timed_out:
            return self._fallback_text(cfg), None, None
        content = msg.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return content, msg.get("tool_calls"), None

    def _await_brain(self, cfg, messages, model, emit_ka=None):
        """等"大脑"回复，返回 (msg, timed_out, err)。

        - 后台线程里跑 complete()（最久阻塞到 bridge.timeout_seconds）；
          主线程只轮询状态，因此等待期间能周期性保活（emit_ka），
          客户端不会因为半天收不到字节而断开——这就是"先开流"的意义。
        - 超过 gateway.patience_ms 仍无回复：删掉请求文件（含认领锁），
          让迟到的回单失败而不是写成没人要的孤儿文件；返回 timed_out=True，
          由调用方改发兜底文案。客户端永远在耐心内得到一句完整回复。
        - 保活期间发现客户端已断开（写失败）：同样清掉请求文件与认领锁——
          没人会读答案，死单不留影子。
        """
        gw = cfg.get("gateway", {}) or {}
        patience = max(0.0, float(gw.get("patience_ms", 45000)) / 1000.0)
        ping = max(0.2, float(gw.get("ping_ms", 3000)) / 1000.0)
        box: dict = {}

        def work():
            try:
                box["msg"] = complete(cfg, messages, model,
                                      on_enqueue=lambda p: box.__setitem__("req_path", p))
            except Exception as e:  # noqa: BLE001 —— 大脑异常不当成崩溃，降级兜底
                box["err"] = e

        threading.Thread(target=work, daemon=True).start()
        deadline = time.monotonic() + patience
        next_ka = time.monotonic() + ping
        while "msg" not in box and "err" not in box:
            if time.monotonic() >= deadline:
                break
            if emit_ka and time.monotonic() >= next_ka:
                try:
                    emit_ka()
                except Exception:
                    # 客户端已断开：没人会读答案了，清掉请求文件与认领锁再收尾
                    p = box.get("req_path")
                    if p:
                        for suffix in ("", ".lock"):
                            try:
                                Path(str(p) + suffix).unlink()
                            except OSError:
                                pass
                    return None, True, None      # 客户端已断开：等同超时收尾
                next_ka = time.monotonic() + ping
            time.sleep(0.1)
        if "msg" in box:
            return box["msg"], False, None
        if "err" in box:
            return None, False, box["err"]
        # 超时：客户端已经放弃，清掉请求文件，防止死单之后还被回
        p = box.get("req_path")
        if p:
            for suffix in ("", ".lock"):
                try:
                    Path(str(p) + suffix).unlink()
                except OSError:
                    pass
        return None, True, None


class DeepSinkServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(cfg: dict) -> DeepSinkServer:
    srv = DeepSinkServer((cfg["host"], int(cfg["port"])), Handler)
    srv.cfg = cfg
    srv.store = deepsink_store.Store(db_path(cfg), cfg)
    rl = cfg.get("rate_limit", {}) or {}
    srv.limiter = RateLimiter(rpm=int(rl.get("rpm", 0) or 0), burst=int(rl.get("burst", 0) or 0))
    lt = cfg.get("login_throttle", {}) or {}
    srv.login_throttle = LoginThrottle(max_fails=int(lt.get("max_fails", 8) or 0),
                                       window=float(lt.get("window_seconds", 300) or 300))
    srv.store.purge_expired_sessions()
    legacy = cfg.get("legacy_import_dir")
    if legacy:
        lp = Path(legacy)
        if not lp.is_absolute():
            lp = BASE / lp
        got = srv.store.import_legacy(lp)
        if got:
            print(f"[deepsink] 已把旧账本迁入「本机账户」{got['email']}"
                  f"（导入 {got.get('migrated_keys', 0)} 个密钥，余额 {got['balance']}）")
    return srv


# ----------------------------- CLI -----------------------------
def _store(cfg: dict) -> "deepsink_store.Store":
    return deepsink_store.Store(db_path(cfg), cfg)


def cmd_info(cfg: dict) -> int:
    host, port = cfg["host"], cfg["port"]
    s = _store(cfg)
    users = [{"id": u["id"], "email": u["email"], "name": u["name"], "balance": round(u["balance"], 4)}
             for u in s._q("SELECT * FROM users ORDER BY id")]  # noqa: SLF001
    print(json.dumps({
        "db": str(db_path(cfg)),
        "openai_base_url": f"http://{host}:{port}/v1",
        "anthropic_base_url": f"http://{host}:{port}",
        "backend": cfg["backend"], "model": cfg["model_name"], "models": model_ids(cfg),
        "users": users,
        "pages": {"官网": f"http://{host}:{port}/", "网页版": f"http://{host}:{port}/chat",
                  "登录": f"http://{host}:{port}/login",
                  "开放平台": f"http://{host}:{port}/platform", "文档": f"http://{host}:{port}/docs",
                  "状态": f"http://{host}:{port}/status"},
        "endpoints": ["/v1/chat/completions", "/v1/messages", "/v1/models", "/v1/models/{id}",
                      "/health",
                      "/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/me",
                      "/api/account", "/api/account/password", "/api/sessions", "/api/sessions/revoke",
                      "/api/bridge", "/api/keys", "/api/usage", "/api/ledger", "/api/recharge",
                      "/api/chat"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(cfg: dict, backend: str | None, port: int | None) -> int:
    if backend:
        cfg["backend"] = backend
    if port:
        cfg["port"] = port
    srv = build_server(cfg)
    host, p = cfg["host"], cfg["port"]
    print(f"[deepsink] DeepSink 上线 http://{host}:{p}  backend={cfg['backend']} model={cfg['model_name']}")
    print(f"[deepsink] 数据库: {db_path(cfg)}   账户数: {srv.store.count_users()}")
    print(f"[deepsink] OpenAI base_url: http://{host}:{p}/v1    Anthropic base_url: http://{host}:{p}")
    print(f"[deepsink] 官网: /   登录: /login   对话: /chat   开放平台: /platform   文档: /docs")
    if cfg["backend"] == "bridge":
        print("[deepsink] bridge 后端：另开一个进程跑 `python deepsink_brain.py watch` 来接单。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[deepsink] 停止")
    finally:
        srv.server_close()
    return 0


def cmd_client(cfg: dict, text: str, stream: bool, timeout: float, key: str | None) -> int:
    if not key:
        # 库里只存 key 哈希，取不回明文——改用本地密钥文件（`sim/data/.dskey`，
        # 自测时把平台创建的 key 存一份进去即可）；没有就明确报错。
        kf = BASE / "data" / ".dskey"
        try:
            key = kf.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
    if not key:
        print("[client] 没有可用 key：用 --key 指定，或先在 /platform 建 key 并存入 "
              "sim/data/.dskey", file=sys.stderr)
        return 1
    url = f"http://{cfg['host']}:{cfg['port']}/v1/chat/completions"
    body = json.dumps({"model": cfg["model_name"], "stream": stream,
                       "messages": [{"role": "user", "content": text}]}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except Exception as e:
        print(f"[client] 请求失败（服务在跑吗？）：{e}", file=sys.stderr)
        return 1
    if stream:
        print(raw)
    else:
        data = json.loads(raw)
        print(data["choices"][0]["message"].get("content", ""))
    return 0


def cmd_users(cfg: dict) -> int:
    s = _store(cfg)
    rows = s._q("SELECT id,email,name,balance,is_admin,enabled,created FROM users ORDER BY id")  # noqa: SLF001
    if not rows:
        print("（还没有账户，用 adduser 或网页 /login 注册）")
        return 0
    print(f"{'ID':<4}{'邮箱':<28}{'名称':<14}{'余额':>12}  管理员")
    for u in rows:
        print(f"{u['id']:<4}{u['email']:<28}{u['name']:<14}{u['balance']:>12.4f}  "
              f"{'✓' if u['is_admin'] else ''}")
    print(f"\n数据库: {db_path(cfg)}")
    return 0


def cmd_adduser(cfg: dict, email: str, password: str, name: str, admin: bool) -> int:
    s = _store(cfg)
    try:
        u = s.create_user(email, name, password, is_admin=admin)
    except ValueError as e:
        print(f"[adduser] {e}", file=sys.stderr)
        return 1
    print(f"[adduser] 已创建 #{u['id']} {u['email']}（余额 {u['balance']}）")
    return 0


def cmd_passwd(cfg: dict, email: str, password: str) -> int:
    s = _store(cfg)
    try:
        s.set_password(email, password)
    except ValueError as e:
        print(f"[passwd] {e}", file=sys.stderr)
        return 1
    print(f"[passwd] 已更新 {email} 的密码")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deepsink_server")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("info")
    sub.add_parser("users")
    s = sub.add_parser("serve")
    s.add_argument("--backend", choices=["bridge", "openai", "echo"], default=None)
    s.add_argument("--port", type=int, default=None)
    c = sub.add_parser("client")
    c.add_argument("text")
    c.add_argument("--stream", action="store_true")
    c.add_argument("--timeout", type=float, default=300.0)
    c.add_argument("--key", default=None)
    a = sub.add_parser("adduser")
    a.add_argument("--email", required=True)
    a.add_argument("--password", required=True)
    a.add_argument("--name", default="")
    a.add_argument("--admin", action="store_true")
    p = sub.add_parser("passwd")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    args = ap.parse_args(argv)
    cfg = load_cfg()
    return {"info": lambda: cmd_info(cfg),
            "users": lambda: cmd_users(cfg),
            "serve": lambda: cmd_serve(cfg, args.backend, args.port),
            "client": lambda: cmd_client(cfg, args.text, args.stream, args.timeout, args.key),
            "adduser": lambda: cmd_adduser(cfg, args.email, args.password, args.name, args.admin),
            "passwd": lambda: cmd_passwd(cfg, args.email, args.password)}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
