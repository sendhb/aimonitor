#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台进程文件锁（flock 替代，TASK-012）。

在 Git Bash/MSYS 上替换 util-linux flock：用 Python 标准库实现同语义的
非阻塞排他锁，供 autoloop / autoloop-coder / autoloop-reviewer 使用。
  - Windows: msvcrt.locking（底层 LockFileEx，字节区间锁）
  - POSIX : fcntl.flock（与 util-linux flock 同为 flock(2)）

子命令:
  probe <lockfile>          锁空闲 → exit 0；被占用 → exit 1（等价: flock -n <f> true）
  hold <lockfile> -- CMD... 非阻塞拿锁；拿不到 → exit 1（stderr 提示）；
                            拿到 → 以持锁状态运行 CMD，透传其退出码
                            （等价: exec 9>f; flock -n 9 ... 持锁覆盖运行期）

说明: hold 由本进程持锁并 spawn CMD 子进程；子进程退出后释放锁。这满足
autoloop-* 的"锁覆盖整个运行期"语义（TASK-008/032）。进程被 kill 时 OS 自动释放。
"""
import errno
import os
import shutil
import subprocess
import sys

_LOCK_BUSY = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)


def _acquire(fd):
    """非阻塞拿排他锁；拿到 True，被占用 False，其它错误上抛。"""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in _LOCK_BUSY:
                return False
            raise
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in _LOCK_BUSY:
            return False
        raise


def _release(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def _open_lock(lockfile):
    return os.open(lockfile, os.O_CREAT | os.O_RDWR, 0o644)


def _usage(err=None):
    if err:
        print("✗ %s" % err, file=sys.stderr)
    print("用法: lock.py probe <lockfile> | lock.py hold <lockfile> -- CMD...", file=sys.stderr)
    return 2


def _resolve_cmd(argv):
    """首参做 PATH 解析（Windows 上 bash 等需定位 .exe）。"""
    head = shutil.which(argv[0]) or argv[0]
    return [head] + list(argv[1:])


def main(argv):
    if len(argv) < 2 or argv[0] not in ("probe", "hold"):
        return _usage()
    cmd, lockfile = argv[0], argv[1]
    try:
        fd = _open_lock(lockfile)
    except OSError as e:
        print("✗ 无法打开锁文件 %s: %s" % (lockfile, e), file=sys.stderr)
        return 1

    if cmd == "probe":
        held = not _acquire(fd)
        if not held:
            _release(fd)
        os.close(fd)
        return 1 if held else 0

    if len(argv) < 3 or argv[2] != "--":
        os.close(fd)
        return _usage()
    if not _acquire(fd):
        os.close(fd)
        print("✗ 锁已被占用: %s（拒绝启动，避免重复进程）" % lockfile, file=sys.stderr)
        return 1
    try:
        rc = subprocess.call(_resolve_cmd(argv[3:]))
        return rc if rc is not None else 1
    finally:
        _release(fd)
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
