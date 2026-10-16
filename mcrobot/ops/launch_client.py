#!/usr/bin/env python3
"""启动 AI 专用 Minecraft 客户端（minecraft-launcher-lib 版）。

版本/库文件读自 PCL2 的 .minecraft（只读）；游戏目录指向 mc_robot/instance；
账号 DeepSeek 离线登录，启动后自动连服务器。
"""
import os
import subprocess
import sys
import uuid

import minecraft_launcher_lib as mll

from ..paths import REPO, INSTANCE  # 唯一路径来源：别再自己算 parent（一深就错位）

WORKSPACE = str(REPO)
INSTANCE_DIR = str(INSTANCE)
MC_DIR = r"D:\Game\PCL 2\.minecraft"
JAVA = r"C:\Program Files\Java\jdk-17.0.12\bin\java.exe"

USERNAME = "DeepSeek"
MEMORY = "4G"
DEFAULT_VERSION = "1.20.1-forge"


def find_version() -> str:
    target = [n for n in os.listdir(os.path.join(MC_DIR, "versions")) if "1.20.1" in n and "野" in n]
    if not target:
        raise FileNotFoundError("找不到野棕乡 1.20.1 版本目录")
    return target[0]


def offline_uuid(name: str) -> str:
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + name))


def server_addr(version: str = DEFAULT_VERSION) -> tuple[str, str]:
    """服务器地址/端口以 version/<版本>/config.json 为准（别硬编码，端口会变）。"""
    import json as _json
    with open(os.path.join(WORKSPACE, "version", version, "config.json"),
              encoding="utf-8") as f:
        cfg = _json.load(f)
    sv = cfg.get("server") or {}
    return sv.get("host", "play.simpfun.cn"), str(sv.get("port", 15685))


def main():
    version = find_version()
    v = next((a for a in sys.argv if a.startswith("--version=")), None)
    vname = v.split("=", 1)[1] if v else DEFAULT_VERSION
    SERVER, PORT = server_addr(vname)
    options = {
        "username": USERNAME,
        "uuid": offline_uuid(USERNAME),
        "token": "0",
        "executablePath": JAVA,
        "jvmArguments": [f"-Xmx{MEMORY}", f"-Xms{MEMORY}"],
        "launcherName": "mc_robot",
        "launcherVersion": "1.0",
        "gameDirectory": INSTANCE_DIR,
        "quickPlayMultiplayer": f"{SERVER}:{PORT}",
    }
    cmd = mll.command.get_minecraft_command(version, MC_DIR, options)
    print("version =", version)
    print("gameDir =", INSTANCE_DIR)
    print("args count =", len(cmd))
    if "--dry-run" in sys.argv:
        print(cmd)
        return 0
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    import json as _json
    cfg_src = os.path.join(WORKSPACE, "version", vname, "config.json")
    with open(cfg_src, encoding="utf-8") as f:
        vcfg = _json.load(f)
    mcrobot_cfg = {
        "registerPassword": vcfg["account"]["password"],
        "gatePassword": vcfg["account"].get("gate_password", ""),
        "maxAttempts": 3,
    }
    with open(os.path.join(INSTANCE_DIR, "mcrobot.json"), "w", encoding="utf-8") as f:
        _json.dump(mcrobot_cfg, f, ensure_ascii=False)
    print("启动中…（自动连接", SERVER + ":" + PORT, "）")
    subprocess.Popen(cmd, cwd=INSTANCE_DIR, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
