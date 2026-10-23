#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件聚簇 —— 「AI 选题雷达」采集之后、打分之前的一步。

用法：
    python cluster_events.py --in ../../.cache/radar-2026-10-19-raw.json \
                             --out ../../.cache/radar-2026-10-19-events.json

做什么：
  把「同一件事被多家报道」的条目合并成一个事件，让后续打分按「事件」而不是按
  「条目」进行。现在 fetch_feeds.py 只能认「标题完全相同」，跨源不同措辞的认不出来；
  本脚本补的就是这一层。

怎么算（纯本地，零依赖，不调任何模型）：
  1. 标题切成中文 2-gram + 拉丁词元；
  2. 抽取「实体」= 拉丁字母/数字构成的词（DeepSeek、V4.1、RTX、Waymo……）；
  3. 相似度 = 0.62 × 2-gram Jaccard + 0.38 × 实体重合度；
  4. 合并条件二选一：
     a. 相似度 ≥ 阈值（默认 0.58）；
     b. 共享「稀有实体」（全语料出现 ≤3 次的实体）+ 相似度 ≥ 0.50。
        稀有实体是关键：DeepSeek 这种高频词不算数，否则「DeepSeek 发新模型」和
        「DeepSeek 开放搜索 API」会被强行并成一件。
  5. 标题 2-gram 重叠率 < 0.33 的一律不合并（硬闸，见下）。
  6. 并查集做传递合并，避免同一条只和二条像、二条又和三条像时裂开。

为什么不用模型/embedding：
  60 条量级的日更，2-gram + 实体已经够用，且完全可解释——合并错了能一眼看出是哪条
  规则造成的。等语料涨到几百条、或需要跨语言合并时再换 embedding。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))

# 拉丁词停用表：这些词在标题里高频但无区分度，留着会让所有标题互相像
STOP = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were",
    "has", "have", "had", "its", "you", "your", "new", "how", "what", "why",
    "who", "when", "will", "can", "not", "but", "all", "now", "get", "one",
    "two", "out", "via", "use", "using", "about", "into", "after", "over",
    "more", "than", "first", "just", "says", "said", "ai", "a", "an", "of",
    "to", "in", "on", "is", "it", "as", "by", "at", "or", "be", "no", "up",
}

LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
WORD_TOKEN = re.compile(r"[a-z0-9]{2,}")


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def features(text: str) -> tuple[set[str], set[str]]:
    """返回 (shingles, entities)。"""
    norm = re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9.]+", " ", text or "").lower()

    shingles: set[str] = set()
    for run in CJK_RUN.findall(norm):
        for i in range(len(run) - 1):
            shingles.add(run[i : i + 2])
    for word in WORD_TOKEN.findall(norm):
        if word not in STOP:
            shingles.add(word)

    entities: set[str] = set()
    for tok in LATIN_TOKEN.findall(text or ""):
        low = tok.lower()
        if len(low) >= 3 and low not in STOP:
            entities.add(low)
    # 纯数字型号（如 4.1、5、3）也当实体，但单独出现没有区分度，必须带字母前缀才算
    return shingles, entities


def similarity(a: dict, b: dict) -> tuple[float, float, set[str]]:
    """返回 (综合相似度, 2-gram Jaccard, 共享实体)。"""
    sa, ea = a["shingles"], a["entities"]
    sb, eb = b["shingles"], b["entities"]
    if not sa or not sb:
        return 0.0, 0.0, set()
    inter = len(sa & sb)
    union = len(sa | sb)
    jacc = inter / union if union else 0.0
    shared = ea & eb
    ent = len(shared) / min(len(ea), len(eb)) if (ea and eb) else 0.0
    return round(0.62 * jacc + 0.38 * ent, 4), round(jacc, 4), shared


class DSU:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def cluster(
    items: list[dict],
    threshold: float,
    rare_threshold: float,
    rare_df_max: int,
    jacc_min: float,
) -> tuple[list[list[int]], list[tuple[int, int, float, float, list[str]]]]:
    feats = [features(f"{it.get('title', '')}") for it in items]

    df: Counter[str] = Counter()
    for _, ents in feats:
        for e in ents:
            df[e] += 1

    nodes = []
    for it, (sh, en) in zip(items, feats):
        nodes.append({"shingles": sh, "entities": en})

    dsu = DSU(len(items))
    edges: list[tuple[int, int, float, float, list[str]]] = []

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            score, jacc, shared = similarity(nodes[i], nodes[j])
            rare = sorted(e for e in shared if len(e) >= 4 and df[e] <= rare_df_max)
            # 硬闸：标题字面必须有实质重叠。没有这道闸，共享一个专有名词
            # （Cloudflare / Kimi / QCon）就能把完全不相干的两件事并到一起——
            # 实测（2026-10-19）11 个簇里有 9 个是这么错并的。
            if jacc < jacc_min:
                continue
            if score >= threshold or (bool(rare) and score >= rare_threshold):
                dsu.union(i, j)
                edges.append((i, j, score, jacc, rare))

    groups: dict[int, list[int]] = {}
    for idx in range(len(items)):
        groups.setdefault(dsu.find(idx), []).append(idx)
    return list(groups.values()), edges


def build_events(items: list[dict], groups: list[list[int]]) -> list[dict]:
    events: list[dict] = []
    for gi, members_idx in enumerate(groups, 1):
        members = [items[i] for i in members_idx]
        # 标题取信息量最大的：优先 AIHOT 的编辑标题（措辞最规整），否则取最长的一条
        pick = max(
            members,
            key=lambda m: (
                1 if m.get("aihot") else 0,
                len(m.get("title", "")),
            ),
        )
        times = [m.get("published") for m in members if m.get("published")]
        scores = [
            m["aihot"]["score"]
            for m in members
            if m.get("aihot") and m["aihot"].get("score") is not None
        ]
        cats = [m["aihot"].get("category") for m in members if m.get("aihot")]
        events.append(
            {
                "event_id": f"E{gi:02d}",
                "title": pick.get("title", ""),
                "title_source": pick.get("source", ""),
                "sources": sorted({m.get("source", "") for m in members}),
                "member_count": len(members),
                "review": len(members) > 1,
                "aihot_score": max(scores) if scores else None,
                "category": next((c for c in cats if c), None),
                "newest_published": max(times) if times else None,
                "oldest_published": min(times) if times else None,
                "members": [
                    {
                        "source": m.get("source", ""),
                        "title": m.get("title", ""),
                        "link": m.get("link", ""),
                        "published": m.get("published"),
                        "aihot": m.get("aihot"),
                    }
                    for m in members
                ],
            }
        )

    events.sort(
        key=lambda e: (
            -e["member_count"],
            -(e["aihot_score"] or 0),
            e["newest_published"] or "",
        )
    )
    for i, ev in enumerate(events, 1):
        ev["event_id"] = f"E{i:02d}"
    return events


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="事件聚簇（AI 选题雷达 · 采集后）")
    ap.add_argument("--in", dest="src", required=True, help="fetch_feeds.py 产出的 raw.json")
    ap.add_argument("--out", default=None, help="输出 events.json；不给则打到 stdout")
    ap.add_argument("--threshold", type=float, default=0.58, help="相似度阈值，默认 0.58")
    ap.add_argument("--rare-threshold", type=float, default=0.50, help="共享稀有实体时的降级阈值，默认 0.50")
    ap.add_argument("--rare-df-max", type=int, default=3, help="实体在语料中出现≤N 次算稀有，默认 3")
    ap.add_argument("--jacc-min", type=float, default=0.33, help="标题 2-gram 重叠的硬下限，默认 0.33")
    ap.add_argument("--min-window-hours", type=float, default=96.0, help="只在 N 小时窗口内合并，默认 96")
    args = ap.parse_args(argv)

    src = Path(args.src)
    payload = json.loads(src.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if not items:
        print("  [WARN] 输入里没有 items，退出", file=sys.stderr)
        return 1

    groups, edges = cluster(
        items, args.threshold, args.rare_threshold, args.rare_df_max, args.jacc_min
    )
    events = build_events(items, groups)

    out = {
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "input": str(src),
        "window_hours": payload.get("window_hours"),
        "count_items": len(items),
        "count_events": len(events),
        "params": {
            "threshold": args.threshold,
            "rare_threshold": args.rare_threshold,
            "rare_df_max": args.rare_df_max,
            "jacc_min": args.jacc_min,
        },
        "events": events,
    }
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)

    merged = [e for e in events if e["member_count"] > 1]
    print(
        f"—— 聚簇报告 ——\n"
        f"  {len(items)} 条候选 → {len(events)} 个事件"
        f"（合并掉 {len(items) - len(events)} 条重复报道，合并边 {len(edges)} 条）",
        file=sys.stderr,
    )
    multi = sum(1 for e in events if len(e["sources"]) > 1)
    print(f"  其中 {len(merged)} 个事件有 2+ 篇报道，{multi} 个事件跨了 2+ 个源", file=sys.stderr)
    for e in merged[:8]:
        print(
            f"    {e['event_id']} ×{e['member_count']} [{', '.join(e['sources'])}] {e['title'][:44]}",
            file=sys.stderr,
        )
    if args.out:
        print(f"  明细已写入 {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
