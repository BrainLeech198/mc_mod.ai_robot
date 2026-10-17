"""零依赖的中英混合检索打分（技能库、知识库共用）。

为什么单独抽一层：技能检索先落地时切词函数藏在 skills/library.py 里，知识库要用
同一套规则；复制一份就会出现「同一个词两边切法不同」的怪事，改一处漏一处。
"""
from __future__ import annotations

import re


def tokens(q: str) -> list[str]:
    """中英混合切词：英文按词（<3 字母丢掉，a/of 只会制造噪声），中文按 2 字滑窗。"""
    q = (q or "").lower()
    out: list[str] = []
    out.extend(t for t in re.findall(r"[a-z0-9_]+", q) if len(t) >= 3)
    for run in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(run) <= 2:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return [t for t in out if t]
