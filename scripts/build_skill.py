#!/usr/bin/env python3
"""把技能打成「拷到别的电脑直接能用」的 zip。

    python scripts/build_skill.py            # 打包
    python scripts/build_skill.py --check    # 只校验不打包

打包内容（缺一不可，之前漏了文档导致换机后技能哑火）：
    SKILL.md / 安装说明.md
    references/  实测约束.md 组件库.md 排版规范.md 写作规则.md
    assets/模板/  template.body.html
    scripts/     gzh_*.py + radar_*.py + test_body_scan.py

产物：dist/gzh-publisher-skill.zip（顶层目录 gzh-publisher/）
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".workbuddy" / "skills" / "gzh-publisher"
STAGE = ROOT / "dist" / "gzh-publisher"
OUT = ROOT / "dist" / "gzh-publisher-skill.zip"

DOCS = ["实测约束.md"]
ROOT_DOCS = ["组件库.md", "排版规范.md", "写作规则.md"]
TEMPLATES = ["template.body.html", "写稿任务.md", "每日任务.md"]
SCRIPTS = [
    "gzh_doctor.py",
    "gzh_state.py",
    "gzh_queue.py",
    "gzh_export.py",
    "gzh_prep.py",
    "gzh_covercard.py",
    "gzh_lint.py",
    "gzh_lock.py",
    "pick_one.py",
    "pool_store.py",
    "backfill_pool.py",
    "gzh_source.py",
    "gzh_cover.py",
    "gzh_draft.py",
    "gzh_publish.py",
    "gzh_auto.py",
    "gzh_cleanup.py",
    "radar_enrich.py",
    "radar_server.py",
    "test_body_scan.py",
]


def check() -> list[str]:
    """返回缺失清单，空表示齐全。"""
    missing: list[str] = []
    if not (SKILL / "SKILL.md").exists():
        missing.append("SKILL.md")
    for d in DOCS:
        if not (SKILL / "references" / d).exists():
            missing.append(f"references/{d}")
    for d in ROOT_DOCS:
        if not (ROOT / d).exists():
            missing.append(d)
    for t in TEMPLATES:
        if not (ROOT / "模板" / t).exists():
            missing.append(f"模板/{t}")
    for s in SCRIPTS:
        if not (ROOT / "scripts" / s).exists():
            missing.append(f"scripts/{s}")
    return missing


def build() -> Path:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    (STAGE / "references").mkdir(parents=True)
    (STAGE / "assets" / "模板").mkdir(parents=True)
    (STAGE / "scripts").mkdir(parents=True)

    # 技能自带的（SKILL.md、实测约束、安装说明）
    shutil.copy2(SKILL / "SKILL.md", STAGE / "SKILL.md")
    for name in ("安装说明.md", "references/实测约束.md"):
        src = SKILL / name
        if src.exists():
            shutil.copy2(src, STAGE / name)

    # 工作区根的三份文档：源在工作区，打包时同步进来（改完重跑本脚本即可）
    for d in ROOT_DOCS:
        shutil.copy2(ROOT / d, STAGE / "references" / d)

    # 模板
    for t in TEMPLATES:
        shutil.copy2(ROOT / "模板" / t, STAGE / "assets" / "模板" / t)

    # 脚本
    for s in SCRIPTS:
        src = ROOT / "scripts" / s
        if src.exists():
            shutil.copy2(src, STAGE / "scripts" / s)

    if OUT.exists():
        OUT.unlink()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(STAGE.rglob("*")):
            if f.is_file() and f.suffix != ".pyc":
                z.write(f, f.relative_to(STAGE.parent))
    return OUT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包 gzh-publisher 技能（可移植）")
    ap.add_argument("--check", action="store_true", help="只校验不打包")
    args = ap.parse_args(argv)

    missing = check()
    if missing:
        print("❌ 打包会缺内容：", file=sys.stderr)
        for m in missing:
            print(f"   - {m}", file=sys.stderr)
        return 1
    if args.check:
        print("✅ 内容齐全，可以打包")
        return 0

    out = build()
    names = zipfile.ZipFile(out).namelist()
    print(f"✅ 打包完成：{out.relative_to(ROOT)}  {out.stat().st_size // 1024} KB  {len(names)} 个文件")
    for n in names:
        print("   ", n)
    print("\n拷到别的电脑后：解压 → 放进 <项目>/.workbuddy/skills/ → python scripts/gzh_doctor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
