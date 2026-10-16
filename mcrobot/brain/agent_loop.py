"""编排：显式状态机（模式对齐 LangGraph，但零依赖、约 200 行）。

为什么不用 LangGraph：本项目一直 stdlib-only，装依赖会拖进一整套生态；
而 LangGraph 的核心价值是「显式 state + node + edge + 可断点续跑 + 可观测」，
这些用一个小图跑循环就能拿到，不需要框架。

    perceive ──▶ decide ──▶ act ──▶ verify ──┬─ ok ─────────▶ END
                    ▲                        │
                    └──── critique ──────────┘  (未达 max_rounds)

对齐 Voyager：执行后必须有**环境反馈 + 自检**，最多 N 轮（默认 4）；
对齐 mcagent/airi：**行动预算**（默认 20 次工具调用）与**失败熔断**（不空转）。

decide / verify 是可注入的（纯函数 → 好测）：
    decide(state) -> list[{"name":..., "arguments":{...}}]
    verify(state) -> (ok: bool, critique: str)
真接 LLM 时把 brain_llm 包成这两个函数传进来即可。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..tools.builtin import build_registry
from ..tools.registry import CallContext, Registry, ToolResult

END = None


@dataclass
class AgentState:
    """一次任务的完整状态。可序列化 → 能落盘 → 断点续跑。"""

    goal: str
    actor: str = ""
    dangerous_allowed: tuple[str, ...] = ()
    observation: dict = field(default_factory=dict)
    plan: list[dict] = field(default_factory=list)
    receipts: list[ToolResult] = field(default_factory=list)
    round: int = 0
    max_rounds: int = 4
    tool_calls: int = 0
    verdict: str | None = None      # "ok" | "fail" | None
    critique: str = ""
    aborted: str = ""               # 熔断原因
    trace: list[dict] = field(default_factory=list)

    def log(self, node: str, **kv: Any):
        self.trace.append({"ts": round(time.time(), 3), "node": node, **kv})

    def to_dict(self) -> dict:
        return {"goal": self.goal, "actor": self.actor, "round": self.round,
                "tool_calls": self.tool_calls, "verdict": self.verdict,
                "critique": self.critique, "aborted": self.aborted,
                "receipts": [r.to_dict() for r in self.receipts],
                "trace": self.trace}


def next_node(s: AgentState) -> str | None:
    """边：当前该去哪个节点。返回 END(None) 表示结束。"""
    if s.aborted:
        return END
    if not s.trace:
        return "perceive"
    last = s.trace[-1]["node"]
    if last == "perceive":
        return "decide"
    if last == "decide":
        # 没计划出动作就直接进自检（比如纯聊天，decide 只回了 say）
        return "act" if s.plan else "verify"
    if last == "act":
        return "verify"
    if last == "verify":
        if s.verdict == "ok":
            return END
        return "decide" if s.round < s.max_rounds else END
    return END


class AgentLoop:
    def __init__(self,
                 registry: Registry | None = None,
                 decide: Callable[[AgentState], list[dict]] | None = None,
                 verify: Callable[[AgentState], tuple[bool, str]] | None = None,
                 max_rounds: int = 4,
                 action_budget: int = 20,
                 tools_for_observation: tuple[str, ...] = ("get_state",)):
        self.registry = registry or build_registry()
        # 注意：不能叫 self.decide / self.verify —— 会盖掉同名节点方法
        self.decide_fn = decide or _fallback_decide
        self.verify_fn = verify or _fallback_verify
        self.max_rounds = max_rounds
        self.action_budget = action_budget
        self.tools_for_observation = tools_for_observation

    # ---------------- 节点 ----------------

    def perceive(self, s: AgentState, ctx: CallContext):
        for name in self.tools_for_observation:
            r = self.registry.call(name, {}, ctx)
            if r.ok:
                s.observation[name] = r.data or {"text": r.text}
            else:
                s.observation[name] = {"error": r.error}
        s.log("perceive", tools=list(self.tools_for_observation))

    def decide(self, s: AgentState, ctx: CallContext):
        s.round += 1
        s.plan = list(self.decide_fn(s) or [])
        s.log("decide", round=s.round,
              plan=[c.get("name") for c in s.plan])

    def act(self, s: AgentState, ctx: CallContext):
        for call in s.plan:
            if s.tool_calls >= self.action_budget:
                s.aborted = f"行动预算用尽（{self.action_budget} 次），停手"
                s.log("act", aborted=s.aborted)
                return
            s.tool_calls += 1
            r = self.registry.call(call.get("name") or "", call.get("arguments"), ctx)
            s.receipts.append(r)
            s.log("act", tool=r.tool, ok=r.ok, action_id=r.action_id,
                  error=r.error[:120] if r.error else "")
            if not r.ok:
                break  # 失败止损：一条失败就不继续刷后面的
        s.plan = []

    def verify(self, s: AgentState, ctx: CallContext):
        ok, critique = self.verify_fn(s)
        s.verdict = "ok" if ok else "fail"
        s.critique = critique or ""
        s.log("verify", verdict=s.verdict, critique=s.critique[:200])

    # ---------------- 跑 ----------------

    def run(self, goal: str, *, actor: str = "", dry: bool = False,
            dangerous_allowed: tuple[str, ...] = ()) -> AgentState:
        s = AgentState(goal=goal, actor=actor, max_rounds=self.max_rounds,
                       dangerous_allowed=dangerous_allowed)
        ctx = CallContext(actor=actor, dry=dry, dangerous_allowed=dangerous_allowed)
        node = next_node(s)
        while node is not END:
            getattr(self, node)(s, ctx)
            nxt = next_node(s)
            if nxt == node:
                s.aborted = f"节点 {node} 自环，强制结束"
                break
            node = nxt
        if s.verdict is None and not s.aborted:
            s.verdict = "fail"
        return s


# ---------------- 默认策略（无 LLM 时的兜底，保证链路永远能跑） ----------------


def _fallback_decide(s: AgentState) -> list[dict]:
    """无 LLM 时：把目标原样说出去。保证「永不落空」。"""
    from ..body import reflex
    intent = reflex.analyze(s.goal)
    if intent.command:
        return [{"name": "run_command", "arguments": {"command": intent.command}}]
    return [{"name": "say", "arguments": {"text": s.goal[:200]}}]


def _fallback_verify(s: AgentState) -> tuple[bool, str]:
    """无 LLM 时：全成功即算成功；有失败就把错误当 critique。"""
    if not s.receipts:
        return True, ""
    bad = [r for r in s.receipts if not r.ok]
    if bad:
        return False, "；".join(f"{r.tool}: {r.error}" for r in bad[:3])
    return True, ""
