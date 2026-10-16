#!/usr/bin/env python3
"""服务器清理脚本（简幻欢 SFTP）。

默认 dry-run：只列出将删除的内容和释放空间，不动任何文件。
确认后加 --execute 才真正删除。

用法：
    python tools/server_cleanup.py              # 演习，列出将删内容
    python tools/server_cleanup.py --execute    # 执行清理（小件，约 176M）
    python tools/server_cleanup.py --execute --include-dh   # 额外删 5.3G DH 库（必须先停服！）

安全护栏：
- 路径拒绝 .. 与绝对路径
- 重复 jar 删除前强制校验 hash 一致
- DH 库必须显式 --include-dh，且会打印停服警告
"""
import argparse
import hashlib
import sys
from pathlib import Path


from sftp_client import connect, is_dir, human  # noqa: E402

# 重复 mod 的两个拷贝（全名不同，方括号前缀不同）
ARCH_A = "[前置·被多维度、创世神UI、沉浸式飞机、云北城建依赖] architectury-9.2.14-forge.jar"
ARCH_B = "[前置·被多维度、创世神UI、沉浸式飞机依赖] architectury-9.2.14-forge.jar"
DH_DB = "world/data/DistantHorizons.sqlite"
DH_WAL = "world/data/DistantHorizons.sqlite-wal"
DH_SHM = "world/data/DistantHorizons.sqlite-shm"
# logs 目录里这些文件正在被服务端写，不删
LOGS_KEEP = {"latest.log", "debug.log"}


def safe(rel):
    """规范化并校验路径：拒绝 .. / 绝对路径 / 空"""
    if not rel or rel.startswith("/") or ".." in Path(rel).parts or rel in (".",):
        raise ValueError(f"unsafe path: {rel!r}")
    return rel.rstrip("/")


def md5(sftp, path):
    h = hashlib.md5()
    with sftp.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_stats(sftp, path):
    """返回 (总字节, 文件数)，跳过打不开的子目录"""
    total, count = 0, 0
    try:
        attrs = sftp.listdir_attr(path)
    except IOError:
        return 0, 0
    for a in attrs:
        child = f"{path.rstrip('/')}/{a.filename}"
        if is_dir(a):
            t, c = dir_stats(sftp, child)
            total += t
            count += c
        else:
            total += a.st_size or 0
            count += 1
    return total, count


def rm_tree(sftp, path):
    """递归删除目录；先删文件再删空目录"""
    for a in sftp.listdir_attr(path):
        child = f"{path.rstrip('/')}/{a.filename}"
        if is_dir(a):
            rm_tree(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def collect_targets(include_dh):
    """生成 (路径, 删除方式, 说明) 列表"""
    return [
        ("server_map_cache", "tree", "MapSyncer 缓存，可再生"),
        ("crash-reports", "tree", "崩溃报告"),
        # bluemap/minecraft-client-*.jar 是 BlueMap 渲染 3D 模型必需的 Mojang 客户端资源
        # （core.conf: accept-download=true 时缺失会自动重下），不是误传文件，禁止删除
        (f"mods/{ARCH_B}", "file", "重复的 architectury 拷贝（删前校验 hash）"),
        ("logs", "logs", "旧日志轮换（保留 latest.log/debug.log）"),
    ] + ([
        (DH_DB, "file", "DH LOD 数据库 5.3G（必须先停服）"),
        (DH_WAL, "file", "DH WAL"),
        (DH_SHM, "file", "DH SHM"),
    ] if include_dh else [])


def main():
    p = argparse.ArgumentParser(description="服务器清理（默认 dry-run）")
    p.add_argument("--execute", action="store_true", help="真正执行删除（缺省只演习）")
    p.add_argument("--include-dh", action="store_true", help="包含 5.3G DH 库（必须先停服）")
    args = p.parse_args()

    sftp, t = connect()
    freed = 0
    try:
        for rel, mode, note in collect_targets(args.include_dh):
            try:
                rel = safe(rel)
            except ValueError as e:
                print(f"SKIP {e}")
                continue
            try:
                st = sftp.stat(rel)
            except IOError:
                print(f"SKIP 不存在: {rel}")
                continue

            # 统计 + 前置校验
            if mode == "tree":
                size, nfiles = dir_stats(sftp, rel)
            elif mode == "logs":
                size, nfiles, victims = 0, 0, []
                for a in sftp.listdir_attr(rel):
                    if not is_dir(a) and a.filename not in LOGS_KEEP:
                        size += a.st_size or 0
                        nfiles += 1
                        victims.append(a.filename)
            else:
                size, nfiles = st.st_size or 0, 1

            if mode == "file" and "architectury" in rel:
                a = md5(sftp, f"mods/{ARCH_A}")
                b = md5(sftp, rel)
                if a != b:
                    print(f"SKIP hash 不一致，不删: {rel}")
                    continue

            tag = "删" if args.execute else "将删"
            print(f"{tag} {rel}  [{mode}] {human(size)} / {nfiles} files  — {note}")
            if mode == "logs":
                print(f"    保留: {sorted(LOGS_KEEP)}")

            if not args.execute:
                freed += size
                continue

            if mode == "tree":
                rm_tree(sftp, rel)
            elif mode == "logs":
                for name in victims:
                    sftp.remove(f"{rel.rstrip('/')}/{name}")
            else:
                sftp.remove(rel)
            freed += size

        label = "已释放" if args.execute else "预计释放"
        print(f"\n{label}: {human(freed)}")
        if not args.execute:
            print("（dry-run，未动任何文件；加 --execute 执行）")
    finally:
        sftp.close()
        t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
