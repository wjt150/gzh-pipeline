#!/usr/bin/env python3
"""跨平台互斥锁：O_CREAT|O_EXCL 原子创建 + 陈旧锁自动回收。

为什么不用 msvcrt.locking / fcntl：
- Windows 的 msvcrt 锁的是文件句柄区间，进程崩溃后句柄释放但文件残留，语义更绕；
- POSIX 的 fcntl 在 Windows 上不存在。
O_EXCL 创建 + 时间戳判定在两种系统上行为一致，且 `.lock` 里是人能看懂的 JSON。

用法：
    ok, info = acquire(root)
    if not ok:
        return 10
    try:
        ...
    finally:
        release(info)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 最长单次运行（含 120s 扫码等待）约 8 分钟，40 分钟给足 3 倍余量
DEFAULT_STALE = 40 * 60


def lock_path(root: Path, name: str = "gzh") -> Path:
    return root / ".cache" / f"{name}.lock"


def acquire(root: Path, name: str = "gzh", stale: int = DEFAULT_STALE) -> tuple[bool, str]:
    """拿到锁返回 (True, 锁文件路径)；被占用返回 (False, 原因)。"""
    p = lock_path(root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "at": time.time(),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "argv": " ".join(sys.argv)[:300],
        },
        ensure_ascii=False,
    )
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, payload.encode("utf-8"))
            os.close(fd)
            return True, str(p)
        except FileExistsError:
            try:
                info = json.loads(p.read_text(encoding="utf-8"))
                age = time.time() - float(info.get("at", 0))
                who = f"pid={info.get('pid')}，{int(age)} 秒前启动"
            except Exception:
                age = stale + 1
                who = "内容损坏"
            if age < stale:
                return False, f"另一实例在跑（{who}）"
            try:
                p.unlink()  # 陈旧锁回收
            except OSError:
                return False, f"陈旧锁删不掉，请手动删除 {p}"
    return False, "抢锁失败"


def release(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def is_locked(root: Path, name: str = "gzh") -> bool:
    return lock_path(root, name).exists()
