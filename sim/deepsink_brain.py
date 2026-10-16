#!/usr/bin/env python3
"""worker 侧：处理本地「DeepSink」服务（`deepsink_server.py`）的请求。

当 `deepsink.json:backend=bridge` 时，每个 chat 请求都会被写成
一个请求文件（`bridge.req_dir/req_xxx.json`），并在 HTTP 连接上**阻塞等待**回复。
本工具就是 worker 这一侧：把请求捞出来，把回复写回去，闭合回路。

**可并排开多个 worker**（多个 AI 窗口各跑一个 watch）：认领（claim）用
原子锁，同一条请求只会被一个 worker 接走，不会重复回单。详见 `deepsink_bridge.py`。

**多窗口称呼约定**：`<厂商>-<角色>-<序号>`（如 `codebuddy-main` / `workbuddy-1` /
`doubao-web-1`），每次 `watch`/`reply` 都显式 `--worker` 带上。别用默认的
`host-pid`——重启就变，事后没法追溯"这单是谁回的"。

**跨窗口沟通**：黑板留言 `say` / `msgs`（`data/msgs.jsonl`，追加式，所有窗口可读）；
在线登记 `ping`（`data/workers.json`）——`watch` 启动和 `reply` 成功时都会自动 ping，
`status` 会显示"在线窗口"。

用法（在 sim/ 目录下）：
  python deepsink_brain.py watch [--timeout 600] [--worker 名字]  # 阻塞认领一条；有则打印 JSON 退出(0)，超时退出(2)
  python deepsink_brain.py watch --no-claim                       # 只等不认领（旧行为，供排查）
  python deepsink_brain.py read                                   # 非阻塞：打印当前所有未处理请求
  python deepsink_brain.py status                                 # 队列快照：待认领/已认领/已回复 + 各 worker + 在线窗口
  python deepsink_brain.py reply <id> "回复文本" [--worker 名]      # 回单（自动清锁 + 心跳）
  python deepsink_brain.py reply <id> --file a.txt                # 从文件读回复文本
  python deepsink_brain.py release <id>                           # 放弃认领（不回复）
  python deepsink_brain.py say "留言"      [--worker 名]           # 黑板留言：给其它窗口递话（如"这单我接了"）
  python deepsink_brain.py msgs [--tail 20]                       # 读黑板最近 N 条
  python deepsink_brain.py ping             [--worker 名]           # 手动登记在线（watch/reply 会自动做）

回复也可以是完整 message 对象（含 tool_calls）——用 `reply <id> --json '{...}'`。

AI 工作流：起 watch（后台，自动登记在线）→ 被唤醒（已自动认领）→ 看 messages →
`reply` 回写（自动心跳）→ 再起 watch。需要和别的窗口递话用 `say`/`msgs`。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

import deepsink_bridge as bridge

BASE = Path(__file__).resolve().parent          # = sim/
CFG_PATH = BASE / "deepsink.json"


def default_worker() -> str:
    """本 worker 的默认标识：host-pid（区分并排开的多个窗口）。"""
    return f"{socket.gethostname()}-{os.getpid()}"


def _cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def _dirs() -> tuple[Path, Path]:
    return bridge.dirs(_cfg())


def _ttl() -> float:
    return bridge.default_ttl(_cfg())


def _expiry_limit() -> float:
    """请求的"还有客户端在等"上限（秒）。

    处理器最多等 min(gateway.patience_ms, bridge.timeout_seconds) 就会放弃并删掉
    请求文件；比这更老的请求不可能还有客户端在等——回它只是白费功夫，还会产生
    没人消费的孤儿回复文件。+5s 是时钟宽容差。
    """
    cfg = _cfg()
    b = cfg.get("bridge", {}) or {}
    gw = cfg.get("gateway", {}) or {}
    limits = [float(b.get("timeout_seconds", 120))]
    if gw.get("patience_ms"):
        limits.append(float(gw["patience_ms"]) / 1000.0)
    return min(limits) + 5.0


def _pending(req_dir: Path, res_dir: Path) -> list[dict]:
    """未处理的请求（无论是否已被认领）——保留旧语义，供 read / 测试用。"""
    if not req_dir.exists():
        return []
    out = []
    for p in sorted(req_dir.glob("req_*.json"), key=lambda x: x.stat().st_mtime):
        if (res_dir / p.name).exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        d["_file"] = p.name
        out.append(d)
    return out


def cmd_watch(timeout: float, block: bool = True, worker: str | None = None,
              claim: bool = True) -> int:
    req_dir, res_dir = _dirs()
    ttl = _ttl()
    worker = worker or default_worker()
    _ping(worker)                                     # 挂上 watch = 本窗口在线
    deadline = time.monotonic() + timeout
    next_ping = time.monotonic() + 30.0               # 等待期间续期，阻塞也算在线
    while True:
        if claim:
            d = bridge.claim_next(req_dir, res_dir, worker, ttl)
            if d:
                print(json.dumps([d], ensure_ascii=False, indent=2))
                return 0
        else:
            pend = _pending(req_dir, res_dir)
            if pend:
                print(json.dumps(pend, ensure_ascii=False, indent=2))
                return 0
        if not block or time.monotonic() >= deadline:
            print("[]")
            return 2 if block else 0
        if time.monotonic() >= next_ping:
            _ping(worker)
            next_ping = time.monotonic() + 30.0
        time.sleep(0.5)


def cmd_read() -> int:
    req_dir, res_dir = _dirs()
    print(json.dumps(_pending(req_dir, res_dir), ensure_ascii=False, indent=2))
    return 0


def cmd_status() -> int:
    req_dir, res_dir = _dirs()
    print(json.dumps(bridge.snapshot(req_dir, res_dir, _ttl()),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_reply(rid: str, text: str | None, as_json: str | None, worker: str | None = None) -> int:
    req_dir, res_dir = _dirs()
    if not rid.startswith("req_"):
        rid = "req_" + rid
    src = req_dir / f"{rid}.json"
    if not src.exists():
        print(f"[deepsink] 找不到请求 {rid}（可能已超时被客户端放弃，或从未存在）", file=sys.stderr)
        return 1
    # 过期拒回：客户端等待有上限，超龄请求早已无人认领——回它只会写成孤儿文件
    try:
        ts = float(json.loads(src.read_text(encoding="utf-8")).get("ts", 0) or 0)
    except Exception:
        ts = 0.0
    if ts > 0:
        age = time.time() - ts
        limit = _expiry_limit()
        if age > limit:
            print(f"[deepsink] 请求 {rid} 已过期（age={age:.0f}s > {limit:.0f}s），"
                  f"客户端早已放弃，不回复。", file=sys.stderr)
            return 1
    if as_json:
        try:
            payload = json.loads(as_json)
        except Exception as e:
            print(f"[deepsink] --json 解析失败：{e}", file=sys.stderr)
            return 1
    else:
        payload = {"text": text or ""}
    if isinstance(payload, dict):
        payload.setdefault("_worker", worker or default_worker())   # 归属留痕（服务端忽略）
    bridge.write_reply(req_dir, res_dir, rid, payload)
    _ping(worker or default_worker())                 # 回单成功 = 本窗口在线
    print(f"[deepsink] 已回复 {rid}（worker={worker or default_worker()}）: {str(payload)[:120]}")
    return 0


def _ping(worker: str) -> None:
    """登记心跳（失败不影响主流程——心跳只是协作便利，不是正确性依赖）。"""
    try:
        bridge.ping(_cfg(), worker)
    except Exception:
        pass


def cmd_say(text: str, worker: str | None = None) -> int:
    w = worker or default_worker()
    rec = bridge.say(_cfg(), w, text)
    _ping(w)
    print(f"[deepsink] [{rec['worker']}] {text}")
    return 0


def cmd_msgs(tail: int = 20) -> int:
    msgs = bridge.read_msgs(_cfg(), tail)
    print(json.dumps(msgs, ensure_ascii=False, indent=2))
    return 0


def cmd_ping(worker: str | None = None) -> int:
    w = worker or default_worker()
    bridge.ping(_cfg(), w)
    print(f"[deepsink] ping {w} -> {bridge.workers_path(_cfg())}")
    return 0


def cmd_release(rid: str) -> int:
    req_dir, _res = _dirs()
    if not rid.startswith("req_"):
        rid = "req_" + rid
    ok = bridge.release(req_dir, rid)
    print(f"[deepsink] {'已释放' if ok else '无锁可释放'} {rid}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deepsink_brain")
    sub = ap.add_subparsers(dest="action", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--timeout", type=float, default=600.0)
    w.add_argument("--worker", default=None)
    w.add_argument("--no-claim", dest="claim", action="store_false",
                   help="只等待、不认领（旧行为，排查用）")
    sub.add_parser("read")
    sub.add_parser("status")
    r = sub.add_parser("reply")
    r.add_argument("id")
    r.add_argument("text", nargs="?")
    r.add_argument("--file")
    r.add_argument("--json")
    r.add_argument("--worker", default=None)
    rel = sub.add_parser("release")
    rel.add_argument("id")
    s = sub.add_parser("say")
    s.add_argument("text")
    s.add_argument("--worker", default=None)
    m = sub.add_parser("msgs")
    m.add_argument("--tail", type=int, default=20)
    sub.add_parser("ping").add_argument("--worker", default=None)
    args = ap.parse_args(argv)
    if args.action == "watch":
        return cmd_watch(args.timeout, worker=args.worker, claim=args.claim)
    if args.action == "read":
        return cmd_read()
    if args.action == "status":
        return cmd_status()
    if args.action == "reply":
        text = args.text
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        return cmd_reply(args.id, text, args.json, worker=args.worker)
    if args.action == "release":
        return cmd_release(args.id)
    if args.action == "say":
        return cmd_say(args.text, worker=args.worker)
    if args.action == "msgs":
        return cmd_msgs(args.tail)
    if args.action == "ping":
        return cmd_ping(worker=args.worker)
    return 1


if __name__ == "__main__":
    sys.exit(main())
