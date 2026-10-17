"""把 LLM 接进 AgentLoop 的 decide / verify（Voyager 式闭环的大脑部分）。

decide：看完「目标 + 环境观察 + 上一轮回执 + 批评」后，输出要调用的工具（JSON 数组）
verify：看完「目标 + 所有回执」后，独立判断成败并给出批评（Voyager 的 critic）

两者都只依赖一个 `ask(messages) -> str`，所以任何后端（DeepSink 伪 API / 真 API /
人肉）都能套进来，也方便用假函数做单测。
"""
from __future__ import annotations

import json
import re
import time
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
- 要走到某处就调 goto(x,y,z)，别用 /tp 或 /execute tp：goto 是客户端真走路，服务器看到的是正常移动。
  它是异步的，出发后不要立刻做依赖位置的事；先用 get_state 确认到了再动手。
- 拿不准方块 ID 就先 block_lookup（目录里的显示名是英文）。
- 改过世界（砌/挖/替换）之后必须验：单点用 block_at，整面墙/整间房用 scan_region
  （一次拿逐格坐标，比 block_probe 快且不需要 OP）；命令返回 ok 只代表发出去了，不代表改对了。
- 外观问题（歪不歪、像不像房子、颜色搭不搭、门洞留没留）用 look 看一眼；但它会说错方块名，
  精确核对仍用 block_at/scan_region，且一次任务看 1~2 眼就够（每次 10~20 秒）。
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
- 改过世界的任务，回执里必须有 block_at / scan_region 之类的**世界读取**，且看到的结果符合目标；
  只有「命令返回 ok」不算做成了。
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


def _extract_json_objects(text: str) -> list[dict]:
    """抠出所有独立的 JSON 对象（容忍「一行一个对象」/ 前后废话 / 尾部截断）。

    真模型的输出不稳定：要求数组，它可能给 JSONL、也可能给半个对象。
    用 raw_decode 逐个吃，吃到一个算一个。
    """
    t = text or ""
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1)
    dec, out, i = json.JSONDecoder(), [], 0
    while i < len(t):
        j = t.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(t, j)
        except Exception:
            i = j + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = end
    return out


_XML_CALL = re.compile(
    r"<tool_call>\s*<function=([^>\s]+)\s*>(.*?)</function>\s*</tool_call>", re.DOTALL)
_XML_CALL_BARE = re.compile(r"<function=([^>\s]+)\s*>(.*?)</function>", re.DOTALL)
_XML_PARAM = re.compile(r"<parameter=([^>\s]+)\s*>(.*?)</parameter>", re.DOTALL)


def _coerce(v: str):
    """参数值尽量还原类型：3 → 3、["a"] → list、true → True，还原不了就当字符串。"""
    s = (v or "").strip()
    try:
        return json.loads(s)
    except Exception:
        return s


def _extract_xml_calls(text: str) -> list[dict]:
    """解析 XML 风格的工具调用。

    MiMo 一类按 tool-call 微调过的模型会直接吐
    <tool_call><function=name><parameter=k>v</parameter></function></tool_call>，
    完全不理会「只输出 JSON」的指令——只认 JSON 的话整段决策会被丢掉，
    表现为闭环 0 次调用却判成功（真机踩过）。
    """
    body = text or ""
    chunks = _XML_CALL.findall(body) or _XML_CALL_BARE.findall(body)
    out = []
    for name, inner in chunks:
        args = {k: _coerce(v) for k, v in _XML_PARAM.findall(inner)}
        out.append({"name": name.strip(), "arguments": args})
    return out


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


def _parsed(raw: str) -> bool:
    return (_extract_json_array(raw) is not None
            or bool(_extract_xml_calls(raw))
            or bool(_extract_json_objects(raw)))


def _ask_with_retry(ask: Callable[[list[dict]], str], msgs: list[dict],
                    tries: int = 2, pause: float = 2.0) -> str:
    """决策请求重试：上游偶尔只回「服务暂时繁忙」，当成空计划会白烧一整轮。

    只在「解析不出任何动作」时重试，正常回复不额外等待。
    """
    raw = ""
    for i in range(tries + 1):
        try:
            raw = ask(msgs) or ""
        except Exception:
            raw = ""
        if raw and _parsed(raw):
            return raw
        if i < tries:
            time.sleep(pause)
    return raw


def make_decide(ask: Callable[[list[dict]], str],
                registry: Registry,
                system: str = DECIDE_SYSTEM) -> Callable[[object], list[dict]]:
    def decide(s) -> list[dict]:
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": render_state(s, registry.mcp_specs())}]
        raw = _ask_with_retry(ask, msgs)
        arr = _extract_json_array(raw)
        if arr is None:
            xml = _extract_xml_calls(raw)
            if xml:
                return xml
            objs = [o for o in _extract_json_objects(raw) if o.get("name")]
            if objs:
                return [{"name": str(o["name"]), "arguments": o.get("arguments") or {}}
                        for o in objs]
            s.log("decide_parse_fail", raw=raw[:300])
            return []
        out = []
        for c in arr:
            if isinstance(c, dict) and c.get("name"):
                out.append({"name": str(c["name"]),
                            "arguments": c.get("arguments") or {}})
        return out
    return decide


_WORLD_WORDS = ("砌", "建", "造", "搭", "放", "挖", "替换", "墙", "房", "平台",
                "填", "铺", "盖", "set", "build", "place", "fill", "replace")


def _needs_world_evidence(goal: str) -> bool:
    g = (goal or "").lower()
    return any(w in g for w in _WORLD_WORDS)


def make_verify(ask: Callable[[list[dict]], str],
                system: str = VERIFY_SYSTEM) -> Callable[[object], tuple[bool, str]]:
    def verify(s) -> tuple[bool, str]:
        if not s.receipts:
            # 一个动作都没执行，目标不可能达成。以前这里返回 True，于是
            # 「决策解析失败 → 空动作 → 自检通过」变成闭环静默成功（真机踩过）。
            return False, "这一轮没有执行任何工具调用，目标不可能达成，先动手"
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
            # 模型常写坏 JSON（漏逗号、截断）。先把 ok/critique 抠出来，
            # 实在抠不出就**按未完成处理**——以前按「回执全 ok」硬判，
            # 于是「没砌墙 + 自检写坏」也会被判成功（真机踩过）。
            m = re.search(r'"ok"\s*:\s*(true|false)', raw or "", re.I)
            if m:
                c = re.search(r'"critique"\s*:\s*"((?:[^"\\]|\\.)*)"', raw or "", re.DOTALL)
                return m.group(1).lower() == "true", (c.group(1) if c else "")
            s.log("verify_parse_fail", raw=raw[:300])
            return False, "自检输出无法解析，按未完成处理；做到了就直接用 say 说结果"
        ok = bool(obj.get("ok"))
        critique = str(obj.get("critique") or "")
        if ok and _needs_world_evidence(s.goal):
            # 硬闸：改世界的任务必须留下世界读取回执（新接口优先，block_probe 兜底）。
            # 模型会自相矛盾（ok:true 配一段「没验证」的批评），也会凭
            # 「命令返回 ok」就宣布完成——真机上把 0/15 判成成功过。
            probe = [r for r in s.receipts
                     if r.tool in ("block_at", "scan_region", "block_probe") and r.ok]
            if not probe:
                return False, ("没有任何世界读取回执（block_at / scan_region），"
                               "不能算改对了；先读坐标确认再回答")
        return ok, critique
    return verify
