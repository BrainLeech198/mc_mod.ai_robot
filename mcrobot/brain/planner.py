"""任务分解（planner）：把高层目标拆成有序子目标 —— 对齐 mcagent 的 task decomposition。

为什么需要它：以前「建学校」这种目标是**直接塞进单层闭环**的，模型要在 4 轮、20 次调用里
同时搞定选址、砌墙、装窗、内饰——结果通常是第 3 轮就把预算烧光，或者每轮各干一点、
没有一件是完整的。mcagent 的做法是先规划后执行：每个子目标单独跑闭环、单独验收、
单独学技能/记教训，前一个的结果作为后一个的上下文。

设计取舍：
- 拆不出来的目标（模型胡话 / 目标本身就很简单）**退回单步**，行为与没有 planner 时一致——
  planner 是增值层，不是新的失败点。
- 子目标默认**失败即停**：建造任务大多有依赖（没砌墙就没法装窗），硬撑着往下跑只会烧预算。
- 子目标之间只传「文字摘要」当上下文，不共享状态对象——简单、可落盘、可单测。
"""
from __future__ import annotations

import json
import re
from typing import Callable

PLAN_SYSTEM = """你是 Minecraft 建造任务的规划器。把玩家给的高层目标拆成 2~6 个**有序子目标**。
规则：
- 只能输出一个 JSON 数组，元素是子目标短句（中文，20 字以内）。
- 顺序即执行顺序；有依赖的（先砌墙再装门窗）必须把被依赖的放前面。
- 每个子目标必须是**一次闭环能做完、能独立验收**的一件事（砌一面墙 / 装三扇门 / 清一块地），
  不许写「把学校建好」这种没拆掉的大话。
- 目标本身就已经很简单（说句话、看一眼、问个事）就返回只含它自己的数组，别硬拆。
- 不许输出 JSON 之外的任何文字。"""

# 这些词一出现，说明是真要动世界——再短也不能当聊天跳过分解
_BUILD_WORDS = ("建", "盖", "砌", "造", "修", "拆", "铺", "填", "换", "清空", "清理",
                "school", "build", "house", "wall", "room", "bridge", "tower")
# 一看就是闲聊/问话，拆它纯属浪费一次 LLM 往返（约 10 秒）
_CHAT_HINTS = ("在吗", "你好", "谢谢", "再见", "是谁", "帮助", "help", "早上好", "晚上好",
               "？", "?", "名字", "天气")


def looks_simple(goal: str) -> bool:
    """这个目标要不要跳过分解？短且不动世界的，直接单步跑。"""
    g = (goal or "").strip()
    if not g:
        return True
    low = g.lower()
    if any(w in low for w in _BUILD_WORDS):
        return False
    return len(g) <= 12 or any(h in g for h in _CHAT_HINTS)


def _clean_steps(raw: str, goal: str, max_subtasks: int) -> list[str]:
    """从模型输出里抠子目标。抠不出来就退回 [goal]——planner 不许成为新的失败点。"""
    t = (raw or "").strip()
    arr = None
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, list):
            arr = obj
    except Exception:
        arr = None
    if arr is None:
        # 容忍「一行一个子目标」的散文式输出：引号包着的都算
        arr = re.findall(r"[\"「『]([^\"」』]{2,40})[\"」』]", t)
    out: list[str] = []
    for x in arr or []:
        s = " ".join(str(x).split())
        if s and s not in out:
            out.append(s)
    out = out[:max_subtasks]
    return out or [goal]


def decompose(goal: str, ask: Callable[[list[dict]], str], *,
              max_subtasks: int = 6) -> list[str]:
    """把目标拆成有序子目标；目标很简单或拆失败时返回 [goal]。

    真机踩过：MiMo 这类推理模型有时只回 reasoning、content 为空（同 vision 那个坑），
    第一次拆解会静默退化成单步。所以**解析不出东西时补问一次**，别让高层目标白白
    失去分解机会——再失败就认命退单步（planner 不许成为新的失败点）。
    """
    if looks_simple(goal):
        return [goal]
    msgs = [{"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": f"# 目标\n{goal}"}]
    last = ""
    for attempt in (0, 1):
        try:
            last = ask(msgs) or ""
        except Exception:
            return [goal]
        steps = _clean_steps(last, goal, max_subtasks)
        if steps != [goal]:
            return steps
        if attempt == 0:
            # 补一刀：明确告诉它刚才那段没法用，只要 JSON 数组
            msgs = msgs + [
                {"role": "user", "content": "你上一条没法解析。只回一个 JSON 数组，"
                                            "元素是 2~6 个有序子目标短句，不要任何解释。"}]
    return [goal]


def make_planner(ask: Callable[[list[dict]], str], *, max_subtasks: int = 6
                 ) -> Callable[[str], list[str]]:
    """给 AgentLoop 注入用的规划器（失败一律退单步，和 decide/verify 一样可打桩）。"""
    def plan(goal: str) -> list[str]:
        return decompose(goal, ask, max_subtasks=max_subtasks)
    return plan
