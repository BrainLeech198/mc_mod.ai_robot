#!/usr/bin/env python3
"""能力分级表（curriculum）：把「它到底会到什么程度」变成一本账。

mcagent 四件套的最后一件。前三件（任务内反思 / 技能库 / 失败沉淀教训 / 任务分解）
回答的是「怎么把一件事做成」，这一件回答的是**「哪些事已经敢让它做了」**。

设计（和知识库/技能库一致：只记有证据的，不记模型瞎说的）：
- 每个跑完的任务按**文本特征定级 L1~L5**（grade，纯规则、零 LLM 往返、可单测）；
- 结果追加进 `version/<版本>/memory/curriculum.jsonl`（append-only，一行一次任务）；
- 晋升规则：某级成功 >= PROMOTE_AFTER[级] 且成功率 >= MIN_RATE 才算解锁，
  **逐级跳不过**（L4 达标但 L3 没达标，只解锁到 L2）；
- 大脑只在「目标定级 > 已验证等级」时收到一句提醒（note）——分级表是给自己看的
  诚实账，**不是权限门**：超纲的任务照跑，但要它步步为营、做不成就直说。

和其他层的关系：
- 技能库记「怎么做成的」（动作序列），分级表记「做到没做到」（成败与级别）；
- 教训库记「为什么失败」（知识），分级表只记级别和成败，不记原因——原因归教训。

用法：
  python -m mcrobot.curriculum            # 看分级表（默认 = show）
  python -m mcrobot.curriculum levels     # 各级别定义与定级规则
  python -m mcrobot.curriculum path       # 数据文件位置
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 级别定义：编号 -> (名称, 一句话说明)。顺序即从易到难。
LEVELS: dict[int, tuple[str, str]] = {
    1: ("交流感知", "说话、回答问题、读状态——只读不改，说错了也就是一句话"),
    2: ("观察描述", "扫区域、看外观、描述环境——产出是信息，不改世界"),
    3: ("单点修改", "放/挖/补少量方块——有世界回执可以逐点核对"),
    4: ("小型建造", "一段墙、一个平台、一间房——多步且有依赖"),
    5: ("工程建造", "学校这类多子目标项目——要分解、要验收、常带危险权限"),
}

# 晋升门槛：某级成功这么多次且成功率不低于 MIN_RATE，才解锁这一级
PROMOTE_AFTER: dict[int, int] = {1: 3, 2: 3, 3: 3, 4: 3, 5: 2}
MIN_RATE = 0.5

# ---- 定级词表（grade 用；顺序即优先级，先命中高级别） ----
# L5：工程级信号——项目名、大尺度、世界编辑大动作
_L5_WORDS = ("学校", "教学楼", "操场", "跑道", "足球", "小区", "城堡", "宫殿", "大桥",
             "迷宫", "庄园", "大楼", "多层", "四层", "五层", "大范围", "大规模", "批量",
             "worldedit", "//set", "//replace", "全校", "整片")
_L5_SCALE = re.compile(r"\d{2,}\s*(?:米|m|间|层|人|班|平)")     # 12间 / 200平 / 30米
_L5_BLOCKS = re.compile(r"\d{3,}\s*格")                          # 100格才算工程级，10格只是小品
_L5_DIM = re.compile(r"\d+\s*[×x]\s*\d+\s*(?:米|m)")              # 5×4米
# L4：建造词
_L4_WORDS = ("建", "盖", "砌", "造", "搭", "装修", "装饰", "围一")
# L3：单点修改词
_L3_WORDS = ("放", "挖", "补", "拆", "换", "填", "修", "清理", "收拾", "种", "改")
# L2：观察词
_L2_WORDS = ("看", "扫", "描述", "说说", "读", "查", "在哪", "是什么", "多少",
             "长什么样", "周围", "环境", "确认", "有没有", "告诉")
# L1：闲聊词
_L1_WORDS = ("你好", "在吗", "谢谢", "再见", "是谁", "名字", "帮助", "help", "天气",
             "干嘛", "hello", "hi", "早", "晚")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _path(version: str | None = None) -> Path:
    """分级表文件。MCROBOT_CURRICULUM_FILE 可临时指到别处（单测用）。"""
    env = os.environ.get("MCROBOT_CURRICULUM_FILE")
    if env:
        return Path(env)
    from .paths import memory_dir
    return memory_dir(version) / "curriculum.jsonl"


# ------------------------------ 定级 ------------------------------


def grade(goal: str, steps: list[str] | None = None) -> int:
    """给目标定级 1~5。纯文本规则，不花 LLM 往返。

    steps 是 planner 拆出来的子目标：子目标里的动词也参与文本匹配（「把活干完」拆出
    「砌墙」就是建造）；另外 **5 个以上有序步骤本身就算工程级**。但只读目标不因步骤多
    而抬级——把一件事拆成三步去看，不会比看一眼更难。
    """
    g = " ".join((goal or "").split()).lower()
    if not g:
        return 1
    blob = g + " " + " ".join(" ".join(str(s).split()).lower() for s in (steps or []))
    # 「建筑/建设/建议」里的「建」是名词不是动词，先剔掉再匹配建造词
    for noun in ("建筑", "建设", "建议"):
        blob = blob.replace(noun, "")
    if (any(w in blob for w in _L5_WORDS) or _L5_SCALE.search(blob)
            or _L5_BLOCKS.search(blob) or _L5_DIM.search(blob)):
        return 5
    if any(w in blob for w in _L4_WORDS):
        return _steps_bump(4, steps)
    if any(w in blob for w in _L3_WORDS):
        return _steps_bump(3, steps)
    if any(w in blob for w in _L2_WORDS):
        return 2
    if any(w in blob for w in _L1_WORDS):
        return 1
    return 2


def _steps_bump(base: int, steps: list[str] | None) -> int:
    """子目标数只往工程级抬：5+ 个有序步骤本身就是项目，3 步的琐碎链不算
    （子目标里的动词已经参与文本匹配了，「放一格石+核对+报告」该是 L3 不是 L4）。
    """
    n = len([s for s in (steps or []) if str(s).strip()])
    if n >= 5:
        return 5
    return base


# ------------------------------ 记账 ------------------------------


def load(version: str | None = None) -> list[dict]:
    """读全部任务记录（一行一次跑完的任务，append-only）。"""
    p = _path(version)
    if not p.exists():
        return []
    out: list[dict] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("level"):
            out.append(d)
    return out


def record(goal: str, verdict: str, *, level: int | None = None, steps: int = 0,
           task_id: str = "", version: str | None = None) -> dict:
    """记一次任务的结果。verdict 非 "ok" 一律记 fail（含熔断/跳过导致的整体失败）。"""
    if level is None:
        level = grade(goal)
    rec = {"ts": _now(), "task_id": task_id or "",
           "goal": " ".join((goal or "").split())[:80],
           "level": int(level), "steps": int(steps),
           "verdict": "ok" if verdict == "ok" else "fail"}
    p = _path(version)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


# ------------------------------ 统计与晋升 ------------------------------


def stats(version: str | None = None,
          thresholds: dict[int, int] | None = None) -> list[dict]:
    """各级别统计 + 解锁状态。unlocked 逐级计算：下级没解锁，上级达标也不算。"""
    th = dict(PROMOTE_AFTER)
    if thresholds:
        th.update({int(k): int(v) for k, v in thresholds.items()})
    recs = load(version)
    rows: list[dict] = []
    for lv in sorted(LEVELS):
        mine = [r for r in recs if int(r.get("level") or 0) == lv]
        ok = sum(1 for r in mine if r.get("verdict") == "ok")
        rows.append({"level": lv, "name": LEVELS[lv][0], "desc": LEVELS[lv][1],
                     "tries": len(mine), "ok": ok,
                     "rate": round(ok / len(mine), 2) if mine else 0.0,
                     "need": th.get(lv, 3), "unlocked": False})
    top = 0
    for r in rows:
        if r["ok"] >= r["need"] and (not r["tries"] or r["ok"] / r["tries"] >= MIN_RATE):
            top = r["level"]
        else:
            break
    for r in rows:
        r["unlocked"] = r["level"] <= top
    return rows


def unlocked_level(version: str | None = None,
                   thresholds: dict[int, int] | None = None) -> int:
    for r in stats(version, thresholds):
        if r["unlocked"]:
            continue
        return r["level"] - 1
    return max(LEVELS)


def note(level: int, version: str | None = None) -> str:
    """给模型的超纲提醒。没超纲（level <= 已验证等级）返回空串——不塞废话。"""
    lv = int(level)
    if lv <= unlocked_level(version):
        return ""
    name = LEVELS.get(lv, ("未知", ""))[0]
    top = unlocked_level(version)
    if top <= 0:
        return (f"【能力分级】这个目标定级 L{lv}（{name}），但你的分级表还是空的——"
                f"从没被验证过。拆成小子目标一步步做，每步做完拿 block_at/scan_region 验；"
                f"做不成就直说做不成了，别硬装。")
    uname = LEVELS.get(top, ("未知", ""))[0]
    return (f"【能力分级】这个目标定级 L{lv}（{name}），你验证过的最高是 L{top}（{uname}）"
            f"——超纲了。一步一步做，每步拿 block_at/scan_region 验；做不成就直说，"
            f"别硬装做完了。")


def report(version: str | None = None) -> str:
    """人/模型都能看的分级表。"""
    rows = stats(version)
    top = unlocked_level(version)
    lines: list[str] = []
    for r in rows:
        pct = f"{round(r['rate'] * 100)}%" if r["tries"] else "-"
        if r["unlocked"]:
            flag = "✓ 已解锁"
        elif r["ok"] >= r["need"]:
            flag = f"✗ 成功率 {pct} 不够（要 ≥{round(MIN_RATE * 100)}%）"
        else:
            flag = f"✗ 还差 {r['need'] - r['ok']} 次成功"
        lines.append(f"L{r['level']} {r['name']}（{r['desc']}）："
                     f"{r['tries']} 次尝试 / {r['ok']} 次成功（{pct}） {flag}")
    if top <= 0:
        if any(r["tries"] for r in rows):
            lines.append("已验证到：无（有记录，但还没达到任何一级的解锁条件）")
        else:
            lines.append("已验证到：无（分级表还是空的）")
    else:
        nxt = LEVELS.get(top + 1)
        lines.append(f"已验证到：L{top}（{LEVELS[top][0]}）"
                     + (f"；下一级 L{top + 1}（{nxt[0]}）" if nxt else "；已是最高级"))
    return "\n".join(lines)


def summary_line(version: str | None = None) -> str:
    """给聊天提示词用的一句话自我认知（没数据就说没数据，不编）。"""
    top = unlocked_level(version)
    if top <= 0:
        return "我的能力分级表还是空的——还没有任何任务被验证过。"
    nxt = LEVELS.get(top + 1)
    if not nxt:
        return f"我的能力分级：已验证到 L{top}（{LEVELS[top][0]}），最高级。"
    row = next(r for r in stats(version) if r["level"] == top + 1)
    gap = (f"还差 {row['need'] - row['ok']} 次成功" if row["ok"] < row["need"]
           else f"成功率 {round(row['rate'] * 100)}% 不够")
    return (f"我的能力分级：已验证到 L{top}（{LEVELS[top][0]}）；"
            f"下一级 L{top + 1}（{nxt[0]}）{gap}。")


# -------------------------------- CLI --------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="curriculum", description="能力分级表")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("show", help="看分级表（默认）")
    sub.add_parser("levels", help="各级别定义")
    sub.add_parser("path", help="数据文件位置")
    args = ap.parse_args(argv)

    if args.action == "levels":
        for lv in sorted(LEVELS):
            print(f"L{lv} {LEVELS[lv][0]}：{LEVELS[lv][1]}"
                  f"（晋升需成功 {PROMOTE_AFTER.get(lv, 3)} 次且成功率 ≥"
                  f"{round(MIN_RATE * 100)}%）")
        return 0
    if args.action == "path":
        print(_path())
        return 0
    print(report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
