#!/usr/bin/env python3
"""一键发送：从稿件队列取下一篇 → 推到二维码 → 等扫码 → 写台账 → 报结果。

    python scripts/gzh_send.py                  # 发队列里的下一篇（推荐，默认 300 秒等扫码）
    python scripts/gzh_send.py --wait-scan 600  # 多等一会儿
    python scripts/gzh_send.py --dry-run        # 只看会发哪篇，不点
    python scripts/gzh_send.py --list           # 看队列里排着什么

为什么还要一个包装：`gzh_publish --from-queue` 已经能干这活，但它有十几个参数，
日常只想「按一下」。这里把它固定成一条命令，并把结果翻译成人话。

三条硬约束（脚本会替你挡住）：
  · 一天只发 1 篇（平台群发配额 1 次/天）
  · 每篇发表都要人扫一次码（平台限制，无法绕过）
  · 今天发过就直接返回，不会换一篇再发
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gzh_draft import workroot  # noqa: E402

ROOT = workroot()


def run(args: list[str], timeout: int) -> tuple[int, str, str]:
    import os

    # 本脚本不加锁，让 gzh_publish 自己抢；显式去掉透传，避免误以为已有锁
    e = dict(os.environ)
    e.pop("GZH_LOCK_HELD", None)
    p = subprocess.run(
        [sys.executable, str(HERE / "gzh_publish.py"), *args],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", env=e,
    )
    return p.returncode, p.stdout or "", p.stderr or ""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    wait = 300
    if "--wait-scan" in args:
        i = args.index("--wait-scan")
        wait = int(args[i + 1])

    if "--list" in args:
        return subprocess.call([sys.executable, str(HERE / "gzh_queue.py"), "list"])

    cmd = ["--from-queue", "--mode", "dry-run" if dry else "qr-wait",
           "--wait-scan", str(wait), "--headed"]
    print("→ 准备发送（会开一个浏览器窗口，二维码在上面）", file=sys.stderr)
    code, out, err = run(cmd, wait + 260)

    # 把脚本的结构化结果翻译成人话
    import json

    data = {}
    try:
        data = json.loads(out[out.index("{"):])
    except Exception:
        pass

    if code == 0 and data.get("published"):
        print(f"\n✅ 已发表：{data.get('appmsgid')}")
        print(f"   核对：{data.get('evidence')}")
        md = ROOT / "运行台账" / "notice"
        latest = sorted(md.glob("*.md"))[-1] if md.exists() else None
        if latest:
            print(f"   记录：{latest}")
        return 0

    if code == 8:
        print("\n⏳ 码过期了（没人扫），草稿还在，重跑本命令即可。", file=sys.stderr)
        return 8
    if code == 4:
        print("\n· 今天已经发过了，不重复发。", file=sys.stderr)
        return 4
    if code == 2:
        print("\n· 队列里没有可发的稿。先写稿：python scripts/gzh_prep.py", file=sys.stderr)
        return 2
    if code == 5:
        print("\n⚠ 登录态失效。重扫：python scripts/gzh_doctor.py --login", file=sys.stderr)
        return 5

    print(f"\n✗ 失败（exit={code}）", file=sys.stderr)
    print((err or out)[-800:], file=sys.stderr)
    return code or 1


if __name__ == "__main__":
    sys.exit(main())
