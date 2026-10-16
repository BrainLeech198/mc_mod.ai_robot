#!/usr/bin/env python3
"""bridge 队列的共享工具：多 worker 认领（claim）与队列快照（纯标准库）。

`deepsink.json:backend=bridge` 时，服务端把每个 chat 请求写成一个请求文件，
在 HTTP 连接上阻塞等待回复。**"谁来思考"就是从这里取单的 worker**——
可以是本机的一个 AI 窗口，也可以是**并排开的多个窗口（多个 CodeBuddy）**。

文件协议（`req_dir`/`res_dir` 由 `deepsink.json:bridge` 指定）：

    req_dir/req_xxx.json    服务端写入的请求（worker 只读，永不改写）
    req_dir/req_xxx.lock    worker 的**认领标记**（O_CREAT|O_EXCL 原子创建）
    res_dir/req_xxx.json    worker 写回的回复（服务端轮询到后消费并删除）

认领用 `os.open(..., O_CREAT|O_EXCL)`——POSIX / Windows 都保证**只有一个成功**，
因此无论开多少个 worker，同一条请求只会被一个人接走，不会重复回单。

锁超过 `ttl` 秒仍未被回复，视为该 worker 失联（崩溃/超时），可被他人**抢占**，
避免请求永久卡在"已被认领"状态。`claim_next` 每次轮询还会自愈清理
"请求已不存在"的孤儿锁（worker 在服务端放弃请求的瞬间抢单会产生）。

除队列外还有两块**多窗口协作设施**（同目录 `data/` 下）：

    data/msgs.jsonl     黑板：追加式留言（say），所有 worker 可读（msgs）
    data/workers.json   心跳：worker 的 last_seen（ping），status 据此显示"在线窗口"

窗口之间的**称呼**约定：`<厂商>-<角色>-<序号>`（如 `codebuddy-main` / `doubao-web-1`），
显式传 `--worker`；别用默认的 host-pid——重启就变，没法追溯"这单是谁回的"。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def dirs(cfg: dict) -> tuple[Path, Path]:
    """按配置解析 (req_dir, res_dir) 的绝对路径（相对 sim/）。"""
    base = Path(__file__).resolve().parent
    b = cfg.get("bridge", {}) or {}
    req = Path(b.get("req_dir", "data/req"))
    res = Path(b.get("res_dir", "data/res"))
    return (req if req.is_absolute() else base / req,
            res if res.is_absolute() else base / res)


def default_ttl(cfg: dict) -> float:
    b = cfg.get("bridge", {}) or {}
    # 默认取请求超时 + 余量；至少 60s
    return float(b.get("claim_ttl_seconds", max(60.0, float(b.get("timeout_seconds", 120)) * 1.5)))


def _resolve(cfg: dict, key: str, default: str) -> Path:
    b = cfg.get("bridge", {}) or {}
    p = Path(b.get(key, default))
    base = Path(__file__).resolve().parent
    return p if p.is_absolute() else base / p


def msgs_path(cfg: dict) -> Path:
    """黑板文件（追加式 JSONL）。"""
    return _resolve(cfg, "msg_file", "data/msgs.jsonl")


def workers_path(cfg: dict) -> Path:
    """心跳文件（worker -> last_seen/pid）。"""
    return _resolve(cfg, "workers_file", "data/workers.json")


def heartbeat_ttl(cfg: dict) -> float:
    b = cfg.get("bridge", {}) or {}
    return float(b.get("heartbeat_ttl_seconds", 90))


def _req_files(req_dir: Path) -> list[Path]:
    if not req_dir.exists():
        return []
    return sorted(req_dir.glob("req_*.json"), key=lambda p: p.stat().st_mtime)


def _lock_path(req_dir: Path, rid: str) -> Path:
    return req_dir / f"{rid}.lock"


def _answered(res_dir: Path, rid: str) -> bool:
    return (res_dir / f"{rid}.json").exists()


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _lock_info(lp: Path) -> dict:
    d = _read_json(lp)
    return d if isinstance(d, dict) else {}


def _stale(lp: Path, ttl: float) -> bool:
    """锁是否已过期（worker 失联）。读不到时间戳 → 视为过期可抢占。"""
    info = _lock_info(lp)
    try:
        ts = float(info.get("ts", 0))
    except Exception:
        return True
    return (time.time() - ts) > ttl


def _try_lock(lp: Path, worker: str, ttl: float) -> bool:
    """原子认领：成功返回 True。锁存在且未过期 → False；已过期 → 抢占。"""
    for attempt in (0, 1):
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 0 and _stale(lp, ttl):
                try:
                    lp.unlink()
                except OSError:
                    return False
                continue
            return False
        except OSError:
            return False
        try:
            os.write(fd, json.dumps({"worker": worker, "ts": time.time(),
                                     "pid": os.getpid()}).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    return False


def _sweep_orphan_locks(req_dir: Path) -> None:
    """自愈：清掉"请求文件已不存在"的认领锁。

    竞竞态下会产生这种孤儿：worker 在服务端放弃请求（删 req）的同一瞬间抢单，
    锁落盘时请求已经没了。锁本身无害（没有请求可认领），但会堆积——
    每个 worker 的每次轮询都顺手扫掉，队列永不留影子。
    """
    for lp in req_dir.glob("req_*.lock"):
        if not (req_dir / f"{lp.stem}.json").exists():
            try:
                lp.unlink()
            except OSError:
                pass


def claim_next(req_dir: Path, res_dir: Path, worker: str, ttl: float) -> dict | None:
    """认领最早的一条未处理请求；返回请求 dict（附 `_claimed`/`_worker`）或 None。"""
    _sweep_orphan_locks(req_dir)
    for p in _req_files(req_dir):
        rid = p.stem
        if _answered(res_dir, rid):
            continue
        lp = _lock_path(req_dir, rid)
        if lp.exists() and not _stale(lp, ttl):
            continue                      # 别人正拿着且未超时
        if not _try_lock(lp, worker, ttl):
            continue                      # 抢输了 / 锁无法删除
        d = _read_json(p)
        if not isinstance(d, dict):
            release(req_dir, rid)         # 坏文件：释放锁，跳过
            continue
        d["_claimed"] = True
        d["_worker"] = worker
        return d
    return None


def claim_one(req_dir: Path, res_dir: Path, rid: str, worker: str, ttl: float) -> bool:
    """认领指定请求（供 `reply` 直接补锁用）。已被他人有效认领时返回 False。"""
    lp = _lock_path(req_dir, rid)
    if (req_dir / f"{rid}.json").exists() is False:
        return False
    if lp.exists() and not _stale(lp, ttl):
        info = _lock_info(lp)
        return info.get("worker") == worker
    return _try_lock(lp, worker, ttl)


def release(req_dir: Path, rid: str) -> bool:
    """放弃认领（删锁）。"""
    try:
        _lock_path(req_dir, rid).unlink()
        return True
    except OSError:
        return False


def write_reply(req_dir: Path, res_dir: Path, rid: str, payload: dict) -> None:
    """写回复文件并释放认领锁。服务端轮询到后会消费并删除回复文件。"""
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / f"{rid}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    release(req_dir, rid)


# ---- 黑板（跨窗口留言）----

def say(cfg: dict, worker: str, text: str) -> dict:
    """往黑板追加一条留言（所有窗口都能读）。返回该条记录。"""
    mp = msgs_path(cfg)
    mp.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "worker": worker, "text": str(text)}
    with open(mp, "a", encoding="utf-8") as f:      # O_APPEND：小行追加即可视为原子
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read_msgs(cfg: dict, tail: int = 20) -> list[dict]:
    """读黑板最近 tail 条（旧→新）。文件不存在/损坏的行跳过。"""
    mp = msgs_path(cfg)
    if not mp.exists():
        return []
    out = []
    try:
        lines = mp.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out[-max(0, int(tail)):] if tail else out


# ---- 心跳（窗口在线登记）----

def ping(cfg: dict, worker: str) -> dict:
    """登记/刷新本 worker 的心跳。过期（>1 天）的记录顺手清掉。"""
    wp = workers_path(cfg)
    now = time.time()
    try:
        data = _read_json(wp)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
    data = {k: v for k, v in data.items()
            if isinstance(v, dict) and now - float(v.get("last_seen", 0) or 0) < 86400}
    data[worker] = {"last_seen": now, "pid": os.getpid()}
    wp.parent.mkdir(parents=True, exist_ok=True)
    tmp = wp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, wp)                              # 原子替换，读方永远看到完整 JSON
    return data[worker]


def online_workers(cfg: dict, workers_file: Path | None = None,
                   ttl: float | None = None) -> list[dict]:
    """最近 ttl 秒内心跳过的 worker（按名字排序）。"""
    wp = workers_file or workers_path(cfg)
    ttl = heartbeat_ttl(cfg) if ttl is None else ttl
    data = _read_json(wp) if wp.exists() else None
    if not isinstance(data, dict):
        return []
    now = time.time()
    out = []
    for name, v in data.items():
        if not isinstance(v, dict):
            continue
        try:
            ls = float(v.get("last_seen", 0) or 0)
        except (TypeError, ValueError):
            continue
        if now - ls <= ttl:
            out.append({"worker": name, "last_seen": ls,
                        "age_seconds": round(now - ls, 1),
                        "pid": v.get("pid")})
    return sorted(out, key=lambda x: x["worker"])


def snapshot(req_dir: Path, res_dir: Path, ttl: float,
             workers_file: Path | None = None, online_ttl: float | None = None,
             hb_cfg: dict | None = None) -> dict:
    """队列快照：待认领 / 已被认领 / 已回复 数量与明细，各 worker 接单数，以及在线窗口。

    workers_file 缺省从 req_dir 推断（`../workers.json`），供旧调用方兼容。
    """
    now = time.time()
    pending, claimed, answered = [], [], 0
    workers: dict[str, dict] = {}
    for p in _req_files(req_dir):
        rid = p.stem
        age = max(0.0, now - p.stat().st_mtime)
        if _answered(res_dir, rid):
            answered += 1
            continue
        lp = _lock_path(req_dir, rid)
        info = _lock_info(lp) if lp.exists() else {}
        if lp.exists() and not _stale(lp, ttl):
            w = str(info.get("worker") or "?")
            item = {"id": rid, "worker": w,
                    "held_seconds": round(now - float(info.get("ts", now)), 1)}
            if info.get("pid"):
                item["pid"] = info["pid"]
            claimed.append(item)
            slot = workers.setdefault(w, {"claimed": 0, "last_id": rid})
            slot["claimed"] += 1
            slot["last_id"] = rid
        else:
            d = _read_json(p) or {}
            pending.append({"id": rid, "age_seconds": round(age, 1),
                            "last_user": str(d.get("last_user", ""))[:60]})
    wf = workers_file or (req_dir.parent / "workers.json")
    online = online_workers(hb_cfg or {}, wf, online_ttl)
    return {"pending": pending, "claimed": claimed, "answered": answered,
            "workers": [{"worker": k, **v} for k, v in sorted(workers.items())],
            "online": online, "claim_ttl_seconds": ttl}
