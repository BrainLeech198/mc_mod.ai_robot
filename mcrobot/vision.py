#!/usr/bin/env python3
"""看图：截图 → 多模态模型 → 一段文字描述。

为什么单独一层：L1 大脑是纯文本模型（便宜、快、6~10 秒能回话），但「砌得歪不歪、
颜色难不难看、门洞留没留」只能看。把看图做成按需调用的工具（`look`）：
文本大脑仍然做决策，需要眼睛时才花一次多模态请求。

和 `screenshot` 的分工：
- `screenshot`  只抓帧存盘，返回路径（给玩家看 / 给未来原生多模态大脑当附件）
- `look`        抓帧 + 问视觉模型 + 回一段文字（给现在的文本大脑用）

配置在 `version/<版本>/robot.json:vision`（key 只从环境变量读，不进 git）：
  enabled          总开关（默认 false，没配多模态后端时别误触发）
  endpoint/model   留空则复用 llm_api 的端点与模型（同后端支持多模态时最省事）
  key_env          默认 MCROBOT_LLM_KEY；也可单独设 MCROBOT_VISION_KEY
  max_width        截图宽度，默认 854（省 token）
  detail           OpenAI 图片细节档位：auto / low / high

用法：
  python -m mcrobot.vision check                     # 用 1×1 图探上游吃不吃图
  python -m mcrobot.vision shot "墙砌得直吗"          # 现截一张再问（客户端要在游戏里）
  python -m mcrobot.vision describe --file shot.png "描述画面"
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .paths import REPO, load_secrets

DEFAULT_QUESTION = ("描述这张 Minecraft 截图：你在哪、面前是什么、建筑/结构是否完整。"
                    "有明显缺陷就直说（缺角、悬空、颜色不搭、门洞没留）。120 字内。")
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 1×1 透明 PNG（用于探活，不用碰游戏）
TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class VisionError(Exception):
    pass


def cfg(version: str | None = None) -> dict:
    """读版本配置。vision 段留空的字段回落到 llm_api。"""
    try:
        from .brain.llm_brain import load_cfg
        raw = load_cfg(version)
    except Exception:
        raw = {}
    v = dict(raw.get("vision") or {})
    api = dict(raw.get("llm_api") or {})
    return {
        "enabled": bool(v.get("enabled")),
        "endpoint": (v.get("endpoint") or api.get("endpoint") or "").rstrip("/"),
        "model": v.get("model") or api.get("model") or "",
        "key_env": v.get("key_env") or "MCROBOT_LLM_KEY",
        "max_width": int(v.get("max_width") or 854),
        "detail": v.get("detail") or "auto",
        "timeout": float(v.get("timeout_seconds") or 60),
        "max_tokens": int(v.get("max_tokens") or 300),
    }


def _api_key(c: dict) -> str:
    load_secrets()
    return (os.environ.get(c["key_env"] or "")
            or os.environ.get("MCROBOT_VISION_KEY", "")
            or os.environ.get("MCROBOT_LLM_KEY", ""))


def describe(image_path: str, question: str = "", *, version: str | None = None) -> dict:
    """问视觉模型。返回 {text, model, ms, path, bytes}；配置不全抛 VisionError。"""
    from pathlib import Path
    p = Path(image_path)
    if not p.exists():
        raise VisionError(f"图片不存在：{image_path}")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        raise VisionError(f"图片太大（{p.stat().st_size // 1024} KB），先缩到 854 宽再问")
    out = describe_bytes(p.read_bytes(), question, version=version)
    out["path"] = str(p)
    return out


def describe_bytes(data: bytes, question: str = "", *,
                   version: str | None = None, mime: str = "image/png",
                   force: bool = False) -> dict:
    """底层：给一段图片字节，返回模型的说法。探活也走这条。

    force=True 时忽略 enabled 开关——探活必须能在「还没启用」时先问清楚上游吃不吃图，
    否则顺序反了：先开 → 每次调用才报错。
    """
    c = cfg(version)
    if not c["enabled"] and not force:
        raise VisionError("vision 未启用：在 robot.json 的 vision 段设 enabled=true，"
                          "并填 endpoint/model（留空则复用 llm_api）")
    if not c["endpoint"] or not c["model"]:
        raise VisionError("vision 缺 endpoint 或 model：robot.json:vision 或 llm_api 要有一份")
    key = _api_key(c)
    if not key:
        raise VisionError(f"没有 API key：设环境变量 {c['key_env']}（或写进 instance/robot/secrets.env）")
    if not data:
        raise VisionError("图片是空的")

    b64 = base64.b64encode(data).decode("ascii")
    payload = {
        "model": c["model"],
        "stream": False,
        "max_tokens": c["max_tokens"],
        "temperature": 0.2,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": (question or DEFAULT_QUESTION).strip()},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}", "detail": c["detail"]}},
            ],
        }],
    }
    req = urllib.request.Request(
        c["endpoint"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=c["timeout"]) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        hint = ("。上游可能不支持图片输入：换一个多模态模型/端点，或把 vision.enabled 设 false"
                if e.code in (400, 404, 415, 422) else "")
        raise VisionError(f"视觉请求失败 HTTP {e.code}：{detail}{hint}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VisionError(f"视觉请求网络错误：{e}")

    try:
        msg = body["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        finish = body["choices"][0].get("finish_reason") or ""
    except Exception:
        raise VisionError(f"视觉接口回了看不懂的结构：{str(body)[:200]}")
    if not text:
        # 推理型模型会把 max_tokens 全花在 reasoning_content 上，content 空但没报错。
        # 不兜底拼思考过程（太长且不是给工具用的），直接把原因说清楚。
        if msg.get("reasoning_content"):
            raise VisionError("视觉模型只输出了推理、没给结论：max_tokens 太小或该模型不适合看图，"
                              "调大 robot.json:vision.max_tokens 或换非推理的多模态模型")
        if finish == "length":
            raise VisionError("视觉模型输出被截断（finish_reason=length）：调大 vision.max_tokens")
        raise VisionError("视觉模型回了空内容（可能这个模型其实不支持图片）")
    return {"text": text, "model": c["model"], "ms": int((time.monotonic() - t0) * 1000),
            "bytes": len(data)}


def check(version: str | None = None) -> dict:
    """探活：把 1×1 图发给当前后端，看它到底吃不吃图（不需要先 enable）。"""
    t0 = time.monotonic()
    out = describe_bytes(TINY_PNG, "只回两个字：收到", version=version, force=True)
    out["total_ms"] = int((time.monotonic() - t0) * 1000)
    return out


# -------------------------------- CLI --------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vision")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="用 1×1 图探上游是否支持图片")
    sh = sub.add_parser("shot", help="现截一张再问")
    sh.add_argument("question", nargs="?", default="")
    sh.add_argument("--max-width", type=int, default=0)
    de = sub.add_parser("describe", help="问已有图片")
    de.add_argument("--file", required=True)
    de.add_argument("question", nargs="?", default="")
    args = ap.parse_args(argv)

    if args.action == "check":
        c = cfg()
        print(f"端点 {c['endpoint'] or '（空）'} / 模型 {c['model'] or '（空）'} / 启用 {c['enabled']}")
        try:
            out = check()
        except VisionError as e:
            print(f"不吃图：{e}")
            return 1
        print(f"吃图 ✓ {out['ms']}ms → {out['text'][:80]}")
        return 0

    if args.action == "shot":
        from .body import bridge as cc
        width = args.max_width or cfg()["max_width"]
        shot = cc.screenshot(max_width=width)
        try:
            out = describe(shot["abs_path"], args.question)
        except VisionError as e:
            print(f"看图失败：{e}（图在 {shot['path']}）")
            return 1
        print(f"{out['text']}\n（{shot['width']}×{shot['height']} · {out['ms']}ms · {shot['path']}）")
        return 0

    if args.action == "describe":
        try:
            out = describe(args.file, args.question)
        except VisionError as e:
            print(f"看图失败：{e}")
            return 1
        print(f"{out['text']}\n（{out['ms']}ms · {out['path']}）")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
