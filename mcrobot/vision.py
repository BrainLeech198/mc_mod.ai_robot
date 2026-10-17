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


# ------------------------------ 验收（砌完自检） ------------------------------
# 分工：成败由 block_at/scan_region 定（世界证据），这里只回答「好不好看」——
# 轮廓齐不齐、墙线直不直、配色搭不搭、是不是呆方盒。所以结论只有三种：
#   pass    五项都过，没意见
#   fix     有具体整改意见（issues 里 1~2 条，suggest 必须能用工具做到）
#   unclear 照片无效/输出没法解析——**不产意见，也不判成败**
# 视觉会说错方块名、给不出准确坐标，所以它的意见只能当「下一步该看哪」的线索，
# 永远不能当世界证据，也不能把 ok 洗成 fail 或把 fail 洗成 ok。

RUBRIC: tuple[tuple[int, str, str], ...] = (
    (1, "轮廓完整", "外轮廓没有缺口、破洞、悬空"),
    (2, "边缘整齐", "墙线基本平直，没有明显凸出歪斜"),
    (3, "配色协调", "主色不超过 3 种，没有突兀色块"),
    (4, "有层次", "有立柱/出檐/门窗等细节，不是纯长方体（0-2 分）"),
    (5, "照片有效", "能看清主体，不是全黑、被界面或天气糊住"),
)

REVIEW_SYSTEM = """你是 Minecraft 建筑验收员。给你一张建造完成后的截图，按下面五项评分表逐项打分。
只依据图片本身判断：不许猜坐标、方块 ID、数量——你说不出具体是什么砖。

评分表：
1 轮廓完整：外轮廓没有缺口、破洞、悬空（是/否）
2 边缘整齐：墙线基本平直，没有明显凸出歪斜（是/否）
3 配色协调：主色不超过 3 种，没有突兀色块（是/否）
4 有层次：有立柱/出檐/门窗等细节，不是纯长方体（0-2 分）
5 照片有效：能看清主体，不是全黑、被界面或天气糊住（是/否）

输出**只能**是一行 JSON：
{"valid": true, "items": {"1": {"ok": true, "note": ""}, "4": {"score": 1, "note": "就一长方体"}},
 "verdict": "pass|fix|unclear", "issues": [{"item": 2, "what": "差在哪", "suggest": "能用工具做到的动作"}]}

规则：
- 第 5 项为 false 时，verdict 必须是 unclear，issues 必须为空，并在第 5 项的 note 里说明照片为什么无效。
- **第 1~4 项，note 必须写出你据以判断的画面里看得见的具体部位**（如「正面第二根白柱」「屋顶出檐」「南墙中央的门洞」）；
  写不出具体部位的，该项一律判 false（第 4 项 score=0）——没看见就等于没有。
- 只有第 1~4 项里有 false 或 score<2 时，verdict 才是 fix；否则 pass。
- issues 最多 2 条；what 说清差在哪，suggest 必须具体到能用工具做（不许写「再修修」「改进一下」）。
- 不许输出 JSON 之外的任何文字。"""


def _parse_review(raw: str) -> dict:
    """把模型的评分输出归一成 {verdict, items, issues, note}。解析失败一律 unclear。"""
    from .brain.llm_policy import _extract_json_object
    obj = _extract_json_object(raw or "")
    names = {n: t for n, t, _ in RUBRIC}
    items = [{"item": n, "name": t, "ok": None, "score": None, "note": ""}
             for n, t, _ in RUBRIC]
    by_n = {i["item"]: i for i in items}
    verdict, issues, note = "unclear", [], ""
    if obj:
        valid = obj.get("valid")
        got = obj.get("items")
        if isinstance(got, dict):
            for k, v in got.items():
                try:
                    n = int(k)
                except (TypeError, ValueError):
                    continue
                if n not in by_n or not isinstance(v, dict):
                    continue
                it = by_n[n]
                it["note"] = str(v.get("note") or "")[:120]
                if n == 5:
                    it["ok"] = bool(v.get("ok", valid if valid is not None else True))
                    if not it["ok"]:
                        note = it["note"] or "照片看不清主体"
                elif "score" in v:
                    try:
                        it["score"] = max(0, min(2, int(v["score"])))
                    except (TypeError, ValueError):
                        it["score"] = None
                else:
                    it["ok"] = bool(v.get("ok"))
        raw_issues = obj.get("issues")
        if isinstance(raw_issues, list):
            for it in raw_issues[:2]:
                if isinstance(it, dict) and it.get("what"):
                    issues.append({"item": it.get("item"), "what": str(it["what"])[:120],
                                   "suggest": str(it.get("suggest") or "")[:160]})
        verdict = obj.get("verdict") if obj.get("verdict") in ("pass", "fix", "unclear") else "unclear"
    # 归一：结论必须和证据对得上，对不上就降级成「看不清」，绝不让它判成败
    if by_n[5]["ok"] is False:
        verdict, issues = "unclear", []
    elif verdict == "fix" and not issues:
        verdict = "unclear"          # 说要改又不说改哪，等于没说
    elif verdict == "pass" and any(
            i["ok"] is False or (i["score"] is not None and i["score"] < 2)
            for i in items[:4]):
        verdict = "unclear"          # 自相矛盾：说全过却有 false/低分项
    if not obj:
        note = "评分表输出无法解析，只当没看见"
    return {"verdict": verdict, "items": items, "issues": issues, "note": note}


def review(image_path: str, *, version: str | None = None) -> dict:
    """按五项评分表自检外观。返回 {verdict, items, issues, note, model, ms, path, bytes}。"""
    from pathlib import Path
    p = Path(image_path)
    if not p.exists():
        raise VisionError(f"图片不存在：{image_path}")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        raise VisionError(f"图片太大（{p.stat().st_size // 1024} KB），先缩到 854 宽再问")
    out = describe_bytes(p.read_bytes(), REVIEW_SYSTEM, version=version)
    res = _parse_review(out["text"])
    res.update({"model": out["model"], "ms": out["ms"], "bytes": out["bytes"],
                "path": str(p)})
    return res


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
    rv = sub.add_parser("review", help="按五项评分表验收（现截）")
    rv.add_argument("--max-width", type=int, default=0)
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

    if args.action == "review":
        from .body import bridge as cc
        width = args.max_width or cfg()["max_width"]
        shot = cc.screenshot(max_width=width)
        try:
            out = review(shot["abs_path"])
        except VisionError as e:
            print(f"验收失败：{e}（图在 {shot['path']}）")
            return 1
        print(_fmt_review(out) + f"（{shot['width']}×{shot['height']} · {out['ms']}ms）")
        return 0
    return 1


def _fmt_review(r: dict) -> str:
    """把验收结果压成一行给人/模型看的文字。"""
    if r["verdict"] == "unclear":
        return f"验收 unclear：{r['note'] or '没法判断'}（不产意见）"
    if r["verdict"] == "pass":
        return "验收 pass：五项都过，没有要改的"
    bits = [f"{i['item']} {i['name']}——{i['note'] or '有问题'}" for i in r["items"][:4]
            if i["ok"] is False or (i["score"] is not None and i["score"] < 2)]
    if not bits:
        bits = [i["what"] for i in r["issues"]] or ["有问题"]
    head = "验收 fix：" + "；".join(bits)
    tips = [i["suggest"] for i in r["issues"] if i.get("suggest")]
    return head + ("｜建议 " + "；".join(tips) if tips else "")


if __name__ == "__main__":
    sys.exit(main())
