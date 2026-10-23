#!/usr/bin/env python3
"""把雷达三份产物里的元数据回填进 pool.jsonl（历史 108 条一次性补齐）。

    python scripts/backfill_pool.py --dry-run    # 只看报告，不写文件
    python scripts/backfill_pool.py              # 执行（自动留 .bak-YYYYMMDD）
    python scripts/backfill_pool.py --verify     # 只体检现状

为什么必须做：pool 里 78% 的条目没有 score、44% 连来源都没有、编辑星级只写在 HTML 里
从不入库、也没有「已用」台账。选稿器要靠这些字段打分，不补就是空转。

幂等：已有非空字段一律不覆盖，可以反复跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_store as ps  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "雷达" / "pool.jsonl"

HTML_FIELDS = ("display_title", "rating", "section", "tag", "track", "points", "member_count")
RAW_FIELDS = ("lang", "desc")
EVENT_FIELDS = ("aihot_score", "aihot_reason", "category")


def merge_row(row: dict, hmeta: dict | None, raw: dict | None, ev: dict | None) -> list[str]:
    """把命中的元数据填进 row，返回本次填入的字段名。只填空值。"""
    filled: list[str] = []
    if hmeta:
        for f in HTML_FIELDS:
            v = hmeta.get(f)
            if v in (None, "", []):
                continue
            if row.get(f) in (None, "", []):
                row[f] = v
                filled.append(f)
        if hmeta.get("rating_src") and not row.get("rating_src"):
            row["rating_src"] = hmeta["rating_src"]
    if raw:
        for f in RAW_FIELDS:
            if raw.get(f) and not row.get(f):
                row[f] = raw[f]
                filled.append(f)
    if ev:
        for f in EVENT_FIELDS:
            if ev.get(f) not in (None, "") and not row.get(f):
                row[f] = ev[f]
                filled.append(f)
        if ev.get("member_count") and row.get("member_count", 1) < ev["member_count"]:
            row["member_count"] = ev["member_count"]
            filled.append("member_count")
    # 台账字段补默认值（不覆盖已有的）
    for f, default in (
        ("status", "new"),
        ("picked_at", None),
        ("used_at", None),
        ("used_appmsgid", None),
        ("published_at", None),
        ("skip_reason", None),
    ):
        if f not in row:
            row[f] = default
    if "risk_flags" not in row:
        row["risk_flags"] = []
        row["risk_flags"] = []
    return filled


def report(rows: list[dict], label: str = "") -> None:
    n = len(rows)
    rating = Counter(r.get("rating") for r in rows if r.get("rating"))
    has_desc = sum(1 for r in rows if r.get("desc"))
    has_score = sum(1 for r in rows if r.get("aihot_score") or r.get("score"))
    has_lang = sum(1 for r in rows if r.get("lang"))
    has_points = sum(1 for r in rows if r.get("points"))
    status = Counter(r.get("status") for r in rows)
    print(f"{label}pool={n}  有 rating={sum(rating.values())}"
          f"（★3={rating.get(3,0)} ★2={rating.get(2,0)} ★1={rating.get(1,0)}）"
          f"  desc={has_desc}  score={has_score}  lang={has_lang}  points={has_points}")
    print(f"      status={dict(status)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="回填 pool.jsonl 的元数据")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    ap.add_argument("--verify", action="store_true", help="只体检现状")
    args = ap.parse_args(argv)

    rows = ps.load_pool(POOL)
    if not rows:
        print(f"池子为空或不存在：{POOL}", file=sys.stderr)
        return 1

    report(rows, "[回填前] ")
    if args.verify:
        return 0

    hmap = ps.scan_radar_html(ROOT / "雷达")
    rawmap = ps.collect_raw_meta(ROOT / ".cache")
    evmap = ps.collect_event_meta(ROOT / ".cache")
    print(f"\n索引：HTML 卡片 {len(hmap)} / raw {len(rawmap)} / events {len(evmap)}")

    def pick(mapping: dict[str, dict], links: list[str]) -> dict | None:
        for u in links:
            for cand in (u, ps.normalize_url(u)):
                if cand in mapping:
                    return mapping[cand]
        return None

    filled_counter: Counter = Counter()
    matched = Counter()
    for row in rows:
        links = ps.row_links(row)
        norm = [ps.normalize_url(u) for u in links]
        hmeta = pick(hmap, links + norm)
        raw = pick(rawmap, links + norm)
        ev = pick(evmap, links + norm)
        matched["html"] += bool(hmeta)
        matched["raw"] += bool(raw)
        matched["event"] += bool(ev)
        for f in merge_row(row, hmeta, raw, ev):
            filled_counter[f] += 1

    print(f"命中：HTML {matched['html']} / raw {matched['raw']} / events {matched['event']}")
    print(f"本次填入：{dict(filled_counter)}")
    print()
    report(rows, "[回填后] ")

    if args.dry_run:
        print("\n--dry-run：未写文件。")
        return 0

    bak = ps.save_pool(POOL, rows)
    print(f"\n✅ 已写回 {POOL}" + (f"（备份 {bak.name}）" if bak else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
