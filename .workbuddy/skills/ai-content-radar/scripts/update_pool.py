#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选题池入库 —— 把一次雷达运行的结果并进长期池子。

用法：
    # 从聚簇结果入库（新链路）
    python update_pool.py --events ../../.cache/radar-2026-10-19-events.json \
                          --date 2026-10-19 --issue 3

    # 从已出的雷达 HTML 回填（历史期补录）
    python update_pool.py --html ../../雷达/2026-10-16.html --date 2026-10-16 --issue 1

池子文件：`<工作区根>/雷达/pool.jsonl`，一行一个事件，**只增不删**。
合并规则（宁可分开，不可并错，与聚簇脚本同一取向）：
  1. 链接相同 → 同一条；
  2. 标题归一化后完全相同 → 同一条；
  3. 其余一律新建。跨期换措辞的同一事件不会自动并，留给人工。

为什么要有池子：
  单期雷达只能回答「今天有什么」，池子能回答两个单期答不了的问题——
  **一件事被连续几天报道（真热点）**，以及**哪条候选放了三天还没人写（该扔了）**。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
KEY_RE = re.compile(r"[^\w\u4e00-\u9fff]+")
TAG_RE = re.compile(r"<[^>]+>")


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def title_key(title: str) -> str:
    return KEY_RE.sub("", title or "").lower()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", text or "")).strip()


def load_pool(path: Path) -> tuple[list[dict], dict[str, dict]]:
    items: list[dict] = []
    index: dict[str, dict] = {}
    if not path.exists():
        return items, index
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        items.append(row)
        if row.get("key"):
            index[row["key"]] = row
        for link in row.get("links") or []:
            if link.get("url"):
                index["url:" + link["url"]] = row
    return items, index


def rows_from_events(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for ev in data.get("events") or []:
        links = []
        sources = []
        for m in ev.get("members") or []:
            if m.get("link"):
                links.append(
                    {
                        "url": m["link"],
                        "source": m.get("source", ""),
                        "title": m.get("title", ""),
                    }
                )
            if m.get("source"):
                sources.append(m["source"])
        rows.append(
            {
                "title": ev.get("title", ""),
                "links": links,
                "sources": sources,
                "score": ev.get("aihot_score"),
                "category": ev.get("category"),
                "member_count": ev.get("member_count", 1),
                "published": ev.get("newest_published"),
            }
        )
    return rows


def rows_from_html(path: Path) -> list[dict]:
    """从已出的雷达页回填：抓 h3 卡片与 .one 待观察行。"""
    text = path.read_text(encoding="utf-8")
    rows: list[dict] = []

    for block in re.findall(r"<h3[^>]*>(.*?)</h3>", text, re.S):
        m = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        rows.append(
            {
                "title": clean(m.group(2)),
                "links": [{"url": m.group(1), "source": "", "title": clean(m.group(2))}],
                "sources": [],
                "score": None,
                "category": None,
                "member_count": 1,
                "published": None,
            }
        )

    for block in re.findall(r'<p class="one">(.*?)</p>', text, re.S):
        m = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        src = re.search(r"[（(]([^（）()]{2,12})[）)]", clean(block))
        rows.append(
            {
                "title": clean(m.group(2)),
                "links": [{"url": m.group(1), "source": "", "title": clean(m.group(2))}],
                "sources": [src.group(1)] if src else [],
                "score": None,
                "category": None,
                "member_count": 1,
                "published": None,
            }
        )
    return rows


def merge_into_pool(
    pool: list[dict],
    index: dict[str, dict],
    rows: list[dict],
    date: str,
    issue: int,
) -> tuple[int, int]:
    added = hit = 0
    for row in rows:
        title = row.get("title") or ""
        if not title:
            continue
        key = title_key(title)
        target = None
        for link in row.get("links") or []:
            if link.get("url") and ("url:" + link["url"]) in index:
                target = index["url:" + link["url"]]
                break
        if target is None:
            target = index.get(key)

        if target is None:
            target = {
                "key": key,
                "title": title,
                "links": [],
                "sources": [],
                "score": row.get("score"),
                "category": row.get("category"),
                "member_count": row.get("member_count", 1),
                "published": row.get("published"),
                "dates": [],
                "issues": [],
                "first_seen": date,
                "last_seen": date,
                "status": "new",
            }
            pool.append(target)
            index[key] = target
            added += 1
        else:
            hit += 1
            if date < target.get("first_seen", date):
                target["first_seen"] = date
            if date > target.get("last_seen", date):
                target["last_seen"] = date
            if (row.get("score") or 0) > (target.get("score") or 0):
                target["score"] = row.get("score")
            target["member_count"] = max(
                target.get("member_count", 1), row.get("member_count", 1)
            )

        # 日期与期号去重累积
        if date not in target["dates"]:
            target["dates"].append(date)
        if issue not in target["issues"]:
            target["issues"].append(issue)

        # 链接合并（去重）
        have = {l.get("url") for l in target["links"]}
        for link in row.get("links") or []:
            if link.get("url") and link["url"] not in have:
                target["links"].append(link)
                have.add(link["url"])
                index["url:" + link["url"]] = target

        # 来源合并去重
        for s in row.get("sources") or []:
            if s and s not in target["sources"]:
                target["sources"].append(s)
    return added, hit


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="选题池入库（AI 选题雷达 · 长期池）")
    ap.add_argument("--pool", required=True, help="pool.jsonl 路径")
    ap.add_argument("--date", required=True, help="本次运行的日期 YYYY-MM-DD")
    ap.add_argument("--issue", type=int, required=True, help="期号")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--events", help="cluster_events.py 产出的 events.json")
    src.add_argument("--html", help="已出的雷达 HTML（用于回填历史期）")
    ap.add_argument(
        "--filter-html",
        default=None,
        help="配合 --events 使用：只把该期雷达实际收录的条目入库（用标题匹配），"
        "避免把 58 条候选全部灌进池子",
    )
    args = ap.parse_args(argv)

    pool_path = Path(args.pool)
    pool, index = load_pool(pool_path)
    rows = (
        rows_from_events(Path(args.events))
        if args.events
        else rows_from_html(Path(args.html))
    )
    if args.filter_html:
        # 只认每张卡片的「主链接」（h3 与 .one 里的第一个 a），不认同题备选链接：
        # 否则一张卡合并了两个事件时，两个事件都会入库，池子里就多出重复条目。
        page = Path(args.filter_html).read_text(encoding="utf-8")
        keep_urls = set(
            re.findall(r"<h3[^>]*>.*?<a href=\"(https?://[^\"]+)\"", page, re.S)
            + re.findall(r'<p class="one">.*?<a href="(https?://[^"]+)"', page, re.S)
        )
        before = len(rows)
        rows = [
            r
            for r in rows
            if any((l.get("url") or "") in keep_urls for l in (r.get("links") or []))
        ]
        print(
            f"  按本期雷达页面过滤：{before} 条候选 → {len(rows)} 条收录",
            file=sys.stderr,
        )
    added, hit = merge_into_pool(pool, index, rows, args.date, args.issue)

    pool_path.parent.mkdir(parents=True, exist_ok=True)
    with pool_path.open("w", encoding="utf-8") as fh:
        for row in pool:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    multi = sum(1 for r in pool if len(r.get("dates") or []) > 1)
    print(
        f"—— 入库报告 ——\n"
        f"  本期 {len(rows)} 条：新增 {added}，命中已有 {hit}\n"
        f"  池子现在共 {len(pool)} 条，其中 {multi} 条被 2 个以上日期报到",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
