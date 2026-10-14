#!/usr/bin/env python3
"""ControlMod 本地 HTTP 桥的最小客户端（stdlib，无依赖）。"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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


def cmd(command):
    return _request("POST", "/command", {"command": command})


def close():
    return _request("POST", "/screen/close")


def main(argv=None):
    p = argparse.ArgumentParser(prog="control_client")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("health")
    sub.add_parser("state")
    sub.add_parser("close")
    sc = sub.add_parser("chat"); sc.add_argument("text")
    sm = sub.add_parser("cmd"); sm.add_argument("command")
    args = p.parse_args(argv)
    try:
        if args.action == "health":
            out = health()
        elif args.action == "state":
            out = state()
        elif args.action == "chat":
            out = chat(args.text)
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
