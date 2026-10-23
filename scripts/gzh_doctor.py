#!/usr/bin/env python3
"""换电脑/新环境先跑这个：一条命令查清能不能用。

用法：
    python scripts/gzh_doctor.py           # 只做静态检查（不联网、不开浏览器，1 秒）
    python scripts/gzh_doctor.py --login   # 额外验证登录态（会开浏览器，约 20 秒）

返回码：0 全绿 / 1 有阻塞项 / 2 只有警告。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))

OK, WARN, FAIL = "✅", "⚠️ ", "❌"


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str, str]] = []  # level, name, detail, fix

    def add(self, level: str, name: str, detail: str, fix: str = "") -> None:
        self.items.append((level, name, detail, fix))

    def ok(self, *a) -> None:
        self.add(OK, *a)

    def warn(self, *a) -> None:
        self.add(WARN, *a)

    def fail(self, *a) -> None:
        self.add(FAIL, *a)

    def render(self) -> int:
        for level, name, detail, _ in self.items:
            print(f"{level} {name:<22} {detail}")
        fixes = [(n, f) for lv, n, _, f in self.items if f and lv != OK]
        if fixes:
            print("\n怎么修：")
            for name, fix in fixes:
                print(f"  [{name}] {fix}")
        fails = sum(1 for lv, *_ in self.items if lv == FAIL)
        warns = sum(1 for lv, *_ in self.items if lv == WARN)
        print("\n" + "-" * 56)
        print(f"阻塞 {fails} 项 / 警告 {warns} 项")
        if fails:
            print("结论：还不能用，先处理上面的阻塞项。")
            return 1
        if warns:
            print("结论：能跑，但有警告。")
            return 2
        print("结论：全绿，可以跑。")
        return 0


def find_ab() -> str | None:
    for name in ("agent-browser", "agent-browser.cmd", "agent-browser.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公众号产线环境自检")
    ap.add_argument("--login", action="store_true", help="额外验证登录态（会开浏览器）")
    ap.add_argument("--profile", default=None, help="登录态目录，默认 <工作区>/.cache/browser-profile")
    args = ap.parse_args(argv)

    rep = Report()

    # 1 Python
    v = sys.version_info
    if v >= (3, 10):
        rep.ok("Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        rep.fail("Python", f"{v.major}.{v.minor}（需要 ≥3.10）", "装 Python 3.10+ 后重跑")

    # 2 工作区结构
    try:
        import gzh_draft  # noqa: F401

        root = gzh_draft.workroot()
        rep.ok("工作区根", str(root))
    except Exception as exc:  # noqa: BLE001
        rep.fail("工作区根", f"导入 gzh_draft 失败：{exc}", "确认 scripts/ 完整，或设 GZH_WORKROOT")
        print()
        return rep.render()

    # 文档与脚本：优先工作区根，其次技能自带（references/ 与 scripts/ 就在技能目录里）
    skill_dir = HERE.parent.parent
    must = {
        "组件库.md": "组件片段唯一事实源",
        "排版规范.md": "硬纪律与自检",
        "模板/template.body.html": "样板",
    }

    def locate(rel: str) -> Path | None:
        name = Path(rel).name
        candidates = [
            root / rel,
            root / "references" / name,
            skill_dir / rel,
            skill_dir / "references" / name,
            skill_dir / "assets" / rel,
            skill_dir / "assets" / "模板" / name,
        ]
        for cand in candidates:
            if cand.exists():
                return cand
        return None

    missing = [n for n in must if locate(n) is None]
    if missing:
        rep.fail("文档", f"缺 {', '.join(missing)}", "重新解压技能包，或从 references/ 复制到工作区根")
    else:
        where = "工作区" if all((root / n).exists() for n in must) else "技能包内置"
        rep.ok("文档", f"{len(must)} 份齐全（{where}）")
    for sub in ("scripts", ".cache"):
        p = root / sub
        if p.exists():
            rep.ok(f"目录 {sub}/", "存在")
        else:
            (p.mkdir(parents=True, exist_ok=True))
            rep.warn(f"目录 {sub}/", "刚创建（空的）")

    # 3 脚本齐全（工作区或技能自带都算）
    want = ["gzh_source", "gzh_cover", "gzh_draft", "gzh_publish", "gzh_auto", "gzh_cleanup"]
    lack = [
        w
        for w in want
        if not (root / "scripts" / f"{w}.py").exists()
        and not (skill_dir / "scripts" / f"{w}.py").exists()
    ]
    if lack:
        rep.fail("脚本", f"缺 {', '.join(lack)}", "重新解压技能包，或从 scripts/ 补回工作区")
    else:
        rep.ok("脚本", f"{len(want)} 个齐全")

    # 4 agent-browser
    ab = find_ab()
    if not ab:
        rep.fail(
            "agent-browser",
            "不在 PATH 里",
            "npm install -g agent-browser && agent-browser install",
        )
    else:
        try:
            r = subprocess.run([ab, "--version"], capture_output=True, text=True, timeout=60)
            ver = (r.stdout or r.stderr).strip().splitlines()[0][:40]
            rep.ok("agent-browser", f"{ver}  ({ab})")
        except Exception:  # noqa: BLE001
            rep.warn("agent-browser", f"找得到但跑 --version 失败：{ab}", "手动执行一次确认能启动")

    # 5 登录态目录
    profile = (
        Path(args.profile).expanduser().resolve()
        if args.profile
        else root / ".cache" / "browser-profile"
    )
    if profile.exists() and any(profile.iterdir()):
        rep.ok("登录态目录", str(profile))
    else:
        rep.warn(
            "登录态目录",
            "空或不存在（首次使用正常）",
            "首次跑 gzh_draft.py 会开窗口，扫码登录一次即可",
        )

    # 6 写权限（agent-browser 要写临时文件）
    try:
        with tempfile.NamedTemporaryFile(delete=True) as fh:
            fh.write(b"ok")
        rep.ok("临时目录可写", tempfile.gettempdir())
    except Exception as exc:  # noqa: BLE001
        rep.fail("临时目录可写", str(exc), "检查 TEMP/TMP 环境变量")

    # 7 封面工具（可选）
    cover_tool = Path.home() / ".workbuddy/skills/wechat-mp-typeset/scripts/cover_check.py"
    if cover_tool.exists():
        rep.ok("封面校验工具", "可用")
    else:
        rep.warn("封面校验工具", "未安装（头条封面需 2.35:1）", "装 skill「公众号排版技能」即可")

    # 8 可选：验证登录态
    if args.login:
        if not ab:
            rep.fail("登录态", "跳过（没有 agent-browser）")
        else:
            try:
                import gzh_draft

                token = gzh_draft.get_token(ab, profile)
                rep.ok("登录态", f"有效，token={token}")
            except SystemExit as exc:
                rep.fail("登录态", str(exc), "打开窗口扫码登录一次后重跑")
            except Exception as exc:  # noqa: BLE001
                rep.fail("登录态", str(exc)[:60], "重跑一次，或删掉 .cache/browser-profile 重新登录")
    else:
        rep.warn("登录态", "未验证（加 --login 可以验）", "python scripts/gzh_doctor.py --login")

    print()
    return rep.render()


if __name__ == "__main__":
    sys.exit(main())
