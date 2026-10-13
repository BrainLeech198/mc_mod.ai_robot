#!/usr/bin/env python3
"""启动 AI 专用 Minecraft 客户端（minecraft-launcher-lib 版）。

版本/库文件读自 PCL2 的 .minecraft（只读）；游戏目录指向 mc_robot/instance；
账号 DeekSeek 离线登录，启动后自动连服务器。
"""
import os
import subprocess
import sys
import uuid

import minecraft_launcher_lib as mll

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.path.join(WORKSPACE, "instance")
MC_DIR = r"D:\Game\PCL 2\.minecraft"
JAVA = r"C:\Program Files\Java\jdk-17.0.12\bin\java.exe"

USERNAME = "DeekSeek"
MEMORY = "4G"
SERVER = "play.simpfun.cn"
PORT = "15685"


def find_version() -> str:
    target = [n for n in os.listdir(os.path.join(MC_DIR, "versions")) if "1.20.1" in n and "野" in n]
    if not target:
        raise FileNotFoundError("找不到野棕乡 1.20.1 版本目录")
    return target[0]


def offline_uuid(name: str) -> str:
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + name))


def main():
    version = find_version()
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
    print("启动中…（自动连接", SERVER + ":" + PORT, "）")
    subprocess.Popen(cmd, cwd=INSTANCE_DIR, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
