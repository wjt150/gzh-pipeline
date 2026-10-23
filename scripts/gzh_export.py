#!/usr/bin/env python3
"""把整个产线打成「换电脑能接着用」的迁移包。

    python scripts/gzh_export.py                 # 打包到 dist/迁移-YYYYMMDD.zip
    python scripts/gzh_export.py --list          # 只看会打进去什么
    python scripts/gzh_export.py --no-sources    # 不带原文（体积小，但补不了旧稿的稿）

打进去的（可以搬走）：
    代码、文档、模板、技能本体
    稿件/            每篇的 manifest / body.html / cover / draft.json（当前状态）
    运行台账/        ledger.jsonl（唯一事实源）与 notice/
    雷达/pool.jsonl  选题池（含「已用/已回滚」状态）

不打的（搬不走 / 不该搬）：
    .cache/browser-profile   微信登录态——拷了也无效，新机器必须重扫
    .cache/ 其余缓存          随时可重建
    dist/、__pycache__、*.pyc 产物

换机后仍然要做的三件事：装 agent-browser 与 ffmpeg、扫一次码、跑 doctor。
"""
from __future__ import annotations

import argparse
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "dist"

DOCS = [
    "组件库.md",
    "排版规范.md",
    "写作规则.md",
    "来源清单.md",
    "分类方案.md",
    "Agent设计.md",
]
DIRS = ["稿件", "运行台账", "模板", "scripts", "雷达"]
SKIP_SUFFIX = {".pyc", ".pyo", ".tmp", ".log"}
SKIP_DIRS = {"__pycache__", ".cache", "dist", "node_modules", ".git"}


def collect(with_sources: bool) -> list[tuple[Path, str]]:
    """返回 [(绝对路径, 包内相对路径)]"""
    items: list[tuple[Path, str]] = []

    for name in DOCS:
        p = ROOT / name
        if p.exists():
            items.append((p, name))

    for d in DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            rel_parts = set(f.relative_to(ROOT).parts)
            if rel_parts & SKIP_DIRS:
                continue
            if f.suffix in SKIP_SUFFIX:
                continue
            # 雷达只带池子和看板所需，不带历史 HTML（体积大且可从池子重建）
            if d == "雷达" and f.suffix == ".html":
                continue
            # 原文体积大（每篇 40KB+），可选
            if not with_sources and d == "稿件" and f.suffix in {".md"} and f.name != "README.md":
                if (f.parent / "body.html").exists():
                    continue
            items.append((f, str(f.relative_to(ROOT).as_posix())))

    # 技能本体（含 references / 脚本副本）
    skill = ROOT / ".workbuddy" / "skills" / "gzh-publisher"
    if skill.exists():
        for f in sorted(skill.rglob("*")):
            if f.is_file() and f.suffix not in SKIP_SUFFIX:
                items.append((f, str(f.relative_to(ROOT).as_posix())))
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包迁移包")
    ap.add_argument("--list", action="store_true", help="只列出会打进去什么")
    ap.add_argument("--no-sources", action="store_true", help="不带原文 md")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    items = collect(with_sources=not args.no_sources)
    total = sum(p.stat().st_size for p, _ in items)

    if args.list:
        by_top: dict[str, int] = {}
        for p, rel in items:
            top = rel.split("/")[0]
            by_top[top] = by_top.get(top, 0) + 1
        print(f"共 {len(items)} 个文件，{total // 1024} KB")
        for k, v in sorted(by_top.items(), key=lambda x: -x[1]):
            print(f"  {k:<24} {v} 个")
        print("\n不打包：.cache/browser-profile（登录态，搬不走）、缓存、dist、__pycache__")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"迁移-{datetime.now():%Y%m%d}.zip"

    note = f"""# 迁移说明（包里自述）

生成时间：{datetime.now():%Y-%m-%d %H:%M}

## 这是什么
整条公众号产线的快照：代码、文档、稿件、台账、选题池。

## 到新电脑上怎么做
1. 解压到你想放的位置，作为新的「工作区根」
2. 装依赖：
   - Python ≥ 3.10
   - agent-browser：`npm install -g agent-browser && agent-browser install`
   - ffmpeg（封面裁剪/封面卡要用）
3. 自检：`python scripts/gzh_doctor.py`
4. **重新扫码登录**（登录态是拷不过来的）：
   `python scripts/gzh_doctor.py --login`
5. 确认队列：`python scripts/gzh_queue.py list --json`
6. 试发一篇：`python scripts/gzh_publish.py --from-queue --mode dry-run --plan-only`

## 注意
- `.cache/browser-profile` **不在包里**。cookie 绑定机器与微信号，必须用管理员微信重扫。
- 台账 `运行台账/ledger.jsonl` 是唯一事实源，别手改；`稿件/*/draft.json` 丢了可以用
  `python scripts/gzh_queue.py sync` 从台账重建。
"""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in items:
            z.write(p, rel)
        z.writestr("迁移说明.md", note)

    print(f"✅ 迁移包：{out}  {out.stat().st_size // 1024} KB  含 {len(items)} 个文件")
    print("   已内置「迁移说明.md」，新电脑上先读那一份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
