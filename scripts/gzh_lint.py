#!/usr/bin/env python3
"""正文内容级检查（结构之外的那些「读起来对不对」）。

    python scripts/gzh_lint.py --manifest 稿件/x/manifest.json --form A
    python scripts/gzh_lint.py --manifest x.json --form B --json

和 `gzh_draft.py --check-only` 的分工：
    check-only  只查标签合法性（section/span/strong/br/img）
    lint        查内容规格：字数对不对、加粗多不多、有没有导语和来源行、卡片有没有超标

退出码：0 通过 / 2 不通过 / 1 参数错误
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gzh_draft import scan_body  # noqa: E402

FORM_RANGE = {
    "A": (600, 800),
    "B": (1000, 1600),
    "C": (1000, 1400),
    "D": (800, 1200),
    "E": (1500, 2000),
}
TAG_STRIP = re.compile(r"<[^>]+>")


def blocks_of(html: str) -> list[str]:
    return scan_body(html)["blocks"]


def text_of(block: str) -> str:
    return re.sub(r"\s+", "", TAG_STRIP.sub("", block))


def has_card(block: str) -> bool:
    return "inline-block" in block


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="正文内容级 lint")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--form", default=None, choices=list(FORM_RANGE), help="形态；不给则只做通用检查")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    mp = Path(args.manifest).resolve()
    if not mp.exists():
        print(f"manifest 不存在：{mp}", file=sys.stderr)
        return 1
    data = json.loads(mp.read_text(encoding="utf-8"))
    body_path = (mp.parent / data["body_html_path"]).resolve() if data.get("body_html_path") else None
    if not body_path or not body_path.exists():
        print("manifest 缺 body_html_path 或文件不存在", file=sys.stderr)
        return 1
    html = body_path.read_text(encoding="utf-8").strip()

    scan = scan_body(html)
    blocks = scan["blocks"]
    stats = scan["stats"]
    errors: list[str] = [f"[标签] L{p['line']} {p['msg']}" for p in scan["errors"]]
    warnings: list[str] = [f"[标签] L{p['line']} {p['msg']}" for p in scan["warnings"]]

    chars = stats.get("chars", 0)
    strong = stats.get("strong", 0)

    # 形态字数（允许 0.9–1.15 倍浮动）
    if args.form:
        lo, hi = FORM_RANGE[args.form]
        if not (lo * 0.9 <= chars <= hi * 1.15):
            errors.append(f"[字数] {chars} 不在形态 {args.form} 的 {lo}–{hi}（容差后 {int(lo*0.9)}–{int(hi*1.15)}）")

    if not (6 <= strong <= 10):
        errors.append(f"[加粗] {strong} 处，应为 6–10 处")

    if len(blocks) < 4:
        errors.append(f"[结构] 只有 {len(blocks)} 块，太短（至少 4 块）")

    # 导语 C01：首块有左边框
    if blocks and "border-left:3px solid #1a4d8f" not in blocks[0]:
        errors.append("[结构] 首块不是导语（缺 border-left:3px solid #1a4d8f）")

    # 来源行 C07
    if not blocks or "信息来源" not in text_of(blocks[-1]):
        errors.append("[结构] 末块不是来源行（缺「信息来源：」）")

    # 卡片超量
    card_blocks = [i for i, b in enumerate(blocks) if has_card(b)]
    if len(card_blocks) > 3:
        errors.append(f"[卡片] 含 inline-block 的块有 {len(card_blocks)} 处，卡片类组件各自最多 1 处（总数应 ≤3）")
    # 卡片连排
    for a, b in zip(card_blocks, card_blocks[1:]):
        if b - a == 1:
            errors.append("[卡片] 第 %d、%d 块卡片连排，中间必须隔正文" % (a + 1, b + 1))

    # 段落过长 / 连续纯文本过多
    run = 0
    for i, b in enumerate(blocks):
        t = text_of(b)
        if len(t) > 90 and not has_card(b):
            warnings.append(f"[段长] 第 {i+1} 块 {len(t)} 字，超过 90 字")
        if not has_card(b) and "font-size:18px" not in b and "border-left" not in b and "信息来源" not in t:
            run += 1
            if run == 4:
                warnings.append(f"[节奏] 第 {i-2}–{i+1} 块连续 4 段纯正文，建议插小标题或卡片")
        else:
            run = 0

    # 标题 / 摘要
    title = data.get("title") or ""
    if len(title) > 64:
        warnings.append(f"[标题] {len(title)} 字，超过 64")
    summary = data.get("summary") or ""
    if summary and not (60 <= len(summary) <= 120):
        warnings.append(f"[摘要] {len(summary)} 字，应为 60–120")

    report = {
        "manifest": str(mp),
        "form": args.form,
        "chars": chars,
        "strong": strong,
        "blocks": len(blocks),
        "cards": len(card_blocks),
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{mp.name}  形态={args.form or '-'}  字数={chars}  加粗={strong}  块={len(blocks)}  卡片={len(card_blocks)}")
        for e in errors:
            print(f"  ❌ {e}")
        for w in warnings:
            print(f"  ⚠️  {w}")
        print("结论：" + ("✅ 通过" if not errors else f"❌ {len(errors)} 处不通过"))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
