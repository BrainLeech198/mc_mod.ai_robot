#!/usr/bin/env python3
"""ControlMod 本地 HTTP 桥的最小客户端（stdlib，无依赖）。"""
import argparse
import datetime
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ..paths import REPO


def http_port():
    cfg = json.loads((REPO / "version" / "1.20.1-forge" / "runtime.json").read_text(encoding="utf-8"))
    return int(cfg["control_mod"]["http_port"])


def token():
    return (REPO / "instance" / "control_token").read_text(encoding="utf-8").strip()


class ControlError(Exception):
    pass


def _request(method, path, body=None):
    url = f"http://127.0.0.1:{http_port()}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Control-Token", token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(payload).get("error", payload)
        except Exception:
            err = payload
        raise ControlError(f"HTTP {e.code}: {err}") from None
    except urllib.error.URLError as e:
        raise ControlError(f"无法连接 ControlMod（客户端未运行或端口未开？）: {e.reason}") from None


def health():
    return _request("GET", "/health")


def state():
    return _request("GET", "/state")


def chat(text):
    return _request("POST", "/chat", {"text": text})


def chat_since(since=0):
    """读回 seq > since 的聊天回执。"""
    return _request("GET", f"/chat?since={int(since)}")


def cmd(command):
    return _request("POST", "/command", {"command": command})


def block_at(x, y, z):
    """读单个方块。loaded=false 表示区块没加载，不能当空气。"""
    return _request("GET", f"/blocks?x={int(x)}&y={int(y)}&z={int(z)}")


def blocks_in_box(x1, y1, z1, x2, y2, z2, limit=512):
    """读长方体区域（含端点）。服务端上限 4096 格。"""
    q = (f"/blocks?x1={int(x1)}&y1={int(y1)}&z1={int(z1)}"
         f"&x2={int(x2)}&y2={int(y2)}&z2={int(z2)}&limit={int(limit)}")
    return _request("GET", q)


def looking_at():
    """准星指向的方块/实体。"""
    return _request("GET", "/looking")


def _request_bytes(path):
    """和 _request 一样，但返回原始字节（截图不能走 JSON）。"""
    url = f"http://127.0.0.1:{http_port()}{path}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Control-Token", token())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read(), r.headers
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(payload).get("error", payload)
        except Exception:
            err = payload
        raise ControlError(f"HTTP {e.code}: {err}") from None
    except urllib.error.URLError as e:
        raise ControlError(f"无法连接 ControlMod（客户端未运行或端口未开？）: {e.reason}") from None


def screenshot(max_width=854, out_dir=None):
    """截一张当前画面，存成 PNG，返回路径与尺寸。

    截图在客户端渲染线程抓帧，不含 HUD（AFTER_LEVEL 阶段），不需要 OP。
    max_width 会等比缩小，默认 854（给视觉模型省 token）。
    """
    data, headers = _request_bytes(f"/screen.png?max={int(max_width)}")
    if out_dir is None:
        out_dir = REPO / "instance" / "robot" / "shots"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    path = out_dir / f"shot-{stamp}.png"
    path.write_bytes(data)
    try:
        shown = str(path.relative_to(REPO))
    except ValueError:
        shown = str(path)
    return {
        "path": shown,
        "abs_path": str(path),
        "width": int(headers.get("X-Image-Width") or 0),
        "height": int(headers.get("X-Image-Height") or 0),
        "bytes": len(data),
        "capture_ms": int(headers.get("X-Capture-Ms") or 0),
    }


def close():
    return _request("POST", "/screen/close")


def main(argv=None):
    p = argparse.ArgumentParser(prog="control_client")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("health")
    sub.add_parser("state")
    sub.add_parser("close")
    sc = sub.add_parser("chat"); sc.add_argument("text")
    ss = sub.add_parser("chat-since"); ss.add_argument("since", type=int, nargs="?", default=0)
    sm = sub.add_parser("cmd"); sm.add_argument("command")
    args = p.parse_args(argv)
    try:
        if args.action == "health":
            out = health()
        elif args.action == "state":
            out = state()
        elif args.action == "chat":
            out = chat(args.text)
        elif args.action == "chat-since":
            out = chat_since(args.since)
        elif args.action == "cmd":
            out = cmd(args.command)
        else:
            out = close()
    except ControlError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
