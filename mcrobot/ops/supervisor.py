#!/usr/bin/env python3
"""机器人链路守护进程：保证「身体」常驻，挂了自动重拉。

为什么需要它：
robot.py 是常驻身体，但它自己没有自愈能力——崩一次（控制桥抖动、异常、
误杀）整条链就断了，表现是「机器人不说话了」。以前靠人肉发现+手拉，不可靠。
本守护只管**无需人类判断**的部分：robot.py，以及可选的 DeepSink / API 大脑。

**大脑不归它管**：人肉大脑（AI 窗口）另有自己的循环，见手册 §0。
两个进程不能同时消费 inbox（会抢记录），所以 supervisor 默认不拉 brain_llm。

用法：
  python -m mcrobot.ops.supervisor start [--with-deepsink] [--with-console] [--with-api-brain]
  python -m mcrobot.ops.supervisor status  # 看受管进程是否活着 + 最近重启次数
  python -m mcrobot.ops.supervisor stop    # 停掉本守护拉起的全部子进程

环境变量 MCROBOT_LLM_KEY：给 --with-api-brain 用（伪 API 端点鉴权）。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..paths import REPO, SIM
RUNTIME = REPO / "instance" / "robot"
STATE = RUNTIME / "supervisor.json"

# 名称 -> (cwd, 命令, 日志)
JOBS: dict[str, tuple[Path, list[str], str]] = {
    "robot": (REPO, [sys.executable, "-u", "-m", "mcrobot.body.loop", "--interval", "1.0"], "loop.out"),
    "deepsink": (SIM, [sys.executable, "-u", "deepsink_server.py", "serve", "--backend", "bridge",
                       "--port", "8788"], "deepsink.out"),
    "console": (REPO, [sys.executable, "-u", "-m", "mcrobot.console.server",
                       "--port", "8901"], "console.out"),
}
API_BRAIN = (REPO, [sys.executable, "-u", "-m", "mcrobot.brain.llm_brain", "watch",
                  "--timeout", "7200"], "watch.out")

RESTART_DELAY = 3.0     # 重启前等一会儿，避免崩溃循环打满 CPU
CHECK_INTERVAL = 2.0


class Proc:
    def __init__(self, name: str, cwd: Path, cmd: list[str], log: str):
        self.name = name
        self.cwd = cwd
        self.cmd = cmd
        self.log = RUNTIME / log
        self.proc: subprocess.Popen | None = None
        self.restarts = 0

    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        f = self.log.open("a", encoding="utf-8")
        env = dict(os.environ)
        self.proc = subprocess.Popen(
            self.cmd, cwd=str(self.cwd), stdout=f, stderr=subprocess.STDOUT,
            env=env, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.restarts += 1
        _log(f"start {self.name} pid={self.proc.pid} restarts={self.restarts}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self) -> None:
        if not self.alive():
            _log(f"{self.name} 已退出，{RESTART_DELAY}s 后重拉")
            time.sleep(RESTART_DELAY)
            self.start()

    def stop(self) -> None:
        if self.alive():
            self.proc.terminate()   # type: ignore[union-attr]
            try:
                self.proc.wait(timeout=5)   # type: ignore[union-attr]
            except subprocess.TimeoutExpired:
                self.proc.kill()   # type: ignore[union-attr]
            _log(f"stop {self.name}")


def _log(msg: str) -> None:
    print(f"[supervisor {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_jobs(with_deepsink: bool, with_api_brain: bool, with_console: bool) -> list[Proc]:
    jobs = [Proc("robot", *JOBS["robot"])]
    if with_deepsink:
        jobs.append(Proc("deepsink", *JOBS["deepsink"]))
    if with_console:
        jobs.append(Proc("console", *JOBS["console"]))
    if with_api_brain:
        if not os.environ.get("MCROBOT_LLM_KEY"):
            _log("警告：没设 MCROBOT_LLM_KEY，API 大脑会鉴权失败")
        if any(j.name == "bridge_watch" for j in jobs):
            _log("警告：bridge 大脑与 API 大脑不能同时消费 inbox")
        jobs.append(Proc("brain_llm", *API_BRAIN))
    return jobs


def _save_state(jobs: list["Proc"], started: float) -> None:
    """状态落盘给 status 命令读。重拉后 pid 会变，必须每次都刷新，
    否则 status 读到的是启动时那个早就死掉的旧 pid。"""
    STATE.write_text(json.dumps({
        "supervisor_pid": os.getpid(),
        "started": started,
        "jobs": [{"name": j.name, "pid": j.proc.pid if j.proc else None,
                  "restarts": j.restarts} for j in jobs],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_start(args) -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    jobs = build_jobs(args.with_deepsink, args.with_api_brain, args.with_console)
    started = time.time()
    for j in jobs:
        j.start()
    _save_state(jobs, started)

    stop = False

    def _sig(signum, _frame):
        nonlocal stop
        stop = True
        _log(f"收到信号 {signum}，收工")

    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _sig)

    _log(f"守护中：{', '.join(j.name for j in jobs)}（Ctrl+C 停止）")
    while not stop:
        for j in jobs:
            j.ensure()
        _save_state(jobs, started)
        time.sleep(CHECK_INTERVAL)
    for j in jobs:
        j.stop()
    STATE.unlink(missing_ok=True)
    return 0


def cmd_status(_args) -> int:
    if not STATE.exists():
        print("守护进程没在跑（无 supervisor.json）")
        return 1
    d = json.loads(STATE.read_text(encoding="utf-8"))
    print(f"守护 pid={d['supervisor_pid']}  启动于 {time.strftime('%H:%M:%S', time.localtime(d['started']))}")
    for j in d["jobs"]:
        pid = j["pid"]
        alive = _pid_alive(pid) if pid else False
        print(f"  {j['name']:10s} pid={pid} {'活着' if alive else '已死'}"
              f"  重启次数={j.get('restarts', '?')}")
    return 0


def _pid_alive(pid: int) -> bool:
    """Windows 上 tasklist 输出是 GBK（中文系统），按文本解码会崩——直接比字节。"""
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True)
        return str(pid).encode() in r.stdout
    except Exception:
        return False


def cmd_stop(_args) -> int:
    if not STATE.exists():
        print("守护进程没在跑")
        return 1
    d = json.loads(STATE.read_text(encoding="utf-8"))
    try:
        os.kill(d["supervisor_pid"], signal.SIGTERM)
        print(f"已通知守护 {d['supervisor_pid']} 停止")
    except OSError as e:
        print(f"通知失败（可能已退出）：{e}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="机器人链路守护")
    sub = ap.add_subparsers(dest="action", required=True)
    st = sub.add_parser("start")
    st.add_argument("--with-deepsink", action="store_true", help="一并守护 DeepSink 伪 API 服务")
    st.add_argument("--with-console", action="store_true", help="一并守护网页控制台（:8901）")
    st.add_argument("--with-api-brain", action="store_true",
                    help="一并守护 brain_llm（走伪 API 的大脑；与人类大脑二选一，别同时开）")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = ap.parse_args(argv)
    if args.action == "start":
        return cmd_start(args)
    if args.action == "status":
        return cmd_status(args)
    return cmd_stop(args)


if __name__ == "__main__":
    raise SystemExit(main())
