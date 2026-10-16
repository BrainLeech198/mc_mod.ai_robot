#!/usr/bin/env python3
"""简幻欢 SFTP 服务器文件管理工具（paramiko）。

只读侦查命令：ls / tree / get / du；put（上传）/ mv（改名）仅在用户明确授权后使用。
删除不提供。凭据读自 version/1.20.1-forge/config.json 的 sftp 字段（该文件已 gitignore）。
"""
import argparse
import json
import sys
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parent.parent


def cfg():
    return json.loads((REPO / "version" / "1.20.1-forge" / "config.json").read_text(encoding="utf-8"))["sftp"]


def connect():
    c = cfg()
    t = paramiko.Transport((c["host"], int(c["port"])))
    t.connect(username=c["username"], password=c["password"])
    return paramiko.SFTPClient.from_transport(t), t


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}T"


def is_dir(attr):
    return bool(attr.st_mode and attr.st_mode & 0o040000)


def ls(sftp, path):
    out = []
    for e in sftp.listdir_attr(path):
        out.append((e.filename, is_dir(e), e.st_size or 0))
    out.sort(key=lambda x: (not x[1], x[0].lower()))
    return out


def tree(sftp, path, depth, limit, prefix=""):
    entries = ls(sftp, path)
    shown = 0
    for name, isdir, size in entries:
        if shown >= limit:
            print(f"{prefix}... ({len(entries) - shown} more)")
            break
        tag = "/" if isdir else ""
        print(f"{prefix}{name}{tag}  {human(size) if not isdir else ''}")
        shown += 1
        if isdir and depth > 0:
            tree(sftp, f"{path.rstrip('/')}/{name}", depth - 1, limit, prefix + "  ")


def du(sftp, path):
    total, count = 0, 0
    for attr in sftp.listdir_attr(path):
        child = f"{path.rstrip('/')}/{attr.filename}"
        if is_dir(attr):
            t, c = du(sftp, child)
            total += t
            count += c
        else:
            total += attr.st_size or 0
            count += 1
    return total, count


def main(argv=None):
    p = argparse.ArgumentParser(prog="sftp_client")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("ls", help="列目录（一层）").add_argument("path", nargs="?", default=".")
    tp = sub.add_parser("tree", help="递归列目录")
    tp.add_argument("path", nargs="?", default=".")
    tp.add_argument("--depth", type=int, default=2)
    tp.add_argument("--limit", type=int, default=100)
    gp = sub.add_parser("get", help="下载远程文件到本地")
    gp.add_argument("remote")
    gp.add_argument("local")
    dp = sub.add_parser("du", help="递归统计目录大小")
    dp.add_argument("path", nargs="?", default=".")
    pp = sub.add_parser("put", help="上传本地文件到服务器")
    pp.add_argument("local")
    pp.add_argument("remote")
    mv = sub.add_parser("mv", help="远程文件改名/移动（同名目标默认拒绝，需 --force）")
    mv.add_argument("src")
    mv.add_argument("dst")
    mv.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    args = p.parse_args(argv)

    sftp, t = connect()
    try:
        if args.action == "ls":
            for name, isdir, size in ls(sftp, args.path):
                print(f"{name}{'/' if isdir else ''}  {human(size)}")
        elif args.action == "tree":
            tree(sftp, args.path, args.depth, args.limit)
        elif args.action == "get":
            sftp.get(args.remote, args.local)
            print(f"downloaded {args.remote} -> {args.local}")
        elif args.action == "du":
            total, count = du(sftp, args.path)
            print(f"{args.path}: {human(total)} ({count} files)")
        elif args.action == "put":
            sftp.put(args.local, args.remote)
            print(f"uploaded {args.local} -> {args.remote}")
        elif args.action == "mv":
            exists = True
            try:
                sftp.stat(args.dst)
            except IOError:
                exists = False
            if exists and not args.force:
                print(f"拒绝：目标已存在 {args.dst}（加 --force 才覆盖）")
                return 1
            sftp.rename(args.src, args.dst)
            print(f"renamed {args.src} -> {args.dst}")
    finally:
        sftp.close()
        t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
