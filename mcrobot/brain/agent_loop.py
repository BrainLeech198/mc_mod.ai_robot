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
    task_id: str = ""
    actor: str = ""
    dangerous_allowed: tuple[str, ...] = ()
    observation: dict = field(default_factory=dict)
    plan: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)   # 真正发出去的调用（含参数，供学技能用）
    receipts: list[ToolResult] = field(default_factory=list)
    round: int = 0
    max_rounds: int = 4
    tool_calls: int = 0
    verdict: str | None = None      # "ok" | "fail" | None
    critique: str = ""
    aborted: str = ""               # 熔断原因
    # 任务分解（planner）：steps 是有序子目标，subtasks 是每个子目标的执行结果摘要，
    # context 是前序子目标的结果——只作为文字上下文传给 decide，不共享状态对象。
    steps: list[str] = field(default_factory=list)
    subtasks: list[dict] = field(default_factory=list)
    context: str = ""
    trace: list[dict] = field(default_factory=list)

    def log(self, node: str, **kv: Any):
        self.trace.append({"ts": round(time.time(), 3), "node": node, **kv})

    def to_dict(self) -> dict:
        return {"goal": self.goal, "task_id": self.task_id, "actor": self.actor,
                "round": self.round,
                "max_rounds": self.max_rounds, "tool_calls": self.tool_calls,
                "verdict": self.verdict, "observation": self.observation,
                "plan": self.plan, "actions": self.actions,
                "critique": self.critique, "aborted": self.aborted,
                "steps": self.steps, "subtasks": self.subtasks,
                "context": self.context,
                "receipts": [r.to_dict() for r in self.receipts],
                "trace": self.trace}

    @classmethod
    def from_dict(cls, d: dict) -> "AgentState":
        """从落盘状态恢复（断点续跑）。回执只留摘要字段。"""
        s = cls(goal=d.get("goal") or "", task_id=d.get("task_id") or "",
                actor=d.get("actor") or "",
                round=int(d.get("round") or 0),
                max_rounds=int(d.get("max_rounds") or 4),
                tool_calls=int(d.get("tool_calls") or 0),
                verdict=d.get("verdict"), critique=d.get("critique") or "",
                aborted=d.get("aborted") or "",
                observation=d.get("observation") or {},
                plan=d.get("plan") or [], actions=d.get("actions") or [],
                steps=d.get("steps") or [], subtasks=d.get("subtasks") or [],
                context=d.get("context") or "",
                trace=d.get("trace") or [])
        for r in d.get("receipts") or []:
            s.receipts.append(ToolResult(bool(r.get("ok")), r.get("tool") or "",
                                         r.get("action_id") or "",
                                         text=r.get("text") or "",
                                         error=r.get("error") or "",
                                         data=r.get("data") or {}))
        return s


def next_node(s: AgentState) -> str | None:
    """边：当前该去哪个节点。返回 END(None) 表示结束。"""
    if s.aborted:
        return END
    if not s.trace:
        return "perceive"
    # "resume" 只是恢复标记，不算节点——按它前面那个真实节点决定去哪
    last = next((t["node"] for t in reversed(s.trace)
                 if t.get("node") != "resume"), None)
    if last is None:
        return "perceive"
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
                 tools_for_observation: tuple[str, ...] = ("get_state",),
                 skills_enabled: bool = True,
                 lessons_enabled: bool = True,
                 plan_fn: Callable[[str], list[str]] | None = None,
                 stop_on_fail: bool = True):
        self.registry = registry or build_registry()
        # 注意：不能叫 self.decide / self.verify —— 会盖掉同名节点方法
        self.decide_fn = decide or _fallback_decide
        self.verify_fn = verify or _fallback_verify
        self.max_rounds = max_rounds
        self.action_budget = action_budget
        self.tools_for_observation = tools_for_observation
        # Voyager 技能库：规划前检索、做成后入库。关掉就是纯闭环（便于单测隔离）
        self.skills_enabled = skills_enabled
        # mcagent 的反思：失败沉淀成知识库里的「教训」，下次同类目标检索得到。
        # 关掉给单测用——不然每次跑失败用例都往版本知识表里写垃圾。
        self.lessons_enabled = lessons_enabled
        # mcagent 的任务分解：注入 plan_fn 才能拆；没注入就还是单层闭环（老行为）。
        self.plan_fn = plan_fn
        self.stop_on_fail = stop_on_fail

    # ---------------- 节点 ----------------

    def perceive(self, s: AgentState, ctx: CallContext):
        for name in self.tools_for_observation:
            r = self.registry.call(name, {}, ctx)
            if r.ok:
                s.observation[name] = r.data or {"text": r.text}
            else:
                s.observation[name] = {"error": r.error}
        # Voyager：规划前先查技能库，把相近的绝活塞进观察里（检索失败不影响流程）
        skill_names: list[str] = []
        if self.skills_enabled:
            try:
                from ..skills import library as skl
                hits = skl.search(s.goal, top_k=3)
                if hits:
                    s.observation["skills"] = [h.brief() for h in hits]
                    skill_names = [h.name for h in hits]
            except Exception:
                pass
        # 知识库同理：只塞相关事实，查不到就不塞（红线 3）
        knowledge = []
        try:
            from .. import knowledge as kno
            knowledge = kno.search(s.goal, top_k=3)
            if knowledge:
                s.observation["knowledge"] = [
                    {"topic": f["topic"], "text": f["text"],
                     "source": f.get("source") or ""} for f in knowledge]
        except Exception:
            pass
        s.log("perceive", tools=list(self.tools_for_observation), skills=skill_names,
              knowledge=len(knowledge))

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
            s.actions.append({"name": call.get("name") or "",
                              "arguments": call.get("arguments") or {}})
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
            dangerous_allowed: tuple[str, ...] = (), task_id: str = "",
            checkpoint=None, context: str = "") -> AgentState:
        """跑一次任务。给了 checkpoint 就每拍落盘；同 task_id 有存档则从存档续跑。

        context 是任务分解时前序子目标的结果摘要，只作为文字上下文传给 decide
        （`render_state` 会渲染），不参与状态恢复之外的任何逻辑。
        """
        s = None
        if checkpoint and task_id:
            s = checkpoint.load(task_id)
            if s:
                s.log("resume", round=s.round)
        if s is None:
            s = AgentState(goal=goal, task_id=task_id, actor=actor,
                           max_rounds=self.max_rounds,
                           dangerous_allowed=dangerous_allowed,
                           context=context or "")
        ctx = CallContext(actor=actor, dry=dry, dangerous_allowed=dangerous_allowed)
        node = next_node(s)
        while node is not END:
            getattr(self, node)(s, ctx)
            if checkpoint and s.task_id:
                try:
                    checkpoint.save(s)
                except Exception:
                    pass  # 落盘失败不影响干活
            nxt = next_node(s)
            if nxt == node:
                s.aborted = f"节点 {node} 自环，强制结束"
                break
            node = nxt
        if s.verdict is None and not s.aborted:
            s.verdict = "fail"
        if self.skills_enabled and s.verdict == "ok" and s.actions:
            # Voyager：做成过的事才配进库，下次同类目标直接调模板
            try:
                from ..skills import library as skl
                skl.learn_from_actions(s.goal, s.actions)
            except Exception:
                pass
        if self.lessons_enabled and s.verdict != "ok":
            # mcagent 的反思：做成的路进技能库，做错的路进知识库当「教训」——
            # 下次同类目标在 perceive 就能检索到，不用同一個坑踩两次。
            self._remember_lesson(s)
        return s

    def _remember_lesson(self, s: AgentState) -> None:
        """把一次失败沉淀成知识库里的一条「教训」。

        红线（和 remember 本身一致）：出处写死是这次闭环的 task_id，模型不能凭空造记忆；
        任何异常都吞掉——记忆是锦上添花，不能反过来影响任务结论。
        """
        why = " ".join((s.critique or s.aborted or "").split())[:200]
        if not why:
            return
        goal = " ".join(s.goal.split())[:60]
        try:
            from .. import knowledge as kno
            kno.remember("教训", f"{goal} 没做成：{why}",
                         tags=["闭环", "失败"], source=f"闭环 {s.task_id or '无 task_id'}")
        except Exception:
            pass

    # ---------------- 任务分解（mcagent 的 task decomposition） ----------------

    def run_plan(self, goal: str, *, actor: str = "", dry: bool = False,
                 dangerous_allowed: tuple[str, ...] = (), task_id: str = "",
                 checkpoint=None) -> AgentState:
        """先拆成有序子目标，再逐个跑闭环；前一个的结果作为后一个的上下文。

        没有注入 plan_fn 时退化成单步（= 老行为），所以 planner 永远是增值层。
        子目标默认失败即停（建造任务大多有依赖），剩下的标记成 skipped 不空跑。
        返回的 AgentState 是**汇总态**：goal 是原始目标，steps/subtasks 记录拆了啥、
        每个子目标成败如何，verdict/tool_calls/receipts 是合并后的结果。
        """
        steps = [goal]
        if self.plan_fn:
            try:
                steps = list(self.plan_fn(goal) or [goal]) or [goal]
            except Exception:
                steps = [goal]          # 拆失败不许把任务搞失败
        total = AgentState(goal=goal, task_id=task_id, actor=actor,
                           max_rounds=self.max_rounds,
                           dangerous_allowed=dangerous_allowed, steps=steps)
        total.log("plan", steps=steps, n=len(steps))
        done: list[str] = []            # 前序子目标的一句话结果，传给下一个
        for i, sub in enumerate(steps, 1):
            if total.aborted:
                total.subtasks.append({"goal": sub, "verdict": "skipped",
                                       "critique": f"前序失败，跳过（{total.aborted}）",
                                       "round": 0, "tool_calls": 0, "task_id": ""})
                continue
            s = self.run(sub, actor=actor, dry=dry,
                         dangerous_allowed=dangerous_allowed,
                         task_id=f"{task_id}-{i}" if task_id else "",
                         checkpoint=checkpoint, context="；".join(done))
            total.subtasks.append({"goal": sub, "verdict": s.verdict or "fail",
                                   "critique": (s.critique or s.aborted or "")[:200],
                                   "round": s.round, "tool_calls": s.tool_calls,
                                   "task_id": s.task_id})
            total.receipts.extend(s.receipts)
            total.actions.extend(s.actions)
            total.tool_calls += s.tool_calls
            total.round = max(total.round, s.round)
            line = f"{sub}→{s.verdict or 'fail'}"
            if s.verdict != "ok":
                total.critique = f"第 {i}/{len(steps)} 步「{sub}」没做成：" \
                                 f"{(s.critique or s.aborted or '未知原因')[:160]}"
                if self.stop_on_fail:
                    total.aborted = f"子目标 {i} 失败，后续 {len(steps) - i} 步跳过"
                total.log("plan_step", i=i, goal=sub, verdict=s.verdict)
                done.append(line)
                continue
            done.append(line)
            total.log("plan_step", i=i, goal=sub, verdict="ok")
        total.verdict = "ok" if all(
            t["verdict"] == "ok" for t in total.subtasks) and total.subtasks else "fail"
        if total.verdict == "fail" and not total.critique:
            total.critique = total.aborted or "有子目标没做成"
        return total


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
