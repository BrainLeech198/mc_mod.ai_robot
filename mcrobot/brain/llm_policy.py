"""把 LLM 接进 AgentLoop 的 decide / verify（Voyager 式闭环的大脑部分）。

decide：看完「目标 + 环境观察 + 上一轮回执 + 批评」后，输出要调用的工具（JSON 数组）
verify：看完「目标 + 所有回执」后，独立判断成败并给出批评（Voyager 的 critic）

两者都只依赖一个 `ask(messages) -> str`，所以任何后端（DeepSink 伪 API / 真 API /
人肉）都能套进来，也方便用假函数做单测。
"""
from __future__ import annotations

import json
import re
from typing import Callable

from ..tools.registry import Registry

DECIDE_SYSTEM = """你是 Minecraft 里一个真玩家 AI 的行动决策器。

输入：玩家给你的目标、当前环境观察、上一轮的工具回执、上一轮的批评。
输出：**只能是**一个 JSON 数组，每项形如 {"name": "<工具名>", "arguments": {...}}。

规则：
- 只能从给定工具里选，不许编造工具名。
- 要跟玩家说话就用 say；只需要说话时数组里就只放 say。
- 一次回合尽量把能合并的指令放进 run_batch（省往返）。
- 上一轮批评说哪里没做到，这一轮就先补哪里。
- 拿不准方块 ID 就先 block_lookup（目录里的显示名是英文）。
- 观察里的 skills 是**已经做成过**的技能模板：有能对上的就用 skill_run 跑它（参数填全），
  别从头现搓；没有就按工具一步步做，做成了可以 skill_save 存下来。
- 不要输出 JSON 之外的任何文字。
"""

VERIFY_SYSTEM = """你是 Minecraft 里一个真玩家 AI 的自检裁判。

输入：玩家给你的目标、这一轮执行过的工具与它们的回执。
判断：目标达成了吗？

输出**只能是**一行 JSON：
  {"ok": true}
或
  {"ok": false, "critique": "具体差在哪、下一步该做什么（一句话）"}

规则：
- 只根据回执判断，不许臆测没发生的事。
- 有失败回执（未授权、报错）一律 ok:false。
- critique 要具体到能指导下一步，别写空话。
"""


def _extract_json_array(text: str) -> list | None:
    """从模型输出里抠出 JSON 数组（容忍 ```json 围栏与前后废话）。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    start = t.find("[")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "[":
            depth += 1
        elif t[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None


def _extract_json_object(text: str) -> dict | None:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except Exception:
        return None


def render_state(s, specs: list[dict]) -> str:
    """把 AgentState 渲染成给模型看的上下文。"""
    parts = [f"# 目标\n{s.goal}",
             f"\n# 下达者\n{s.actor or '未知'}（危险操作白名单："
             f"{', '.join(s.dangerous_allowed) if s.dangerous_allowed else '无'}）"]
    if s.observation:
        parts.append("\n# 环境观察\n" + json.dumps(s.observation, ensure_ascii=False)[:1200])
    if s.receipts:
        rs = [{"tool": r.tool, "ok": r.ok, "output": (r.text or "")[:300],
               "error": (r.error or "")[:200]} for r in s.receipts[-8:]]
        parts.append("\n# 工具回执\n" + json.dumps(rs, ensure_ascii=False))
    if s.critique:
        parts.append(f"\n# 上一轮批评\n{s.critique}")
    if s.round > 1:
        parts.append(f"\n# 轮次\n第 {s.round} 轮（上限 {s.max_rounds}）")
    parts.append("\n# 可用工具\n" + json.dumps(specs, ensure_ascii=False))
    return "\n".join(parts)


def make_decide(ask: Callable[[list[dict]], str],
                registry: Registry,
                system: str = DECIDE_SYSTEM) -> Callable[[object], list[dict]]:
    def decide(s) -> list[dict]:
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": render_state(s, registry.mcp_specs())}]
        raw = ask(msgs)
        arr = _extract_json_array(raw)
        if arr is None:
            s.log("decide_parse_fail", raw=raw[:300])
            return []
        out = []
        for c in arr:
            if isinstance(c, dict) and c.get("name"):
                out.append({"name": str(c["name"]),
                            "arguments": c.get("arguments") or {}})
        return out
    return decide


def make_verify(ask: Callable[[list[dict]], str],
                system: str = VERIFY_SYSTEM) -> Callable[[object], tuple[bool, str]]:
    def verify(s) -> tuple[bool, str]:
        if not s.receipts:
            return True, ""
        parts = [f"# 目标\n{s.goal}",
                 "\n# 执行回执\n" + json.dumps(
                     [{"tool": r.tool, "ok": r.ok,
                       "output": (r.text or "")[:300],
                       "error": (r.error or "")[:200]} for r in s.receipts],
                     ensure_ascii=False)]
        if s.critique:
            parts.append(f"\n# 上一轮批评\n{s.critique}")
        raw = ask([{"role": "system", "content": system},
                   {"role": "user", "content": "\n".join(parts)}])
        obj = _extract_json_object(raw)
        if obj is None:
            s.log("verify_parse_fail", raw=raw[:300])
            # 解析不出来就按回执硬判：全 ok 才算成
            return all(r.ok for r in s.receipts), "自检输出没解析出来，按回执硬判"
        return bool(obj.get("ok")), str(obj.get("critique") or "")
    return verify
