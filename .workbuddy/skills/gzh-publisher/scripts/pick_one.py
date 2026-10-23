#!/usr/bin/env python3
"""每天从选题池里挑一条能发的（宁缺毋滥）。

    python scripts/pick_one.py                    # 挑今天该发的，写 .cache/picks/<date>.json
    python scripts/pick_one.py --explain          # 打印全部候选的分数与被拒原因
    python scripts/pick_one.py --dry-run          # 只算不写
    python scripts/pick_one.py --date 2026-10-21  # 按指定日期算新鲜度（回测用）

退出码（与全局约定一致）：
    0  挑到了（写文件）
    2  今日无达标选题（**这不是错误**，是「宁缺毋滥」的正常结果）
    3  急停（.cache/STOP_AUTO 存在）
   10  锁被占用
    1  异常

打分（已用 6 期真实数据回测）：
    S = 100 × (0.42·R + 0.18·Q + 0.15·F + 0.15·H + 0.10·M) − P
    ★★★ 下限 71.0 / ★★ 上限 59.9，中间 11.1 分空档 → 阈值取 68.0
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gzh_lock  # noqa: E402
import pool_store as ps  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POOL = ROOT / "雷达" / "pool.jsonl"
PICKS_DIR = ROOT / ".cache" / "picks"

THRESHOLD = 68.0
RATING_SCORE = {3: 1.00, 2: 0.62, 1: 0.28, None: 0.15}
W = {"R": 0.42, "Q": 0.18, "F": 0.15, "H": 0.15, "M": 0.10}
FORBIDDEN_WORDS = ("招生", "招聘", "促销", "优惠", "抽奖", "加盟", "课程报名")


def parse_day(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s)[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def row_day(row: dict) -> date | None:
    """这条素材最近被看见的日期。"""
    for key in ("last_seen", "first_seen"):
        d = parse_day(row.get(key))
        if d:
            return d
    dates = row.get("dates") or []
    if dates:
        return parse_day(dates[-1])
    return None


def score_row(row: dict, today: date) -> dict:
    """返回 {S, parts, penalty_detail}，所有因子都可解释。"""
    rating = row.get("rating")
    r = RATING_SCORE.get(rating if rating in (1, 2, 3) else None, 0.15)

    raw_q = row.get("aihot_score") or row.get("score")
    q = (float(raw_q) / 100.0) if raw_q else 0.50

    d = row_day(row)
    age = (today - d).days if d else None
    f = math.exp(-max(0, age) / 1.8) if age is not None else 0.0

    member = row.get("member_count") or 1
    h = min(1.0, math.log2(1 + max(1, member)) / math.log2(8))

    m = min(1.0, max(0, len(row.get("dates") or []) - 1) / 2)

    penalties: list[tuple[str, int]] = []
    if row.get("lang") == "en":
        penalties.append(("纯英文源", 3))
    links = ps.row_links(row)
    if len(links) <= 1:
        penalties.append(("单链接", 2))
    if not row.get("desc") and not row.get("aihot_reason"):
        penalties.append(("素材过薄", 5))
    if row.get("tag") == "需授权" and links and all("mp.weixin.qq.com" in u for u in links):
        penalties.append(("封闭源", 4))
    p = sum(v for _, v in penalties)

    s = 100 * (W["R"] * r + W["Q"] * q + W["F"] * f + W["H"] * h + W["M"] * m) - p
    return {
        "S": round(max(0.0, s), 1),
        "parts": {
            "R": round(r, 3), "Q": round(q, 3), "F": round(f, 3),
            "H": round(h, 3), "M": round(m, 3), "P": -p,
            "age_days": age,
            "degraded": member <= 1 and len(row.get("dates") or []) <= 1,
        },
        "penalty_detail": [f"{k}(-{v})" for k, v in penalties],
    }


def hard_rules(row: dict, today: date, used_keys: set[str]) -> str | None:
    """返回拒绝原因；None 表示通过准入。"""
    if row.get("rating") != 3:
        return "not_three_star"
    if row.get("status") in ("drafted", "published", "dropped", "picked"):
        return f"already_{row.get('status')}"
    d = row_day(row)
    if d and (today - d).days > 3:
        return "stale"
    if not [u for u in ps.row_links(row) if u.startswith("http")]:
        return "no_source"
    if row.get("key") in used_keys:
        return "duplicate_recent"
    title = row.get("display_title") or row.get("title") or ""
    if any(w in title for w in FORBIDDEN_WORDS):
        return "off_topic"
    return None


def load_used_keys(pool: list[dict]) -> set[str]:
    """近 7 天已用（含已存草稿）的 key，用于跨期去重。

    另外把「2 天内被回滚过」的也算 used：稿件写坏 → 回滚成 new → 立刻又被挑中 → 又写坏，
    这个循环会让同一条选题反复消耗两三天。冷却期把环打断。
    """
    today = date.today()
    used: set[str] = set()
    for r in pool:
        key = r.get("key")
        if not key:
            continue
        if r.get("status") in ("drafted", "published", "picked"):
            d = parse_day(str(r.get("used_at") or r.get("picked_at") or "")[:10])
            if d and (today - d).days <= 7:
                used.add(key)
        rel = parse_day(str(r.get("released_at") or "")[:10])
        if rel and (today - rel).days <= 2:
            used.add(key)
    return used


def form_hint(row: dict) -> str:
    """按区块/标签给一个形态建议（A 快讯 / B 分析 / C 监管 / D 资本 / E 访谈）。"""
    sec = row.get("section") or ""
    title = (row.get("display_title") or row.get("title") or "")
    if "资本" in sec or any(k in title for k in ("融资", "估值", "财报", "收购", "并购")):
        return "D"
    if "焦点" in sec or any(k in title for k in ("监管", "诉讼", "起诉", "封禁", "漏洞", "泄露", "安全")):
        return "C"
    if "技术" in sec or "开源" in title:
        return "A" if len(row.get("points") or []) <= 3 else "B"
    return "B"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从选题池挑一条")
    ap.add_argument("--date", default=None, help="按指定日期算新鲜度（默认今天）")
    ap.add_argument("--explain", action="store_true", help="打印全部候选的分数与被拒原因（隐含 --dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    ap.add_argument("--top", type=int, default=3, help="输出几条备选")
    ap.add_argument("--min-rating", type=int, default=3, help="星级门槛（测试用，默认 3）")
    args = ap.parse_args(argv)
    if args.explain:
        args.dry_run = True  # 只是看分数，绝不能顺手改池子

    if (ROOT / ".cache" / "STOP_AUTO").exists():
        print(json.dumps({"ok": False, "halted": "STOP_AUTO"}, ensure_ascii=False))
        return 3

    ok, lock = gzh_lock.acquire(ROOT, "pick")
    if not ok:
        print(json.dumps({"ok": False, "halted": "locked", "detail": lock}, ensure_ascii=False))
        return 10
    try:
        return _run(args)
    finally:
        gzh_lock.release(lock)


def _run(args) -> int:
    today = parse_day(args.date) or date.today()
    pool = ps.load_pool(POOL)
    if not pool:
        print(json.dumps({"ok": False, "error": f"池子为空：{POOL}"}, ensure_ascii=False))
        return 1

    used_keys = load_used_keys(pool)
    scored: list[dict] = []
    rejected: list[dict] = []

    for row in pool:
        s = score_row(row, today)
        why = hard_rules(row, today, used_keys)
        if why is None and row.get("rating", 0) < args.min_rating:
            why = "below_min_rating"
        if why is None and s["S"] < THRESHOLD:
            why = "below_threshold"
        rec = {
            "key": row.get("key"),
            "title": row.get("display_title") or row.get("title"),
            "rating": row.get("rating"),
            "score": s["S"],
            "parts": s["parts"],
            "penalty": s["penalty_detail"],
            "why": why,
            "row": row,
        }
        (scored if why is None else rejected).append(rec)

    scored.sort(key=lambda x: (-x["score"], -(x["rating"] or 0), -len(x["row"].get("points") or [])))
    rejected.sort(key=lambda x: -x["score"])

    if args.explain:
        print(f"阈值 {THRESHOLD}  候选 {len(pool)}  通过 {len(scored)}  （按分数降序，前 20 条）")
        for r in (scored + rejected)[:20]:
            mark = "✓" if r["why"] is None else f"✗ {r['why']}"
            p = r["parts"]
            print(
                f"  {r['score']:5.1f} ★{r['rating'] or '-'} {str(r['title'])[:34]:<36}"
                f" R{p['R']:.2f} Q{p['Q']:.2f} F{p['F']:.2f} H{p['H']:.2f} M{p['M']:.2f}"
                f" P{p['P']} → {mark}"
            )
        print()

    out_path = PICKS_DIR / f"{today}.json"
    if not scored:
        payload = {
            "ok": False,
            "date": str(today),
            "picked": None,
            "reason": "no_candidate_above_threshold",
            "checked": len(pool),
            "best": (
                {
                    "title": rejected[0]["title"],
                    "rating": rejected[0]["rating"],
                    "score": rejected[0]["score"],
                    "why": rejected[0]["why"],
                }
                if rejected
                else None
            ),
            "skipped": [{"key": r["key"], "why": r["why"]} for r in rejected[:10]],
        }
        if not args.dry_run:
            PICKS_DIR.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    best = scored[0]
    row = best["row"]
    payload = {
        "ok": True,
        "date": str(today),
        "checked": len(pool),
        "picked": {
            "key": row.get("key"),
            "display_title": row.get("display_title") or row.get("title"),
            "pool_title": row.get("title"),
            "rating": row.get("rating"),
            "score": best["score"],
            "score_parts": best["parts"],
            "section": row.get("section"),
            "tag": row.get("tag"),
            "form_hint": form_hint(row),
            "links": row.get("links") or [],
            "points": row.get("points") or [],
            "desc": row.get("desc"),
            "lang": row.get("lang"),
            "source": "pool",
        },
        "runner_up": [
            {"title": r["title"], "rating": r["rating"], "score": r["score"]}
            for r in scored[1 : 1 + args.top]
        ],
    }
    if not args.dry_run:
        PICKS_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # 标记 picked，避免同一天被重复选中
        for r in pool:
            if r.get("key") == row.get("key"):
                r["status"] = "picked"
                r["picked_at"] = datetime.now().isoformat(timespec="seconds")
                break
        ps.save_pool(POOL, pool)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
