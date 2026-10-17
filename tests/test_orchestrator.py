"""全链路编排的判定逻辑——全部用假依赖 + 假时钟，不真启客户端、不碰网络。

真启一次客户端要 40–60 秒，不可能进单测。所以这里测的是**编排本身**：
顺序对不对、失败会不会停在原地、已就绪的会不会重复拉起。
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from mcrobot.ops import orchestrator as o


class Clock:
    """假时钟：sleep 推进虚拟时间，超时判定瞬间完成。"""
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Fake:
    """假依赖。`ready_after_start` 之外的一律「一开始就没就绪」，拉起后才就绪。"""
    def __init__(self, ready_now=(), never_ready=(), start_raises=""):
        self.clock = Clock()
        self.ready_now = set(ready_now)
        self.never_ready = set(never_ready)
        self.start_raises = start_raises
        self.started: list[str] = []

    def _ready(self, name: str) -> bool:
        if name in self.never_ready:
            return False
        return name in self.ready_now or name in self.started

    def _start(self, name: str) -> str:
        if self.start_raises == name:
            raise RuntimeError("模拟拉起失败")
        self.started.append(name)
        return f"已拉起 {name}"

    # --- 与 Deps 同名的方法 ---
    def probe_backend(self): return (self._ready("backend"), "" if self._ready("backend") else "不通")
    def start_backend(self): return self._start("backend")
    def in_game(self): return (self._ready("client"), "" if self._ready("client") else "还在等进服")
    def start_client(self): return self._start("client")
    def close_screen(self): return self._start("screen")
    def body_alive(self): return self._ready("body")
    def start_body(self): return self._start("body")
    def brain_running(self): return self._ready("brain")
    def start_brain(self): return self._start("brain")

    def deps(self) -> o.Deps:
        return o.Deps(probe_backend=self.probe_backend, start_backend=self.start_backend,
                      in_game=self.in_game, start_client=self.start_client,
                      close_screen=self.close_screen, body_alive=self.body_alive,
                      start_body=self.start_body, brain_running=self.brain_running,
                      start_brain=self.start_brain, now=self.clock.now, sleep=self.clock.sleep)


def _by_id(state: dict, sid: str) -> dict:
    return next(s for s in state["steps"] if s["id"] == sid)


class OrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, f: Fake, stop=None) -> dict:
        return o.run(deps=f.deps(), stop=stop, path=self.tmp / "chain.json")

    # --- 顺利路径 ---
    def test_cold_start_all_green(self):
        f = Fake()                                   # 一开始全没就绪 → 每步都要真拉起
        st = self._run(f)
        self.assertTrue(st["ok"], st)
        self.assertEqual(["backend", "client", "screen", "body", "brain"], f.started)
        for sid in ("backend", "client", "body", "brain"):
            self.assertEqual("ok", _by_id(st, sid)["state"], sid)

    def test_all_already_up_skips_everything(self):
        f = Fake(ready_now={"backend", "client", "body", "brain"})
        st = self._run(f)
        self.assertTrue(st["ok"])
        self.assertEqual(["screen"], f.started)      # 只有「关界面」是每拍都做的动作
        for sid in ("backend", "client", "body", "brain"):
            self.assertEqual("skipped", _by_id(st, sid)["state"], sid)

    # --- 失败要停在原地 ---
    def test_timeout_stops_at_that_step(self):
        f = Fake(never_ready={"client"})
        st = self._run(f)
        self.assertFalse(st["ok"])
        self.assertIn("超时", st["error"])
        self.assertEqual("failed", _by_id(st, "client")["state"])
        self.assertIn("180s", _by_id(st, "client")["detail"])
        # 后面三步一步都不许动
        self.assertNotIn("body", f.started)
        self.assertNotIn("brain", f.started)
        for sid in ("screen", "body", "brain"):
            self.assertEqual("pending", _by_id(st, sid)["state"], sid)

    def test_backend_down_never_reaches_client(self):
        f = Fake(never_ready={"backend"})
        st = self._run(f)
        self.assertFalse(st["ok"])
        # 后端拉了一次但始终没就绪 → 超时停住；客户端连拉都不该拉
        self.assertEqual(["backend"], f.started)
        self.assertIn("后端", st["error"])

    def test_start_failure_marks_failed(self):
        f = Fake(start_raises="screen")
        st = self._run(f)
        self.assertFalse(st["ok"])
        self.assertEqual("failed", _by_id(st, "screen")["state"])
        self.assertIn("模拟拉起失败", _by_id(st, "screen")["detail"])

    def test_stop_event_aborts_before_next_step(self):
        stop = threading.Event()
        stop.set()
        f = Fake()
        st = self._run(f, stop=stop)
        self.assertFalse(st["ok"])
        self.assertEqual("已中止", st["error"])
        self.assertEqual([], f.started)

    # --- 状态可读（控制台靠它画进度） ---
    def test_state_written_to_disk(self):
        f = Fake(ready_now={"backend", "client", "body", "brain"})
        self._run(f)
        on_disk = o.status(self.tmp / "chain.json")
        self.assertTrue(on_disk["ok"])
        self.assertFalse(on_disk["running"])
        self.assertEqual(5, len(on_disk["steps"]))

    def test_status_missing_file_is_safe(self):
        self.assertFalse(o.status(self.tmp / "nope.json")["running"])

    # --- 超时值不能比链路实际耗时短（客户端冷启动 40–60s，手册 §3.1） ---
    def test_client_timeout_covers_cold_boot(self):
        client = next(s for s in o.STEPS if s["id"] == "client")
        self.assertGreaterEqual(client["timeout"], 180)


if __name__ == "__main__":
    unittest.main()
