#!/usr/bin/env python3
"""聊天指挥通道：读游戏内聊天 → 判断是否在叫机器人 → 回复 / 执行（受授权约束）。

设计（2026-10-15 与用户约定）：
- 触发：**明确召唤**（提及名字 或 `!ds`/`!d` 前缀）必应；**附近聆听**——没点名但人就在身边
  （同维度 + `robot.json:listen.radius_blocks` 内）也当在叫它；更远的普通聊天走**抽样听**（记录 + 可选低概率接话）。
- 授权：**危险操作（改世界 / 权限 / 管理）仅 `robot.json:auth.dangerous_allowed` 里的玩家**可下；其余玩家只会被礼貌拒绝。
  靠附近聆听听到的话不算点名，其中的指令同样只认白名单内的人。
- 大脑：`bridge`（AI/预留 API 经 inbox-outbox 决策）+ 本文件内置规则快答。预留 `api`（大模型常驻）。

用法：
  python -m mcrobot.body.loop --once       # 处理自上次以来新增的聊天后退出
  python -m mcrobot.body.loop             # 常驻轮询（Ctrl-C 退出）
  python -m mcrobot.body.loop --once --dry  # 只打印决策，不真发
  python -m mcrobot.body.loop --from-scratch  # 忽略游标，从头处理
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import bridge as cc
from . import reflex as rc
from ..paths import REPO
CFG_PATH = REPO / "version" / "1.20.1-forge" / "robot.json"  # 未激活客户端时的默认


def cfg_path() -> Path:
    """行为配置路径：取激活版本文件夹里的 robot.json（版本目录即身份）。"""
    try:
        from .. import identity as idy
        p = idy.robot_json_path()
        if p.exists():
            return p
    except Exception:
        pass
    return CFG_PATH

# 即时回执池：被召唤后先应一声，别让玩家干等。随机选，避免每次同一句像机器人。
# 可在 robot.json:replies.ack_pool 里覆盖（{speaker} 会被替换成对方名字）。
DEFAULT_ACK_POOL = [
    "收到，我想想。",
    "嗯？等我瞅一眼。",
    "哦？这事我看看。",
    "收到，我琢磨琢磨。",
    "在看了在看了。",
    "嘶——我想一下。",
    "嗯，稍等，脑子转一下。",
    "看着呢，捋一捋。",
]

# 招呼回复池：L0 反射层秒回「在吗/你好」，随机选一句，别每次同一句。
DEFAULT_GREET_POOL = [
    "在呢在呢，怎么了？",
    "在的，刚在看东西，你说。",
    "嗯？叫我呢？在。",
    "在，一直都在（就是脑子偶尔掉线）。",
    "到！有什么指示？",
]

# 大脑超时兜底池：召唤交给大脑却迟迟没回时说的话。玩家视角的口吻，
# 不要暴露「大脑/服务/API」这类内部术语——那听起来像机器人坏了。
DEFAULT_FALLBACK_POOL = [
    "嘶，我刚走神了，你再说一遍？",
    "刚才卡了一下，你说啥来着？",
    "抱歉走神了，重说一次呗。",
    "脑子断片了…你刚说啥？",
]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Robot:
    def __init__(self, cfg: dict, dry: bool = False):
        self.cfg = cfg
        self.dry = dry
        self.nick = cfg["nickname"]
        self.prefixes = list(cfg["summon"]["prefixes"])
        self.mention = bool(cfg["summon"]["mention"])
        self.allowed = set(cfg["auth"]["dangerous_allowed"])
        self.ambient = cfg.get("ambient", {})
        ls = cfg.get("listen") or {}
        self.listen_nearby = bool(ls.get("nearby_enabled", False))
        self.listen_radius = float(ls.get("radius_blocks", 24.0))
        self.listen_same_dim = bool(ls.get("same_dim_only", True))
        self.listen_cache_seconds = float(ls.get("cache_seconds", 3.0))
        self.listen_dim_cache_seconds = float(ls.get("dim_cache_seconds", 30.0))
        self.queue_all_summon = bool(cfg.get("queue_all_summon", True))
        th = cfg.get("throttle", {})
        self.dedup_window = float(th.get("dedup_window_seconds", 6.0))
        self.min_send_interval = float(th.get("min_send_interval_seconds", 1.0))
        self.max_consecutive = int(th.get("max_consecutive_auto_replies", 2))
        self.chain_reset_seconds = float(th.get("chain_reset_seconds", 10.0))
        self.conv_window = float(th.get("conversation_window_seconds", 120.0))
        self.brain_timeout = float(cfg.get("brain_timeout_seconds", 45.0))
        rep = cfg.get("replies") or {}
        self.ack_pool = list(rep.get("ack_pool") or DEFAULT_ACK_POOL)
        self.greet_pool = list(rep.get("greet_pool") or DEFAULT_GREET_POOL)
        self.fallback_pool = list(rep.get("fallback_pool") or DEFAULT_FALLBACK_POOL)
        self.env_snapshot = bool(cfg.get("env_snapshot", True))
        self._online_cache: tuple[float, list[str]] | None = None  # (monotonic, names)
        self._inv_cache: tuple[float, str] | None = None
        self._entity_cache: dict[str, tuple[float, dict | None]] = {}  # 名字 -> (monotonic, {pos}|None)
        self._dim_cache: dict[str, tuple[float, str | None]] = {}  # 名字 -> (monotonic, 维度|None)
        self._last_summon: dict[str, float] = {}
        self._pending: list[tuple[float, str, str]] = []  # (ts, speaker, text) 已交大脑、待回
        self._recent: dict[tuple[str, str], float] = {}
        self._last_send_ts = 0.0
        self._chain_speaker: str | None = None
        self._chain_count = 0
        self._chain_ts = 0.0
        p = cfg["paths"]
        self.log_path = REPO / p["log"]
        self.inbox = REPO / p["inbox"]
        self.outbox = REPO / p["outbox"]
        self.cursor_path = REPO / p["cursor"]
        for d in (self.log_path.parent, self.inbox.parent):
            d.mkdir(parents=True, exist_ok=True)

    # ---------------- IO ----------------
    def log(self, **rec):
        rec = {"ts": _now(), **rec}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("[log]", json.dumps(rec, ensure_ascii=False))

    def say(self, text: str):
        self.log(op="say", text=text)
        if self.dry:
            return
        gap = self.min_send_interval - (time.monotonic() - self._last_send_ts)
        if gap > 0:
            time.sleep(gap)
        cc.chat(text)
        self._last_send_ts = time.monotonic()

    def auto_reply(self, speaker: str, text: str) -> bool:
        """受节流约束的自动回复（问候/快答/抽样接话）。超限只记日志、不发。"""
        now = time.monotonic()
        if self._chain_speaker == speaker and (now - self._chain_ts) < self.chain_reset_seconds:
            self._chain_count += 1
        else:
            self._chain_speaker, self._chain_count = speaker, 1
        self._chain_ts = now
        if self._chain_count > self.max_consecutive:
            self.log(op="throttle_skip", by=speaker, text=text, chain=self._chain_count)
            return False
        self.say(text)
        return True

    def is_duplicate(self, speaker: str, text: str) -> bool:
        """同人同内容在窗口内重复 → True（只处理一次）。"""
        if self.dedup_window <= 0:
            return False
        key = (speaker, (text or "").strip().lower())
        now = time.monotonic()
        last = self._recent.get(key)
        self._recent[key] = now
        if len(self._recent) > 256:  # 防无限增长
            cutoff = now - self.dedup_window
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        if last is not None and (now - last) < self.dedup_window:
            self.log(op="dedup_skip", by=speaker, text=text)
            return True
        return False

    def run_cmd(self, command: str):
        self.log(op="cmd", command=command)
        if not self.dry:
            cc.cmd(command)

    def cmd_read(self, command: str, wait: float = 2.0) -> list[str]:
        """发指令并读回随后的聊天行（用于取 /list 等结果）。"""
        base = self._latest_seq()
        self.run_cmd(command)
        time.sleep(wait)
        return [c["text"] for c in self._chats_since(base)]

    def _chats_since(self, seq: int) -> list[dict]:
        return cc.chat_since(seq).get("chats", [])

    def _latest_seq(self) -> int:
        return int(cc.chat_since(0).get("seq", 0))

    def load_cursor(self) -> int:
        if self.cursor_path.exists():
            try:
                return int(json.loads(self.cursor_path.read_text(encoding="utf-8"))["seq"])
            except Exception:
                return 0
        return 0

    def save_cursor(self, seq: int):
        if not self.dry:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            self.cursor_path.write_text(json.dumps({"seq": seq}), encoding="utf-8")

    # ---------------- 决策 ----------------
    def is_summon(self, text: str) -> bool:
        if not self.mention:
            return rc.is_summon(text, "", self.prefixes)
        return rc.is_summon(text, self.nick, self.prefixes)

    def handle(self, speaker: str, text: str):
        if speaker == self.nick:  # 自己的话不回
            return
        if self.is_duplicate(speaker, text):  # 同人同内容窗口内只处理一次
            return
        explicit = self.is_summon(text)
        now = time.monotonic()
        if explicit:
            self._last_summon[speaker] = now
            nearby = False
        else:
            ts = self._last_summon.get(speaker)
            if ts is not None and (now - ts) < self.conv_window:
                explicit = True  # 会话窗口内：跟他继续聊也算召唤，不用重新 @
                nearby = False
            else:
                nearby = self.is_nearby(speaker)  # 身边有人说话，不用点名也算在叫我
                if nearby:
                    self._last_summon[speaker] = now
                    self.log(op="nearby_summon", by=speaker, text=text)
        summon = explicit or nearby
        intent = rc.analyze(text)

        if intent.dangerous and intent.kind in ("we", "command"):
            self.exec_or_refuse(speaker, intent, text)
            return

        if not summon:
            self.record_ambient(speaker, text)
            return

        self.route_summon(speaker, text, intent, nearby=nearby)

    def exec_or_refuse(self, speaker: str, intent: rc.Intent, raw: str):
        if speaker in self.allowed:
            self.log(op="exec_dangerous", by=speaker, command=intent.command, reason=intent.reason)
            if intent.kind == "we":
                self.run_cmd(intent.command)
                self.report_we_result(speaker, intent.command)
            else:
                self.run_cmd(intent.command)
                self.say(f"@{speaker} 已执行：{intent.command}")
        else:
            self.log(op="refuse", by=speaker, command=intent.command, reason=intent.reason)
            self.say(f"@{speaker} 这属于危险操作（{intent.reason}），只有服主能让我做。")

    def report_we_result(self, speaker: str, command: str, wait: float = 1.5):
        base = self._latest_seq()
        time.sleep(wait)
        lines = [c["text"] for c in self._chats_since(base) if c["text"]]
        msg = lines[-1] if lines else "（无回执）"
        self.say(f"@{speaker} WE 回执：{msg}")

    def route_summon(self, speaker: str, text: str, intent: rc.Intent, nearby: bool = False):
        if intent.kind in ("we", "command"):
            # 被召唤 + 安全只读命令。但「附近听到」不算点名：这类指令只认白名单里的人，
            # 免得随便一个站在旁边的路人就能把它指挥走。
            if nearby and speaker not in self.allowed:
                self.log(op="refuse", by=speaker, command=intent.command, reason="nearby_not_allowed")
                self.say(f"@{speaker} 这个我得等服主点头才敢动，你让他跟我说一声。")
                return
            self.run_cmd(intent.command)
            return
        body = rc.strip_summon(text, self.nick, self.prefixes) or text
        # L0 反射优先：招呼/位置/在线/帮助这类能秒回的先秒回——玩家不必干等大脑。
        # 只有 L0 没命中，才交给 L1 大脑（先应一声，再带上环境快照入队）。
        reply = self.rule_reply(speaker, body)
        if reply is not None:
            self.auto_reply(speaker, reply)
            return
        self.auto_reply(speaker, self.ack(speaker))
        if self.queue_all_summon:
            env = self.snapshot_env() if self.env_snapshot else None
            self.queue_brain(speaker, text, intent, summon=True, env=env)

    def rule_reply(self, speaker: str, body: str) -> str | None:
        b = body.lower()
        if any(k in b for k in ("你好", "您好", "在吗", "在不在", "hi", "hello", "hey", "哈喽")):
            return f"@{speaker} {random.choice(self.greet_pool)}"
        if any(k in b for k in ("你是谁", "你谁", "你叫什么", "名字是什么", "介绍一下")):
            return (f"@{speaker} 我是 DeepSeek，这服里常住的 AI 玩家——逛地图、盖房子、陪人扯淡。"
                    f"会 WorldEdit，改世界的活得服主点头。")
        if any(k in b for k in ("位置", "坐标", "在哪", "哪里", "where")):
            try:
                p = cc.state()["player"]
                return f"@{speaker} 我在 {p['dim']} ({p['x']:.1f}, {p['y']:.1f}, {p['z']:.1f})。"
            except Exception:
                return None
        if any(k in b for k in ("谁在", "在线", "几个人", "多少人", "list")):
            lines = self.cmd_read("/list")
            hit = next((l for l in lines if "players online" in l or "在线" in l), None)
            return f"@{speaker} {hit}" if hit else None
        if any(k in b for k in ("帮助", "help", "能干什么", "会什么", "怎么用")):
            return (f"@{speaker} 我能：聊天、报位置、查在线、用 WorldEdit 盖房子（//sel //set //walls "
                    f"//copy //paste //undo 等）。改世界的操作只有服主能授权。")
        return None

    def record_ambient(self, speaker: str, text: str):
        if not self.ambient.get("enabled", True):
            return
        self.log(op="ambient", by=speaker, text=text)
        self.queue_brain(speaker, text, rc.Intent("chat", None, False, "ambient"))
        kws = self.ambient.get("keywords", [])
        prob = float(self.ambient.get("reply_probability", 0.0))
        if any(k.lower() in text.lower() for k in kws):
            self.auto_reply(speaker, f"@{speaker} 我在听，需要我做什么？")
        elif prob > 0 and random.random() < prob:
            self.auto_reply(speaker, f"@{speaker} 嗯…")

    def ack(self, speaker: str) -> str:
        """即时回执：从池里随机选一句，套上 @对方。"""
        return f"@{speaker} {random.choice(self.ack_pool)}"

    # ---------------- 环境快照（玩家视角） ----------------
    def _online_players(self, ttl: float = 30.0) -> list[str]:
        """在线玩家名单（/list，带 TTL 缓存）。失败返回 []。"""
        now = time.monotonic()
        if self._online_cache and now - self._online_cache[0] < ttl:
            return self._online_cache[1]
        try:
            lines = self.cmd_read("/list")
        except Exception:
            return []
        names: list[str] = []
        for ln in lines:
            if "online:" in ln or "在线" in ln:
                tail = ln.split("online:")[-1].split("在线")[-1]
                names = [n.strip() for n in re.split(r"[,，、]", tail) if n.strip()]
                break
        self._online_cache = (now, names)
        return names

    def _inventory_str(self, ttl: float = 60.0) -> str:
        """背包摘要（/data get entity <nick> 解析 NBT，带 TTL 缓存）。失败返回 ""。"""
        now = time.monotonic()
        if self._inv_cache and now - self._inv_cache[0] < ttl:
            return self._inv_cache[1]
        try:
            lines = self.cmd_read(f"/data get entity {self.nick}")
        except Exception:
            return ""
        counts: dict[str, int] = {}
        for ln in lines:
            if "entity data" not in ln:
                continue
            for m in re.finditer(r'Slot: \d+b, id: "([^"]+)", Count: (\d+)b', ln):
                counts[m.group(1)] = counts.get(m.group(1), 0) + int(m.group(2))
        if not counts:
            return ""
        parts = []
        for item, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            name = item.replace("minecraft:", "")
            parts.append(f"{name} x{n}" if n > 1 else name)
        s = "、".join(parts)
        self._inv_cache = (now, s)
        return s

    def _entity_info(self, name: str) -> dict | None:
        """某个玩家的位置（/data get，带缓存）。拿不到返回 None。"""
        now = time.monotonic()
        hit = self._entity_cache.get(name)
        if hit and now - hit[0] < self.listen_cache_seconds:
            return hit[1]
        info: dict | None = None
        try:
            for ln in self.cmd_read(f"/data get entity {name} Pos", wait=1.2):
                m = re.search(r"entity data:\s*\[(-?[\d.]+)d,\s*(-?[\d.]+)d,\s*(-?[\d.]+)d\]", ln)
                if m:
                    info = {"pos": tuple(float(m.group(i)) for i in (1, 2, 3))}
                    break
        except Exception:
            info = None
        self._entity_cache[name] = (now, info)
        return info

    def _entity_dim(self, name: str) -> str | None:
        """玩家所在维度（缓存比位置更久：维度不常变，省一次阻塞查询）。"""
        now = time.monotonic()
        hit = self._dim_cache.get(name)
        if hit and now - hit[0] < self.listen_dim_cache_seconds:
            return hit[1]
        dim: str | None = None
        try:
            for ln in self.cmd_read(f"/data get entity {name} Dimension", wait=1.0):
                m = re.search(r'entity data:\s*"?([A-Za-z0-9_.:-]+)"?', ln)
                if m:
                    dim = m.group(1)
                    break
        except Exception:
            dim = None
        self._dim_cache[name] = (now, dim)
        return dim

    def is_nearby(self, speaker: str) -> bool:
        """说话的人就在身边（同维度 + 半径内）？拿不到位置/维度一律按不在附近处理。

        先算距离再查维度：远处的人不必多花一次 /data 查询——每次查询都要等回执，
        而主循环是 1 秒一拍，能省则省。
        """
        if not self.listen_nearby:
            return False
        try:
            me = cc.state().get("player")
            if not me:
                return False
        except Exception:
            return False
        info = self._entity_info(speaker)
        if not info or info.get("pos") is None:
            return False
        px, py, pz = info["pos"]
        dx, dy, dz = px - me["x"], py - me["y"], pz - me["z"]
        if (dx * dx + dy * dy + dz * dz) ** 0.5 > self.listen_radius:
            return False
        if self.listen_same_dim:
            other_dim = self._entity_dim(speaker)
            if not other_dim or str(other_dim) != str(me.get("dim")):
                return False
        return True

    def snapshot_env(self) -> dict:
        """交给大脑时的环境快照：我在哪、谁在线、我背包里有什么。"""
        env: dict = {}
        try:
            p = cc.state().get("player")
            if p:
                env["pos"] = f"{p['dim']} ({p['x']:.1f}, {p['y']:.1f}, {p['z']:.1f})"
        except Exception:
            pass
        try:
            online = self._online_players()
            if online:
                env["online"] = online
        except Exception:
            pass
        try:
            inv = self._inventory_str()
            if inv:
                env["inv"] = inv
        except Exception:
            pass
        return env

    def queue_brain(self, speaker: str, text: str, intent: rc.Intent, summon: bool = False,
                    env: dict | None = None):
        rec = {"ts": _now(), "from": speaker, "text": text,
               "intent": intent.kind, "dangerous": intent.dangerous, "summon": summon}
        if env:
            rec["env"] = env
        with self.inbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if summon:
            self._pending.append((time.monotonic(), speaker, text))
            self.log(op="brain_pending", by=speaker, text=text)

    # ---------------- outbox：让 AI/API 注入决策 ----------------
    def drain_outbox(self):
        if not self.outbox.exists():
            return
        lines = self.outbox.read_text(encoding="utf-8").splitlines()
        self.outbox.write_text("", encoding="utf-8")
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                act = json.loads(ln)
            except Exception:
                continue
            if "say" in act:
                if self._pending:
                    self._pending.pop(0)  # 大脑回话了，清掉最旧的一条待办
                self.say(act["say"])
            if "cmd" in act:
                self.run_cmd(act["cmd"])

    def check_brain_timeouts(self):
        """交给大脑(桥/API)的召唤若超时没回，兜底发一句——保证玩家永不落空。"""
        if self.brain_timeout <= 0 or not self._pending:
            return
        now = time.monotonic()
        still = []
        for ts, speaker, text in self._pending:
            if now - ts >= self.brain_timeout:
                self.log(op="brain_timeout", by=speaker, text=text)
                # 直发（不受自动回复条数上限约束），这是"保证送达"类消息。
                self.say(f"@{speaker} {random.choice(self.fallback_pool)}")
            else:
                still.append((ts, speaker, text))
        self._pending = still

    # ---------------- 主循环 ----------------
    def tick(self) -> int:
        seq = self.load_cursor()
        data = cc.chat_since(seq)
        for c in data.get("chats", []):
            kind, speaker, text = rc.parse(c["text"])
            if kind == "player":
                self.handle(speaker, text)
        self.drain_outbox()
        self.check_brain_timeouts()
        latest = int(data.get("seq", seq))
        self.save_cursor(latest)
        return latest

    def run(self, interval: float, once: bool):
        while True:
            try:
                self.tick()
            except cc.ControlError as e:
                print("[warn] 控制桥不可用：", e, file=sys.stderr)
                if once:
                    return 1
            if once:
                return 0
            time.sleep(interval)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="robot")
    ap.add_argument("--once", action="store_true", help="处理一次后退出")
    ap.add_argument("--dry", action="store_true", help="只打印决策，不真发")
    ap.add_argument("--from-scratch", action="store_true", help="忽略游标，从头处理")
    ap.add_argument("--interval", type=float, default=None)
    args = ap.parse_args(argv)

    cfg = json.loads(cfg_path().read_text(encoding="utf-8"))
    bot = Robot(cfg, dry=args.dry)
    if args.from_scratch:
        bot.save_cursor(0)
    interval = args.interval if args.interval is not None else float(cfg.get("sample_interval_seconds", 1.0))
    print(f"[robot] 上线：nick={bot.nick} 授权={sorted(bot.allowed)} dry={args.dry} once={args.once}")
    close_login_screen(bot)
    return bot.run(interval, args.once)


def close_login_screen(bot: Robot):
    """进服后关掉登录 mod 留在屏幕上的铁砧 GUI（只关一次，不碰玩家后来开的界面）。"""
    if bot.dry:
        return
    try:
        if cc.health().get("inGame"):
            cc.close()
            bot.log(op="screen_close", reason="login_anvil")
            print("[robot] 已关掉登录残留的铁砧 GUI")
    except cc.ControlError as e:
        print("[warn] 关登录 GUI 失败（不影响主循环）：", e)


if __name__ == "__main__":
    sys.exit(main())
