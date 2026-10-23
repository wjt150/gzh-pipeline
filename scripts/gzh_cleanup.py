#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理公众号草稿箱里的指定草稿。

用途：演练/测试留下的草稿，一条命令删掉，不用手动点悬停菜单。

用法：
    python scripts/gzh_cleanup.py                      # 只列出草稿箱现状（不删）
    python scripts/gzh_cleanup.py --drafts 100000013   # 删掉这一篇
    python scripts/gzh_cleanup.py --drafts 100000013,100000042 --yes

为什么长成这样（2026-10-19 实测）：
  * 草稿卡片是 `div.weui-desktop-card[data-appid="..."]`，**appmsgid 在 data-appid 上**。
    卡片里的链接全是 `javascript:void(0)`，靠 href 找不到目标。
  * 删除不是 `<a>删除</a>`——是**无文字的垃圾桶图标**，在
    `.weui-desktop-publish__opr a` 里排第一（第二是编辑，第三是发表）。必须先 hover 卡片才会出现。
  * 点完垃圾桶弹出的是**浮层**（不是 dialog）：「确定删除？… 删除 / 取消」。
    确认按钮是文字为「删除」的最后一个可见元素。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOKEN_RE = re.compile(r"[?&]token=(\d+)")
HOME_URL = "https://mp.weixin.qq.com/"
AB_ARGS = "--no-sandbox,--do-not-de-elevate"


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def workroot() -> Path:
    """定位工作区根（可移植版）。

    优先级：环境变量 GZH_WORKROOT → 往上找带标记文件的祖先 → 当前目录 → 脚本上两级。
    标记文件用 3 个任选其一，这样换电脑/换目录名都不用改代码。
    """
    env = os.environ.get("GZH_WORKROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    markers = ("来源清单.md", "排版规范.md", "组件库.md")
    here = Path(__file__).resolve()
    for base in (Path.cwd().resolve(), *here.parents):
        if any((base / m).exists() for m in markers):
            return base
    return here.parents[1] if len(here.parents) > 1 else Path.cwd()


def find_ab(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("agent-browser", "agent-browser.cmd", "agent-browser.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    raise SystemExit("找不到 agent-browser")


def run_batch(ab: str, profile: Path, commands: list[list[str]], timeout: int = 200) -> str:
    payload = json.dumps(commands, ensure_ascii=False)
    cmd = [ab, "--args", AB_ARGS, "--profile", str(profile), "--max-output", "6000", "batch"]
    fd, logname = tempfile.mkstemp(prefix="gzh-clean-", suffix=".log")
    try:
        with os.fdopen(fd, "wb") as logf:
            try:
                subprocess.run(
                    cmd, input=payload.encode("utf-8"), stdout=logf, stderr=subprocess.STDOUT, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                raise SystemExit(f"agent-browser batch 超时（{timeout}s）")
        return Path(logname).read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            os.unlink(logname)
        except OSError:
            pass


def get_token(ab: str, profile: Path) -> str:
    out = run_batch(ab, profile, [["open", HOME_URL], ["wait", "4000"], ["eval", "location.href"]], 120)
    m = TOKEN_RE.search(out)
    if m:
        return m.group(1)
    raise SystemExit(
        "登录态已失效（后台停在登录页）。\n"
        f'请重新扫码：agent-browser --args "{AB_ARGS}" --profile "{profile}" --headed open {HOME_URL}'
    )


def list_url(token: str) -> str:
    return (
        "https://mp.weixin.qq.com/cgi-bin/appmsg?begin=0&count=10&type=77"
        f"&action=list_card&token={token}&lang=zh_CN"
    )


LIST_JS = (
    "var cs=[...document.querySelectorAll('.weui-desktop-card')];"
    "'ITEMS='+cs.map(function(c){"
    "return c.getAttribute('data-appid')+'~'+(c.innerText||'').replace(/\\s+/g,' ').slice(0,28);"
    "}).join(' || ')"
)


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="删除公众号草稿（默认只列出）")
    ap.add_argument("--drafts", default="", help="要删的 appmsgid，逗号分隔")
    ap.add_argument("--yes", action="store_true", help="确认执行删除（不加则只列出）")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--ab", default=None)
    args = ap.parse_args(argv)

    root = workroot()
    profile = Path(args.profile).resolve() if args.profile else root / ".cache" / "browser-profile"
    ab = find_ab(args.ab)
    token = get_token(ab, profile)

    targets = [t.strip() for t in args.drafts.split(",") if t.strip()]

    # 先列现状
    out = run_batch(
        ab,
        profile,
        [["open", list_url(token)], ["set", "viewport", "1440", "1200"], ["wait", "8000"], ["eval", LIST_JS]],
    )
    m = re.search(r"ITEMS=([^\n\"]*)", out)
    items = []
    if m and m.group(1).strip():
        for chunk in m.group(1).split("||"):
            cid, _, title = chunk.strip().partition("~")
            if cid:
                items.append({"appmsgid": cid, "title": title})

    print(json.dumps({"found": items, "token": token}, ensure_ascii=False, indent=2), file=sys.stderr)

    if not targets:
        print(json.dumps({"ok": True, "mode": "list-only", "found": items}, ensure_ascii=False, indent=2))
        return 0

    existing = {i["appmsgid"] for i in items}
    todo = [t for t in targets if t in existing]
    missing = [t for t in targets if t not in existing]

    if not args.yes:
        print(
            json.dumps(
                {"ok": True, "mode": "dry-run", "will_delete": todo, "not_found": missing},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    deleted: list[str] = []
    failed: list[str] = []
    for cid in todo:
        cmds = [
            ["open", list_url(token)],
            ["set", "viewport", "1440", "1200"],
            ["wait", "8000"],
            ["hover", f'.weui-desktop-card[data-appid="{cid}"]'],
            ["wait", "1500"],
            [
                "eval",
                f'var c=document.querySelector(\'.weui-desktop-card[data-appid="{cid}"]\');'
                "var as=c?[...c.querySelectorAll('.weui-desktop-publish__opr a')]:[];"
                "as.length?(as[0].id='__del','DEL-OK'):'NO-DEL-ICON'",
            ],
            ["click", "#__del"],
            ["wait", "3500"],
            [
                "eval",
                "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='删除'&&b.offsetParent;});"
                "bs.length?(bs[bs.length-1].id='__okdel','CONFIRM-OK'):'NO-CONFIRM'",
            ],
            ["click", "#__okdel"],
            ["wait", "6000"],
            ["eval", "'LEFT='+[...document.querySelectorAll('.weui-desktop-card')].map(function(c){return c.getAttribute('data-appid');}).join(',')"],
        ]
        res = run_batch(ab, profile, cmds)
        left = re.search(r"LEFT=([^\n\"]*)", res)
        left_ids = [x for x in (left.group(1).split(",") if left else []) if x]
        if cid not in left_ids:
            deleted.append(cid)
            print(f"已删除 {cid}", file=sys.stderr)
        else:
            failed.append(cid)
            print(f"删除失败 {cid}（现场：{res[-300:]}）", file=sys.stderr)

    print(
        json.dumps(
            {"ok": not failed, "deleted": deleted, "failed": failed, "not_found": missing},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
