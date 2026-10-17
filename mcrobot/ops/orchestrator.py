#!/usr/bin/env python3
"""一键全链路：把手册 §3 那套「按顺序敲五条命令」编排成一次调用。

顺序（前面没好，后面就别做——所以任一步失败就停在原地）：

    后端(:8788) → 游戏客户端 → 关登录界面 → 身体 → 大脑

每步三件事：
- **先看是否已就绪**：已就绪就标 `skipped` 跳过，不重复拉起（一键按第二遍不会又开一个客户端）。
- **没就绪才动手**：拉起进程，记下日志文件。
- **动完轮询到就绪或超时**：超时即失败，停在原地，`detail` 写清卡在哪。

状态实时写 `instance/robot/chain.json`，控制台轮询它画进度。

为什么要抽成模块：这套顺序以前只存在于手册的文字里，靠人照着敲。控制台要的是
「点一下、每步亮灯、失败能看出卡在哪」，所以顺序得是**代码**，不能是文档。

依赖全部收在 `Deps` 里且可注入——单测直接塞假的就绪判定和假时钟，不用真启客户端。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..paths import REPO, ROBOT_DIR, SIM, load_secrets

CHAIN = ROBOT_DIR / "chain.json"
POLL = 1.0          # 轮询间隔（秒）
PROC_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# ---------------- 依赖 ----------------
@dataclass
class Deps:
    """编排要用到的全部外部能力。默认值是真家伙，单测全部替换掉。"""
    probe_backend: object      # () -> (bool, str)
    start_backend: object      # () -> str
    in_game: object            # () -> (bool, str)  桥活着且已进服
    start_client: object       # () -> str
    close_screen: object       # () -> str
    body_alive: object         # () -> bool
    start_body: object         # () -> str
    brain_running: object      # () -> bool
    start_brain: object        # () -> str
    now: object = time.time    # () -> float
    sleep: object = time.sleep  # (float) -> None


# 每步：id / 显示名 / 超时秒 / ready(是否已就绪) / start(拉起) / always(无就绪条件，每拍都做)
STEPS = [
    dict(id="backend", label="后端", timeout=30,
         ready=lambda d: d.probe_backend()[0], start=lambda d: d.start_backend()),
    dict(id="client", label="游戏客户端", timeout=180,
         ready=lambda d: d.in_game()[0], start=lambda d: d.start_client()),
    dict(id="screen", label="关登录界面", timeout=15, always=True,
         ready=lambda d: True, start=lambda d: d.close_screen()),
    dict(id="body", label="身体", timeout=30,
         ready=lambda d: d.body_alive(), start=lambda d: d.start_body()),
    dict(id="brain", label="大脑", timeout=30,
         ready=lambda d: d.brain_running(), start=lambda d: d.start_brain()),
]


# ---------------- 真实实现 ----------------
def _backend_cfg() -> dict:
    """后端端口/主机读 deepsink.json（别再硬编码端口）。"""
    try:
        return json.loads((SIM / "deepsink.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _probe_backend() -> tuple[bool, str]:
    from .. import identity as idy
    be = idy.resolve_backend()
    url = (be.get("endpoint") or "").rstrip("/")
    if not url:
        return False, "identity 里没配后端端点"
    headers = {}
    key = os.environ.get(be.get("key_env") or "MCROBOT_LLM_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url + "/models", headers=headers), timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        ids = [m.get("id") for m in data.get("data", [])][:3]
        return True, "ok：" + "、".join(str(i) for i in ids)
    except urllib.error.HTTPError as e:
        # 401/403 是 key 没配好，不是服务没起来——但这一样算「后端不可用」，不能往下走
        return False, f"HTTP {e.code}（端点活着，但鉴权没过）"
    except Exception as e:
        return False, f"不通（{type(e).__name__}）"


def _spawn(cmd: list[str], cwd: Path, log_name: str) -> str:
    """脱离启动一个后台进程，输出进 instance/robot/<log_name>。

    必须让子进程活过本进程：用户关掉控制台，客户端和身体不该跟着死。
    """
    load_secrets()          # key 走环境变量传给子进程（子进程继承本进程环境）
    ROBOT_DIR.mkdir(parents=True, exist_ok=True)
    log = (ROBOT_DIR / log_name).open("a", encoding="utf-8")
    flags = PROC_FLAGS | getattr(subprocess, "DETACHED_PROCESS", 0)
    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                         env=dict(os.environ), creationflags=flags)
    return f"pid={p.pid}（日志 {log_name}）"


def _start_backend() -> str:
    cfg = _backend_cfg()
    port = str(cfg.get("port") or 8788)
    # 不传 --backend：deepsink.json 说了算（见 supervisor.py 里同一个坑的注释）
    return _spawn([sys.executable, "-u", "deepsink_server.py", "serve", "--port", port],
                  SIM, "deepsink.out")


def _in_game() -> tuple[bool, str]:
    from ..body import bridge as cc
    try:
        h = cc.health()
    except Exception as e:
        return False, f"控制桥还没通（{type(e).__name__}）"
    if not h.get("ok"):
        return False, "控制桥返回 not ok"
    if not h.get("inGame"):
        return False, "客户端起来了，还在等进服/登录"
    return True, f"已进服：{h.get('player')}"


def _start_client() -> str:
    return _spawn([sys.executable, "-m", "mcrobot.ops.launch_client"], REPO, "launch_client.out")


def _close_screen() -> str:
    from ..body import bridge as cc
    cc.close()
    return "已按 ESC（等价）关掉登录/GUI 界面"


def _body_alive() -> bool:
    """用 cursor.json 的新鲜度判断比扫进程稳（与控制台同一判据）。"""
    try:
        return time.time() - (ROBOT_DIR / "cursor.json").stat().st_mtime < 15
    except Exception:
        return False


def _start_body() -> str:
    # 只让守护管身体：后端由本编排负责、控制台自己已经在跑，都不归它管
    return _spawn([sys.executable, "-m", "mcrobot.ops.supervisor", "start"],
                  REPO, "supervisor.out")


def _brain_running() -> bool:
    # 延迟导入：orchestrator 被 console 顶层导入，反过来顶层导入会成环。
    # 借控制台的判定 = 大脑只有一个「主人」，控制台的灯和这里看到的是同一个事实。
    from ..console import server as cs
    return cs._running("mcrobot.brain.llm_brain")


def _start_brain() -> str:
    from ..console import server as cs
    r = cs.brain_start()
    if not r.get("ok"):
        raise RuntimeError(r.get("msg") or "大脑启动失败")
    return r.get("msg") or ""


def real_deps(**overrides) -> Deps:
    # 必须最先灌 key：第一步就是「探后端」，探活要带 Authorization；而 key 只存在
    # secrets.env 里。放在 _spawn 里太晚——那会把「没带 key」误判成 401，一步就卡死。
    load_secrets()
    d = Deps(probe_backend=_probe_backend, start_backend=_start_backend,
             in_game=_in_game, start_client=_start_client, close_screen=_close_screen,
             body_alive=_body_alive, start_body=_start_body,
             brain_running=_brain_running, start_brain=_start_brain)
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


# ---------------- 状态落盘 ----------------
def _write(state: dict, path: Path | None = None) -> None:
    p = path or CHAIN
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)      # 原子替换：控制台不会读到写了一半的 json


def status(path: Path | None = None) -> dict:
    p = path or CHAIN
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"running": False, "steps": [], "ok": None, "error": None}


# ---------------- 编排 ----------------
def run(deps: Deps | None = None, stop=None, path: Path | None = None) -> dict:
    """跑一遍全链路。返回最终状态（同时已写盘）。

    stop 传一个 threading.Event 就能中途叫停（控制台的「停止」按钮）。
    """
    d = deps or real_deps()
    state: dict = {
        "running": True, "ok": None, "current": None, "error": None,
        "started": d.now(), "finished": None,
        "steps": [{"id": s["id"], "label": s["label"], "state": "pending",
                   "detail": "", "secs": None} for s in STEPS],
    }
    _write(state, path)

    def finish(ok: bool, err: str | None = None) -> dict:
        state.update(running=False, ok=ok, error=err, finished=d.now(), current=None)
        _write(state, path)
        return state

    for step, rec in zip(STEPS, state["steps"]):
        if stop is not None and stop.is_set():
            return finish(False, "已中止")

        # 1) 已就绪就跳过（第二遍点「一键」不会又开一个客户端）
        if not step.get("always"):
            try:
                if step["ready"](d):
                    rec.update(state="skipped", detail="已在运行，跳过")
                    _write(state, path)
                    continue
            except Exception as e:
                rec.update(detail=f"就绪探测异常：{type(e).__name__}")   # 探测不了就当作没就绪

        # 2) 拉起
        state["current"] = step["id"]
        rec["state"] = "running"
        t0 = d.now()
        _write(state, path)
        try:
            rec["detail"] = step["start"](d) or ""
        except Exception as e:
            rec.update(state="failed", detail=f"拉起失败：{e}", secs=round(d.now() - t0, 1))
            return finish(False, f"{step['label']}启动失败：{e}")

        # 3) 等就绪
        if not step.get("always"):
            deadline = t0 + step["timeout"]
            while True:
                if stop is not None and stop.is_set():
                    rec.update(state="failed", secs=round(d.now() - t0, 1))
                    return finish(False, "已中止")
                if step["ready"](d):
                    break
                if d.now() >= deadline:
                    why = ""
                    try:
                        r = step["ready"](d)
                        why = r[1] if isinstance(r, tuple) else ""
                    except Exception:
                        pass
                    rec.update(state="failed", secs=round(d.now() - t0, 1),
                               detail=f"等 {step['timeout']:g}s 没就绪" + (f"：{why}" if why else ""))
                    return finish(False, f"{step['label']}超时（{step['timeout']:g}s 未就绪）")
                d.sleep(POLL)
        rec.update(state="ok", secs=round(d.now() - t0, 1))
        _write(state, path)

    return finish(True)


def main(argv=None) -> int:
    print("[chain] 一键全链路：后端 → 客户端 → 关界面 → 身体 → 大脑")
    st = run()
    for s in st["steps"]:
        mark = {"ok": "✓", "skipped": "·", "failed": "✗", "pending": " "}.get(s["state"], "?")
        print(f"  [{mark}] {s['label']:8s} {s['state']:8s} {s['detail']}")
    print(("[chain] 全绿" if st["ok"] else f"[chain] 卡住：{st['error']}"))
    return 0 if st["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
