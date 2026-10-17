#!/usr/bin/env python3
"""MC 接入 AI 的封装 agent —— 图形化控制台（本地网页）。

这是整套接入方案的「外壳」：一个客户端 = 一个游戏 + 服务器/版本组合，每个客户端一套
预设（人格/提示词/权限/记忆/后端）。控制台负责：
- 选客户端、改预设（改完即生效，不用碰代码）
- 选后端、填 API key（存 instance/robot/secrets.env，不进 git）
- 看实时收发（inbox 是玩家喊的话，outbox 是机器人回的话）
- **预览即将发给 AI 的完整提示词**（这个 agent 自动拼装：人格 + 世界常识 + 记忆 + 环境）
- 一键启停 L1 大脑（brain_llm 接单）、手动代发一句话/一条指令

只依赖 Python 标准库。用法：
  python -m mcrobot.console.server [--port 8899] [--host 127.0.0.1]
然后浏览器打开 http://127.0.0.1:8899/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
from ..paths import REPO, ROBOT_DIR

from ..brain import protocol as br
from .. import identity as idy
from ..ops import orchestrator as orch

STATIC = HERE / "web"
ROBOT_DIR = REPO / "instance" / "robot"
SECRETS = ROBOT_DIR / "secrets.env"
LOG_FILES = {
    "brain_llm": ROBOT_DIR / "brain_llm.log",
    "robot": ROBOT_DIR / "loop.out",
    "bridge": ROBOT_DIR / "brain_watch.out",
}

# 控制台自己拉起的大脑进程（与 supervisor 守护的身体互不干扰）
_BRAIN: dict = {"proc": None, "started": 0.0, "kind": ""}
_PROC_CACHE: dict = {"ts": 0.0, "lines": []}


# ---------------- 小工具 ----------------
def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _tail(path: Path, n: int = 60) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
    except Exception:
        return []


def _jsonl_tail(path: Path, n: int = 20) -> list[dict]:
    out = []
    for ln in _tail(path, n):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


# ---------------- 密钥 ----------------
def load_secrets() -> None:
    """启动时把 secrets.env 灌进环境变量（key 不进 git，也不进 preset）。"""
    if not SECRETS.exists():
        return
    for ln in SECRETS.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def save_secret(env_name: str, value: str) -> None:
    lines: list[str] = []
    if SECRETS.exists():
        lines = [l for l in SECRETS.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith(env_name + "=")]
    lines.append(f"{env_name}={value}")
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[env_name] = value


def mask(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v)
    return f"{v[:4]}****{v[-4:]}"


# ---------------- 进程/状态探测 ----------------
def _cmdlines() -> list[str]:
    """本机 python 进程命令行（缓存 5 秒，避免每次刷新都拉 PowerShell）。

    两个坑：
    - 探测命令自身（`python -c "...brain.py watch..."`）会被自己抓到 → 必须排除
      本进程 PID 与所有 `-c` 内联脚本，否则状态灯全是假阳性。
    - PowerShell 只给 CommandLine 时无法区分，所以改成 PID + 命令行成对输出。
    """
    if time.time() - _PROC_CACHE["ts"] < 5.0:
        return _PROC_CACHE["lines"]
    raw: list[str] = []
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                 "| ForEach-Object { $_.ProcessId.ToString() + \"`t\" + $_.CommandLine }"],
                capture_output=True, timeout=15)
            pairs = [l.partition("\t") for l in
                     out.stdout.decode("utf-8", "ignore").splitlines()]
        else:
            out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, timeout=10)
            pairs = [(l.split(None, 1)[0], "\t", l.split(None, 1)[1])
                     for l in out.stdout.decode("utf-8", "ignore").splitlines() if l.strip()]
    except Exception:
        pairs = []
    me = os.getpid()
    lines: list[str] = []
    for pid_s, _, cmd in pairs:
        try:
            pid = int(pid_s.strip())
        except Exception:
            pid = -1
        cmd = cmd.strip()
        if not cmd or pid == me:
            continue
        if " -c " in cmd or cmd.startswith("-c"):  # 内联脚本（含探测自身）
            continue
        lines.append(cmd)
    _PROC_CACHE.update(ts=time.time(), lines=lines)
    return lines


def _running(pattern: str) -> bool:
    """判断某个脚本是否在跑（"brain.py watch" 这类带参数的写法也支持）。

    必须按「脚本名边界」匹配：直接用子串会踩两个坑——
    探测命令 `python -c "...brain.py watch..."` 自己命中自己（已在上层排除），
    以及 sim 侧的 `deepsink_brain.py watch` 命中 `brain.py watch`。
    """
    parts = pattern.split()
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(parts[0]) + r"(?:\s|$)")
    args = parts[1:]
    return any(pat.search(l) and all(a in l for a in args) for l in _cmdlines())


def body_alive() -> bool:
    """身体：supervisor 守护的 robot.py。用 cursor.json 的更新新鲜度判断更稳。"""
    try:
        return time.time() - (ROBOT_DIR / "cursor.json").stat().st_mtime < 15
    except Exception:
        return _running("mcrobot.body.loop")


def game_state() -> dict:
    try:
        from ..body import bridge as cc
        s = cc.state()
        p = s.get("player") or {}
        return {"in_game": bool(s.get("inGame")), "player": p}
    except Exception as e:
        return {"in_game": False, "err": str(e)[:80]}


def endpoint_ok(url: str) -> str:
    if not url:
        return "未配置"
    import urllib.error
    # 带上 key 探测：不带 key 一律 401，会让人误判成「端点挂了」
    headers = {}
    key = os.environ.get("MCROBOT_LLM_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url.rstrip("/") + "/models", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        ids = [m.get("id") for m in data.get("data", [])][:4]
        return "ok：" + "、".join(str(i) for i in ids)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "需要有效 key（401/403）"  # 不是端点挂了，是 key 没填或无效
        return f"HTTP {e.code}"
    except Exception as e:
        return f"不通（{type(e).__name__}）"


# ---------------- 大脑进程（L1）----------------
def brain_start() -> dict:
    if _BRAIN["proc"] and _BRAIN["proc"].poll() is None:
        return {"ok": False, "msg": f"{_BRAIN['kind']} 已在跑"}
    if _running("mcrobot.brain.bridge_brain"):
        # 两个大脑同时消费 inbox 会抢记录（手册禁区）
        return {"ok": False, "msg": "检测到 bridge 大脑（bridge_brain watch）在跑，先停它再起 L1，否则会抢消息"}
    if _running("mcrobot.brain.llm_brain"):
        # 不是本控制台起的（命令行 / 编排起的）。再起一个就是两个 watch 抢同一个
        # inbox 指针：同一句话回两遍，或者一半消息被另一个吃掉。
        return {"ok": False, "msg": "已有 L1 大脑在跑（不是控制台起的），先停它再起"}
    env = dict(os.environ)
    env["MCROBOT_MEMORY_DIR"] = str(idy.memory_dir())
    env["PYTHONIOENCODING"] = "utf-8"
    log = (ROBOT_DIR / "brain_llm.out").open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "mcrobot.brain.llm_brain", "watch", "--timeout", "7200"],
        cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    _BRAIN.update(proc=proc, started=time.time(), kind="L1 大脑")
    return {"ok": True, "msg": f"已启动 L1 大脑 pid={proc.pid}"}


def brain_stop() -> dict:
    p = _BRAIN.get("proc")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        _BRAIN.update(proc=None, kind="")
        return {"ok": True, "msg": "已停止 L1 大脑"}
    _BRAIN.update(proc=None, kind="")
    return {"ok": False, "msg": "没有由控制台启动的大脑"}


def brain_status() -> dict:
    p = _BRAIN.get("proc")
    mine = bool(p and p.poll() is None)
    if mine:
        st = "running"
    elif _running("mcrobot.brain.llm_brain"):
        st = "external"      # 不是控制台起的（比如命令行 watch），一样在接单
    else:
        st = "stopped"
    return {"console_brain": st,
            "pid": p.pid if mine else None,
            "uptime": round(time.time() - _BRAIN["started"]) if mine else 0,
            "bridge_watch": _running("mcrobot.brain.bridge_brain")}


# ---------------- 一键全链路（后端→客户端→关界面→身体→大脑）----------------
_CHAIN: dict = {"thread": None, "stop": None}


def chain_start() -> dict:
    """后台线程跑编排，立刻返回——客户端要 40–60 秒才起得来，不能把请求挂住。"""
    t = _CHAIN.get("thread")
    if t and t.is_alive():
        return {"ok": False, "msg": "全链路正在跑，别重复点"}
    stop = threading.Event()
    deps = orch.real_deps()          # 大脑那两步会走控制台自己的 brain_start，灯才准

    def work():
        try:
            orch.run(deps=deps, stop=stop)
        finally:
            _CHAIN.update(thread=None, stop=None)

    th = threading.Thread(target=work, name="chain", daemon=True)
    _CHAIN.update(thread=th, stop=stop)
    th.start()
    return {"ok": True, "msg": "已开始：后端 → 客户端 → 关界面 → 身体 → 大脑"}


def chain_stop() -> dict:
    stop = _CHAIN.get("stop")
    if not stop:
        return {"ok": False, "msg": "全链路没在跑"}
    stop.set()
    return {"ok": True, "msg": "已请求中止（正在跑的那一步会跑完）"}


def chain_status() -> dict:
    st = orch.status()
    t = _CHAIN.get("thread")
    st["console_running"] = bool(t and t.is_alive())
    return st


# ---------------- API ----------------
def api_state(_body=None) -> dict:
    ver = idy.active_version()
    ctx = idy.context()
    be = idy.resolve_backend()
    lines = br._lines()
    return {
        "ts": _now(),
        "client": {"active": ver, "list": [{"id": v, "instance": v} for v in idy.list_versions()],
                   "name": f"{ctx['bot_name']} @ {ctx['server_name']}", "bot_name": ctx["bot_name"],
                   "game": "mc", "dir": f"version/{ver}"},
        "backend": {**be, "key_masked": mask(os.environ.get(be["key_env"], "")),
                    "probe": endpoint_ok(be.get("endpoint", ""))},
        "status": {"body": body_alive(), "game": game_state(),
                   "supervisor": _running("mcrobot.ops.supervisor"), **brain_status()},
        "chain": chain_status(),
        "queue": {"inbox_lines": len(lines), "ptr": br._ptr(),
                  "unread": max(0, len(lines) - br._ptr()),
                  "inbox": _jsonl_tail(ROBOT_DIR / "inbox.jsonl", 15),
                  "outbox": _jsonl_tail(ROBOT_DIR / "outbox.jsonl", 15)},
    }


def api_preview(body: dict) -> dict:
    """这个 agent 自动拼好的完整提示词——人格 + 世界常识 + 记忆 + 环境 + 历史。"""
    ver = idy.active_version()
    cfg = json.loads(idy.robot_json_path().read_text(encoding="utf-8"))
    from ..brain import llm_brain as bl
    bot = bl.Brain(cfg, version=ver)
    speaker = (body or {}).get("speaker", "oahnuy198")
    text = (body or {}).get("text", "在吗")
    msgs = bot.build_messages(speaker, text, (body or {}).get("env"))
    return {"client": idy.active_version(), "messages": msgs,
            "backend": idy.resolve_backend()}


def api_ask(body: dict) -> dict:
    """手动问一句（走完整提示词）。send=true 时顺便把回复写进 outbox。"""
    ver = idy.active_version()
    cfg = json.loads(idy.robot_json_path().read_text(encoding="utf-8"))
    from ..brain import llm_brain as bl
    bot = bl.Brain(cfg, version=ver)
    text = (body or {}).get("text", "")
    if not text:
        return {"ok": False, "err": "text 为空"}
    try:
        reply = bot._transport(bot.build_messages((body or {}).get("speaker", "console"), text))
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}
    actions = []
    if (body or {}).get("send", False):
        actions = bot.parse_reply(reply)
        for a in actions:
            br.emit(a)
    return {"ok": True, "reply": reply, "actions": actions}


def api_identity_save(body: dict) -> dict:
    """保存身份文件可编辑的分节（元信息/人格/世界常识/后端）。

    按分节原地替换——整文件重写会把「权威来源」表格这些手写说明冲掉。
    """
    body = body or {}
    updates = {k: v for k, v in (
        ("元信息", body.get("meta")),
        ("人格", body.get("persona")),
        ("世界常识", body.get("world_facts")),
        ("后端", body.get("backend")),
    ) if v is not None}
    if not updates:
        return {"ok": False, "err": "没有要保存的分节"}
    idy.save_sections(updates)
    return {"ok": True, "version": idy.active_version()}


def api_key(body: dict) -> dict:
    env_name = (body or {}).get("env") or idy.resolve_backend()["key_env"]
    if "value" in (body or {}):
        save_secret(env_name, (body or {}).get("value") or "")
    return {"ok": True, "env": env_name,
            "set": bool(os.environ.get(env_name, "")),
            "masked": mask(os.environ.get(env_name, ""))}


def api_logs(body: dict) -> dict:
    name = (body or {}).get("name") or "brain_llm"
    return {"name": name, "lines": _tail(LOG_FILES.get(name, LOG_FILES["brain_llm"]), 80)}


ROUTES = {
    ("GET", "/api/state"): api_state,
    ("POST", "/api/preview"): api_preview,
    ("POST", "/api/ask"): api_ask,
    ("POST", "/api/key"): api_key,
    ("GET", "/api/key"): lambda b: api_key({}),
    ("POST", "/api/preset"): api_identity_save,
    ("GET", "/api/preset"): lambda b: {"preset": {
        "id": idy.active_version(),
        "meta": idy.load_identity()["meta"],
        "persona": idy.load_identity()["persona"],
        "world_facts": idy.load_identity()["world_facts"],
        "backend": idy.load_identity()["backend"],
        "_auto": {"mods": idy.mods_summary(), "catalog": idy.catalog_summary(),
                  "boundaries": idy.boundaries(), "context": idy.context()},
    }, "active": idy.active_version(), "backends": idy.BACKEND_PRESETS},
    ("POST", "/api/client/use"): lambda b: {"ok": True, "active": idy.set_active(b["id"])},
    ("GET", "/api/blocks"): lambda b: {"hits": idy.search_blocks(
        (b or {}).get("q", ""), int((b or {}).get("limit", 20) or 20))},
    ("POST", "/api/brain/start"): lambda b: brain_start(),
    ("POST", "/api/brain/stop"): lambda b: brain_stop(),
    ("POST", "/api/chain/start"): lambda b: chain_start(),
    ("POST", "/api/chain/stop"): lambda b: chain_stop(),
    ("GET", "/api/chain/status"): lambda b: chain_status(),
    ("POST", "/api/say"): lambda b: (br.emit({"say": b["text"]}), {"ok": True})[1],
    ("POST", "/api/cmd"): lambda b: (br.emit({"cmd": b["command"]}), {"ok": True})[1],
    ("GET", "/api/logs"): api_logs,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, ctype: str, data: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code: int = 200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlsplit(self.path)
        path = u.path
        if path.startswith("/api/"):
            fn = ROUTES.get(("GET", path))
            if not fn:
                return self._json({"err": f"未知接口 {path}"}, 404)
            try:
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                return self._json(fn(q))
            except Exception as e:
                return self._json({"err": str(e)[:300]}, 500)
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if not str(f).startswith(str(STATIC.resolve())) or not f.exists():
            return self._send(404, "text/plain; charset=utf-8", b"404")
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(f.suffix, "application/octet-stream")
        self._send(200, ctype, f.read_bytes())

    def do_POST(self):
        path = urlsplit(self.path).path
        fn = ROUTES.get(("POST", path))
        if not fn:
            return self._json({"err": f"未知接口 {path}"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            body = {}
        try:
            return self._json(fn(body))
        except Exception as e:
            return self._json({"err": str(e)[:300]}, 500)

    def log_message(self, fmt, *args):  # 安静点
        pass


def main(argv=None) -> int:
    load_secrets()
    ap = argparse.ArgumentParser(prog="console")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[console] MC 接入 AI 控制台： http://{args.host}:{args.port}/")
    print(f"[console] 当前版本（身份）：{idy.active_version()}   "
          f"密钥文件：{SECRETS if SECRETS.exists() else '(尚未创建)'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        brain_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())