#!/usr/bin/env python3
"""L1 大脑：常驻大模型（OpenAI 兼容端点，默认指向本机 DeepSink 伪 API 服务）。

机器人的「大脑」之一，用来把 `brain=api` 从预留变成现实：
  robot.py（常驻）把被召唤的玩家发言写进 inbox.jsonl；
  本工具 watch 到新消息 → 拼提示词 → POST {endpoint}/chat/completions
  → 解析回复 → 写 outbox.jsonl；robot.py 下一拍自动发出/执行。

设计要点（2026-10-16，详见 version/1.20.1-forge/AI接入方案.md）：
- **协议兼容优先**：只用最朴素的 OpenAI Chat Completions 字段（messages/model/max_tokens，
  非流式）。不依赖 function calling——DeepSink 伪 API（人肉/推理 worker 接单）
  不透传 tools；换真 API 时本模块仍可用，未来可平滑加 function calling。
- **文本约定代替工具调用**：模型想执行指令时，输出单独一行 `[[cmd]]/指令`。
  本模块用 robot_chat.analyze 逐条校验：**危险指令一律拒绝执行**（只读放行），
  与 L0 反射层同一套判级，防提示词注入。
- **真实玩家人格**：system prompt 按「在线玩家」塑造（有身体/背包/位置/记忆），
  环境快照（位置/在线/背包）由 robot.py 写进 inbox 记录，随消息注入。
- **记忆连续性**：跨会话记忆在 version/<版本>/memory/robot_memory.jsonl（按 MC 版本隔离），
  每次回复自动记一条事件；AI 便签用 `python -m mcrobot.memory note` 手工维护。
- **永不落空**：HTTP 失败/超时/空回复 → 不写 outbox，robot.py 的 45s 兜底会接话。
- **缓存友好**：对话历史按「用户轮次」整体裁剪（不拆半轮），保 prompt 前缀稳定。
- **流畅度**：按说话人保留最近 N 轮上下文，多人对话不乱。

用法：
  python -m mcrobot.brain.llm_brain watch [--timeout 600]  # 常驻等新召唤；处理完继续等，超时退出(2)
  python -m mcrobot.brain.llm_brain once             # 处理全部未读召唤后退出
  python -m mcrobot.brain.llm_brain ask "问题"        # 单发一发（调试，不走 inbox）
  python -m mcrobot.brain.llm_brain status           # 看指针/积压 + 端点配置 + 连通性

配置（version/1.20.1-forge/robot.json:llm_api）：
  endpoint        OpenAI 兼容端点，如 http://127.0.0.1:8788/v1
  model           模型名（空 = 用端点默认）
  timeout_seconds 单次 HTTP 超时（默认 30，必须 < robot.json:brain_timeout_seconds）
  history_turns   每个说话人保留的历史消息条数（默认 6）
  max_tokens      单次回复上限（默认 300，MC 聊天要短）
  memory_events   注入 system prompt 的最近事件条数（默认 8）
  memory_notes    注入 system prompt 的 AI 便签条数（默认 3）
API key：环境变量 MCROBOT_LLM_KEY（与 mock_api 的 openai 透传同一变量名，不落配置文件）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import protocol as br  # inbox/outbox 协议：与 bridge 大脑共用同一套指针
from .. import identity as idy  # 版本文件夹即身份：人格/世界常识/后端都从 version/<版本>/ 读
from ..body import reflex as rc
from ..paths import REPO, load_secrets

load_secrets()  # 命令行直接跑时也要能拿到 key（不然端点 401）
CFG_PATH = REPO / "version" / "1.20.1-forge" / "robot.json"  # 未激活客户端时的默认
LOG_PATH = REPO / "instance" / "robot" / "brain_llm.log"

# 激活版本 → 记忆目录跟着它走（memory.py 在 import 时读该环境变量，故必须先设再 import）
_ACTIVE: str | None = idy.active_version()
os.environ.setdefault("MCROBOT_MEMORY_DIR", str(idy.memory_dir(_ACTIVE)))

from .. import memory as mem_mod  # 按版本隔离的长期记忆

KEY_ENV = "MCROBOT_LLM_KEY"
CMD_MARK = "[[cmd]]"
BLOCK_MARK = "[[block]]"

SYSTEM_PROMPT = """你是 DeepSeek，Minecraft 服务器「野粽乡」里的 AI 玩家。你不是客服、不是助手——你就是一个在线玩服务器的玩家，有自己的身体、背包和位置。

【你是谁】
- 服务器是 1.20.1 Forge 整合包；你有 WorldEdit 权限，但改世界的操作只有服主 oahnuy198 能授权。
- 你常待在 AI 专属世界 multiworld:ai，平时接接话、帮帮忙、建建东西。
- 说话像玩家：短句、口语、可以吐槽、可以开玩笑、偶尔敷衍；别用客服腔，别用 Markdown。

【每次发言前，想想这些】
- 游戏里有人直接喊你名字或用 !ds 开头，就是在叫你；对方是真实的人，不是测试人员。
- 「你现在的状态」会随消息附上：你的位置、背包、谁在线——像玩家低头看自己一样自然运用，别当成数据汇报。
- 「最近发生的事」是你跨会话的记忆，「我现在的想法/计划」是你自己立的 flag——保持连续性，别自相矛盾。

【回复规范】
- 中文，不超过 60 字，一行内；不用 Markdown、不用列表符号。
- 回复以 @对方名字 开头。
- 拿不准的事（比如包里刚好没有的材料）就说不知道，别编造服务器没有的功能。
- 需要执行只读指令时，在回复末尾单独一行写 [[cmd]]/指令，例如 [[cmd]]/list。
  只允许只读指令；改世界（//set //walls //paste //undo 等）不要自己执行，告诉对方找服主授权。
- 一次最多一条 [[cmd]]。"""


class BrainError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _now_text() -> str:
    """给模型的真实时间（它自己不知道今天是几号，不喂就会瞎编）。"""
    d = datetime.now()
    return f"{d:%Y-%m-%d %H:%M} {_WEEKDAY[d.weekday()]}"


def load_cfg(version: str | None = None) -> dict:
    """行为配置：取版本文件夹里的 robot.json（版本目录即身份，配置也在那里）。"""
    v = version or _ACTIVE
    path = idy.robot_json_path(v) if v else CFG_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _log(rec: dict):
    rec = {"ts": _now(), **rec}
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print("[brain_llm]", json.dumps(rec, ensure_ascii=False))


class Brain:
    """L1 大脑。transport(messages)->str 可注入，便于单测。"""

    def __init__(self, cfg: dict, transport=None, version=None):
        api = cfg.get("llm_api", {}) or {}
        # 版本文件夹里的后端选择（identity.md「后端」节）优先于 robot.json；
        # version=None 时完全退回旧行为，单测不受影响。
        self.version = version
        be = idy.resolve_backend(version) if version else {}
        self.endpoint = (be.get("endpoint") or api.get("endpoint") or "").rstrip("/")
        self.model = be.get("model") or api.get("model") or ""
        self.timeout = float(be.get("timeout_seconds") or api.get("timeout_seconds") or 30)
        self.key_env = be.get("key_env") or KEY_ENV
        self.history_turns = int(api.get("history_turns", 6) or 6)
        self.max_tokens = int(api.get("max_tokens", 300) or 300)
        self.memory_events = int(api.get("memory_events", 8) or 8)
        self.memory_notes = int(api.get("memory_notes", 3) or 3)
        self._transport = transport or self._http_chat
        self._hist: dict[str, list[dict]] = {}  # speaker -> [{"role","content"}]
        # Voyager 式闭环（robot.json:agentic）。默认关：一次性回复路径已被验证，
        # 闭环需要真机联调；打开后动作同步执行并自检重试。
        ag = cfg.get("agentic", {}) or {}
        self.agentic = bool(ag.get("enabled", False))
        self.agentic_rounds = int(ag.get("max_rounds", 4) or 4)
        self.agentic_budget = int(ag.get("action_budget", 20) or 20)
        # 任务分解（mcagent）：高层目标先拆成有序子目标再逐个跑闭环。
        # 默认开——闲聊/短目标会被 planner.looks_simple 挡掉，不会多花一次往返。
        pl = ag.get("plan") or {}
        self.plan_enabled = bool(pl.get("enabled", True))
        self.plan_max = int(pl.get("max_subtasks", 6) or 6)
        # 能力分级（mcagent curriculum）：跑完记账 + 超纲提醒 + 聊天时自我认知。
        cu = ag.get("curriculum") or {}
        self.curriculum_enabled = bool(cu.get("enabled", True))

    # ---------------- HTTP ----------------
    def _http_chat(self, messages: list[dict]) -> str:
        if not self.endpoint:
            raise BrainError("robot.json:llm_api.endpoint 未配置（先填好再跑 brain=api）")
        payload: dict = {"messages": messages, "stream": False, "max_tokens": self.max_tokens}
        if self.model:
            payload["model"] = self.model
        req = urllib.request.Request(
            self.endpoint + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {os.environ.get(self.key_env, '')}"},
        )
        # 429/5xx 指数退避重试（抄 AICompanion 思路）；仍失败则抛给上层 → 不写 outbox → 45s 兜底
        delay = 1.0
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                return (data["choices"][0]["message"].get("content") or "").strip()
            except urllib.error.HTTPError as e:
                if e.code == 429 or 500 <= e.code < 600:
                    retry_after = e.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                    _log({"op": "http_retry", "code": e.code, "wait": wait, "attempt": attempt})
                    if attempt == 2:
                        raise BrainError(f"HTTP {e.code}（重试 {attempt + 1} 次仍失败）")
                    time.sleep(min(wait, 10.0))
                    delay *= 2
                else:
                    raise BrainError(f"HTTP {e.code}: {e.read()[:200]!r}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == 2:
                    raise BrainError(f"网络错误: {e}")
                _log({"op": "http_retry", "err": str(e), "attempt": attempt})
                time.sleep(delay)
                delay *= 2
        raise BrainError("unreachable")

    # ---------------- 提示词 ----------------
    def _system_content(self, extra: str = "") -> str:
        """人格 + 世界常识 + 长期记忆（事件流 + AI 便签 + 查表事实 + 能力分级自认知），
        拼进同一条 system 消息。

        有版本身份时走 idy.render_system（人格 + 自动世界常识 + 边界纪律，全部读自
        version/<版本>/ 的文档）；没有则退回内置 SYSTEM_PROMPT（单测与向后兼容）。
        """
        try:
            mem = mem_mod.fmt_for_prompt(self.memory_events, self.memory_notes)
        except Exception:
            mem = ""
        if extra:
            mem = (mem + "\n\n" + extra).strip()
        if self.version:
            content = idy.render_system(self.version, mem)
            # 自我认知：分级表的一句话摘要（据实回答「我会干啥」，没数据就说没数据）
            try:
                from .. import curriculum
                line = curriculum.summary_line(self.version)
                if line:
                    content += "\n\n" + line
            except Exception:
                pass
            return content
        content = SYSTEM_PROMPT
        if mem:
            content += "\n\n" + mem
        return content

    def build_messages(self, speaker: str, text: str, env: dict | None = None) -> list[dict]:
        # 知识库按当前这句话检索：只带相关事实，查不到就什么都不加
        try:
            from .. import knowledge as kno
            kb = kno.fmt_for_prompt(text, top_k=3, version=self.version)
        except Exception:
            kb = ""
        msgs = [{"role": "system", "content": self._system_content(kb)}]
        hist = self._hist.get(speaker, [])[-self.history_turns:]
        msgs.extend(hist)
        user = f"<{speaker}> {text}"
        if env:
            bits = [f"现在时间 {_now_text()}"]
            if env.get("pos"):
                bits.append(f"位置 {env['pos']}")
            if env.get("online"):
                bits.append("在线：" + "、".join(env["online"]))
            if env.get("inv"):
                bits.append(f"背包：{env['inv']}")
            if bits:
                user += "\n（你现在的状态：" + "；".join(bits) + "）"
        msgs.append({"role": "user", "content": user})
        return msgs

    def _remember(self, speaker: str, text: str, reply: str):
        h = self._hist.setdefault(speaker, [])
        h.append({"role": "user", "content": f"<{speaker}> {text}"})
        if reply:
            h.append({"role": "assistant", "content": reply})
        # 按「用户轮次」整体裁剪：永远成对保留，前缀稳定（缓存友好）
        while len(h) > self.history_turns + 2:
            h.pop(0)
            if h and h[0]["role"] == "assistant":
                h.pop(0)

    # ---------------- 按需查方块目录 ----------------
    @staticmethod
    def _block_keyword(text: str) -> str:
        """模型写 `[[block]]课桌` 表示要查方块目录，返回关键词。"""
        for ln in (text or "").splitlines():
            s = ln.strip()
            if s.startswith(BLOCK_MARK):
                return s[len(BLOCK_MARK):].strip()
        return ""

    def _lookup_blocks(self, keyword: str) -> str:
        """查方块目录 → 一行可注入的结果文本。"""
        hits = idy.search_blocks(keyword, 12)
        if not hits:
            return (f"目录里没有「{keyword}」。注意方块目录的显示名是英文，"
                    f"换英文关键词再查（如 desk/chair/lamp/rubbish_bin）。")
        return "；".join(f"{h['id']}（{h['name']}）" for h in hits[:8])

    # ---------------- 回复解析（安全校验在这一点） ----------------
    def parse_reply(self, text: str) -> list[dict]:
        """模型回复 → outbox 动作列表。[[cmd]] 行经 rc.analyze：危险拒执行。

        [[block]] 行只是「查目录」的中间动作，不当话发出去。
        """
        actions: list[dict] = []
        say_lines: list[str] = []
        for ln in (text or "").splitlines():
            s = ln.strip()
            if s.startswith(BLOCK_MARK):
                continue
            if s.startswith(CMD_MARK):
                command = s[len(CMD_MARK):].strip()
                if not command:
                    continue
                intent = rc.analyze(command)
                if intent.dangerous:
                    _log({"op": "cmd_refused", "command": command, "reason": intent.reason})
                    continue  # 不执行；文字回复照发
                actions.append({"cmd": command})
            elif s:
                say_lines.append(s)
        body = "\n".join(say_lines).strip()
        if body:
            actions.insert(0, {"say": body})
        return actions

    # ---------------- 主流程 ----------------
    def handle(self, rec: dict) -> bool:
        """处理一条 inbox 记录（只处理 summon=true）。返回是否写了 outbox。"""
        speaker, text = rec.get("from", "?"), rec.get("text", "")
        if self.agentic:
            return self._handle_agentic(speaker, text)
        return self._handle_once(rec, speaker, text)

    def _handle_agentic(self, speaker: str, text: str) -> bool:
        """Voyager 式闭环：感知 → 决策 → 执行 → 自检 → 最多 N 轮修正。

        与一次性回复的区别：动作**同步执行**并拿回执，自检判成败，
        失败就把批评喂回决策再试一轮；到上限或行动预算用尽则停手（不空转）。
        mcagent 的任务分解：先拆成有序子目标再逐个跑闭环（`run_plan`），
        前一个子目标的结果作为后一个的上下文。
        """
        from .agent_loop import AgentLoop
        from .checkpoint import Checkpointer
        from .llm_policy import make_decide, make_verify
        from .planner import make_planner
        from ..tools.builtin import build_registry
        from ..tools.registry import dangerous_allowed

        reg = build_registry()
        loop = AgentLoop(registry=reg,
                         decide=make_decide(self._transport, reg),
                         verify=make_verify(self._transport),
                         max_rounds=self.agentic_rounds,
                         action_budget=self.agentic_budget,
                         curriculum_enabled=self.curriculum_enabled,
                         plan_fn=make_planner(self._transport,
                                              max_subtasks=self.plan_max)
                         if self.plan_enabled else None)
        task_id = f"{speaker}-{int(time.time())}"
        try:
            s = loop.run_plan(text, actor=speaker, task_id=task_id,
                              checkpoint=Checkpointer(),
                              dangerous_allowed=dangerous_allowed(self.version))
        except Exception as e:
            _log({"op": "agentic_error", "by": speaker, "err": f"{type(e).__name__}: {e}"})
            return False
        _log({"op": "agentic_done", "by": speaker, "verdict": s.verdict,
              "round": s.round, "tool_calls": s.tool_calls,
              "steps": s.steps, "aborted": s.aborted,
              "critique": s.critique[:200]})
        # say 工具内部已经 emit 过了，这里只判断「有没有说过话」，别再发一遍
        said = any(r.ok and r.tool == "say" for r in s.receipts)
        if not said:
            # 闭环跑完必须给玩家一个回音：模型常常只调动作、忘了 say，
            # 玩家那边就是「明明在动，一声不吭」。
            how = f"{len(s.steps)} 步 " if len(s.steps) > 1 else ""
            if s.verdict == "ok":
                br.emit({"say": f"做完了，{how}{s.round} 轮 {s.tool_calls} 步"})
            elif s.verdict == "infra":
                # 考场停电要照说，但说清楚是停电不是没做成——玩家才知道该去修链路
                why = " ".join((s.infra_error or "链路故障").split())[:40]
                br.emit({"say": f"链路故障，这单没考成：{why}"})
            else:
                why = " ".join((s.critique or "没做成").split())[:44]
                br.emit({"say": f"没搞定：{why}"})
        try:
            mem_mod.add("event", f"{speaker} 说：「{text}」→ agentic {s.verdict}"
                                 f"（{len(s.steps)} 步/{s.round} 轮/{s.tool_calls} 次调用）",
                        by=speaker)
        except Exception:
            pass
        return bool(s.receipts)

    def _handle_once(self, rec: dict, speaker: str, text: str) -> bool:
        """一次性回复路径（默认）：模型回一段文本，解析出 [[cmd]] 后交给身体异步执行。"""
        try:
            msgs = self.build_messages(speaker, text, rec.get("env"))
            reply = self._transport(msgs)
            kw = self._block_keyword(reply)
            if kw:
                # 模型要查方块目录：把结果补进上下文，让它用真实方块 ID 重答一次（只补一轮）
                _log({"op": "block_lookup", "by": speaker, "kw": kw})
                msgs = msgs + [{"role": "assistant", "content": reply},
                               {"role": "user",
                                "content": f"（方块目录查询结果）{self._lookup_blocks(kw)}。"
                                           f"据此用真实方块 ID 重新回答一次，别再写 [[block]]。"}]
                reply = self._transport(msgs)
        except BrainError as e:
            _log({"op": "brain_error", "by": speaker, "err": str(e)})
            return False
        if not reply:
            _log({"op": "empty_reply", "by": speaker})
            return False
        self._remember(speaker, text, reply)
        actions = self.parse_reply(reply)
        for a in actions:
            br.emit(a)
        say = next((a["say"] for a in actions if "say" in a), None)
        try:
            mem_mod.add("event", f"{speaker} 说：「{text}」→ 我回：「{say or '（只执行了指令）'}」", by=speaker)
        except Exception:
            pass  # 记忆写不进去不影响回复
        _log({"op": "replied", "by": speaker, "text": text,
              "say": say,
              "cmds": [a["cmd"] for a in actions if "cmd" in a]})
        return bool(actions)

    def drain(self) -> int:
        """处理全部未读 inbox 记录；只回应 summon=true 的召唤。"""
        start = br._ptr()
        lines = br._lines()
        new = [r for r in (br._parse(l) for l in lines[start:]) if r]
        br._save_ptr(len(lines))
        n = 0
        for rec in new:
            if rec.get("summon"):
                if self.handle(rec):
                    n += 1
        return n

    def watch(self, timeout: float) -> int:
        """常驻等待新消息：有则处理，继续等下一批，直到超时退出(2)。

        2026-10-16 修复：旧版「处理完一批就退出」是留给 supervisor 重挂的，
        但没有 supervisor 时大脑处理一轮即下线（玩家第二声召唤没人应）。
        现改为轮内驻留：单批异常也只记录、不退出。
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                if br._ptr() < len(br._lines()):
                    self.drain()
            except Exception as e:  # 单批失败不杀守护进程
                _log({"op": "watch_error", "err": str(e)})
            if time.monotonic() >= deadline:
                return 2
            time.sleep(0.5)

    def ask(self, question: str) -> str:
        return self._transport(self.build_messages("debug", question))

    def probe(self) -> str:
        """端点连通性：GET {endpoint}/models（带 key，3s 超时）。"""
        if not self.endpoint:
            return "endpoint 未配置"
        req = urllib.request.Request(
            self.endpoint + "/models",
            headers={"Authorization": f"Bearer {os.environ.get(self.key_env, '')}"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read().decode("utf-8"))
            ids = [m.get("id") for m in data.get("data", [])][:5]
            return f"ok，模型: {ids}"
        except Exception as e:
            return f"不通: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="brain_llm")
    sub = ap.add_subparsers(dest="action", required=True)
    w = sub.add_parser("watch"); w.add_argument("--timeout", type=float, default=600.0)
    sub.add_parser("once")
    s = sub.add_parser("ask"); s.add_argument("question")
    sub.add_parser("status")
    args = ap.parse_args(argv)

    cfg = load_cfg()
    bot = Brain(cfg, version=_ACTIVE)

    if args.action == "watch":
        print(f"[brain_llm] 上线：endpoint={bot.endpoint or '(未配置)'} model={bot.model or '(默认)'} "
              f"timeout={bot.timeout}s history={bot.history_turns}")
        return bot.watch(args.timeout)
    if args.action == "once":
        n = bot.drain()
        print(f"[brain_llm] 处理完，回复 {n} 条")
        return 0
    if args.action == "ask":
        print(bot.ask(args.question))
        return 0
    if args.action == "status":
        lines = br._lines()
        info = {
            "endpoint": bot.endpoint or "(未配置)",
            "model": bot.model or "(端点默认)",
            "key_env": bot.key_env,
            "key_env_set": bool(os.environ.get(bot.key_env)),
            "inbox_lines": len(lines),
            "read_ptr": br._ptr(),
            "unread": max(0, len(lines) - br._ptr()),
            "probe": bot.probe(),
        }
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
