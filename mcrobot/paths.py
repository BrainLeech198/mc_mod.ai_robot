"""全项目唯一的路径来源。

各模块别再自己算 `Path(__file__).parent.parent` —— 目录一深就会错位。
"""
from __future__ import annotations

import os
from pathlib import Path

# mcrobot/paths.py -> parents[0]=mcrobot/  parents[1]=仓库根
REPO: Path = Path(__file__).resolve().parents[1]
VERSIONS: Path = REPO / "version"
INSTANCE: Path = REPO / "instance"
ROBOT_DIR: Path = INSTANCE / "robot"          # inbox/outbox/指针/日志

# 伪 API 服务：已独立为同级项目 mock_api（原 mc_robot/sim）。
# 旧目录暂时保留做回退，确认无碍后会删掉；用环境变量可临时指回别处。
MOCK_API: Path = Path(os.environ.get("MCROBOT_MOCK_API", REPO.parent / "mock_api"))
SIM: Path = MOCK_API                          # 兼容旧名，逐步替换掉


def version_dir(name: str | None = None) -> Path:
    """某个版本（身份）的目录。name 为空取当前激活版本。"""
    if not name:
        from . import identity
        name = identity.active_version()
    return VERSIONS / name


def robot_json(name: str | None = None) -> Path:
    return version_dir(name) / "robot.json"


def memory_dir(name: str | None = None) -> Path:
    return version_dir(name) / "memory"
