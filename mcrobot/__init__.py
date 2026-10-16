"""MC 接入 AI 的封装 agent。

分层（自下而上）：

    mcrobot.body     身体：与游戏客户端的桥、环境感知、L0 反射、主循环
    mcrobot.brain    大脑：inbox/outbox 协议、L1 API 大脑、L2 bridge 大脑、编排状态机
    mcrobot.tools    工具层：MCP 风格注册表（JSON Schema + 权限门控）+ stdio MCP server
    mcrobot.skills   技能库：可复用的参数化建造模板
    mcrobot.ops      运维：守护、启动客户端、SFTP、清理
    mcrobot.console  本地网页控制台

身份与环境配置**不在这里**，在 `version/<版本>/`（版本文件夹即一份 AI 身份）。
"""
from __future__ import annotations

__version__ = "0.2.0"
