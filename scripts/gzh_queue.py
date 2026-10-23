#!/usr/bin/env python3
"""稿件队列：盘点 / 取下一篇 / 改状态 / 过期清理 / 迁移修复。

    python scripts/gzh_queue.py list --json
    python scripts/gzh_queue.py next --json
    python scripts/gzh_queue.py mark --dir <稿件目录> --state published --publish-appmsgid 100000089
    python scripts/gzh_queue.py prune --expire-days 4
    python scripts/gzh_queue.py sync --json
    python scripts/gzh_queue.py sync --record-published 100000089 --date 2026-10-22 --title "..."

退出码：0 成功 / 2 队列无可发 / 3 目标不存在 / 1 非法操作 / 10 锁被占用
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gzh_lock  # noqa: E402
import gzh_state as st  # noqa: E402
from gzh_draft import workroot  # noqa: E402

ROOT = workroot()
TARGET_STOCK = 3
MAX_NEW_PER_DAY = 2
EXPIRE_DAYS = 4


def parse_day(s: str | None) -> date:
    if not s:
        return date.today()
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def age_days(rec: dict, today: date) -> int:
    d = rec.get("date") or str(rec.get("created_at") or "")[:10]
    try:
        return (today - parse_day(d)).days
    except Exception:
        return 0


def is_retry_recent(rec: dict) -> bool:
    """24 小时内因扫码超时失败过 → 重试优先，别被别的稿顶掉。"""
    if rec.get("last_error") != "scan_timeout":
        return False
    ts = rec.get("last_attempt_at")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    return (datetime.now() - dt).total_seconds() < 24 * 3600


def collect(today: date, max_age: int = EXPIRE_DAYS) -> tuple[list[dict], dict]:
    items: list[dict] = []
    counts = {"written": 0, "drafted": 0, "published": 0, "stale": 0, "dropped": 0}
    for d, rec in st.iter_drafts(ROOT):
        state = rec.get("state", "written")
        counts[state] = counts.get(state, 0) + 1
        a = age_days(rec, today)
        items.append(
            {
                "dir": str(d.relative_to(ROOT).as_posix()),
                "title": rec.get("title"),
                "state": state,
                "appmsgid": rec.get("appmsgid"),
                "publish_appmsgid": rec.get("publish_appmsgid"),
                "age_days": a,
                "expired": a > max_age,
                "rating": rec.get("rating"),
                "score": rec.get("score"),
                "attempts": int(rec.get("attempts") or 0),
                "last_error": rec.get("last_error"),
                "retry_recent": is_retry_recent(rec),
                "has_body": (ROOT / d / "body.html").exists(),
            }
        )
    return items, counts


def candidates(items: list[dict]) -> list[dict]:
    return [
        i
        for i in items
        if i["state"] == "drafted"
        and i["appmsgid"]
        and i["has_body"]
        and not i["expired"]
    ]


def rank(items: list[dict]) -> list[dict]:
    """FIFO 优先；24h 内扫码超时过的排最前（13:00 兜底要靠它）。"""
    return sorted(
        items,
        key=lambda i: (
            0 if i["retry_recent"] else 1,
            -i["age_days"],
            -(i["rating"] or 0),
            -(i["score"] or 0),
            str(i["appmsgid"]),
        ),
    )


# --------------------------------------------------------------------------- #
def cmd_list(args) -> int:
    today = parse_day(args.today)
    items, counts = collect(today, args.max_age)
    stock = counts.get("drafted", 0)
    wip = counts.get("written", 0)
    shortfall = max(0, TARGET_STOCK - stock - wip)
    payload = {
        "ok": True,
        "today": str(today),
        "stock": stock,
        "wip": wip,
        "target": TARGET_STOCK,
        "shortfall": shortfall,
        "todo": min(shortfall, MAX_NEW_PER_DAY),
        "published_today": st.published_today(ROOT),
        "counts": counts,
        "items": items,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"库存 {stock} / 目标 {TARGET_STOCK}，在写 {wip}，待补 {payload['todo']}")
        print(f"今天发过没有：{payload['published_today']['detail']}")
        for i in sorted(items, key=lambda x: (x["state"], x["age_days"])):
            mark = "!" if i["expired"] else " "
            print(
                f" {mark} {i['state']:<9} {i['age_days']}天  {str(i['title'])[:30]:<32}"
                f" {i['appmsgid'] or '-':<10} {i['dir']}"
            )
    return 0


def cmd_next(args) -> int:
    today = parse_day(args.today)
    items, _ = collect(today, args.max_age)
    cands = rank(candidates(items))
    if not cands:
        any_drafted = [i for i in items if i["state"] == "drafted"]
        reason = "all_expired" if any_drafted else (
            "all_written" if any(i["state"] == "written" for i in items) else "empty"
        )
        advice = {
            "empty": "python scripts/gzh_prep.py（补稿）",
            "all_written": "先存草稿：gzh_auto --manifest <dir>/manifest.json --daily-cap 3 --publish none",
            "all_expired": "库存全过期：prune 清掉后补稿",
        }[reason]
        print(json.dumps({"ok": False, "reason": reason, "advice": advice}, ensure_ascii=False, indent=2))
        return 2
    pick = cands[0]
    payload = {
        "ok": True,
        "pick": pick,
        "why": ("retry:" if pick["retry_recent"] else "fifo:") + f"age={pick['age_days']}",
        "alternatives": [
            {"title": c["title"], "age_days": c["age_days"], "appmsgid": c["appmsgid"]}
            for c in cands[1:3]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _resolve_dir(args) -> Path | None:
    if args.dir:
        p = Path(args.dir)
        return (p if p.is_absolute() else ROOT / p).resolve()
    if args.appmsgid:
        return st.find_dir_by_appmsgid(ROOT, args.appmsgid)
    return None


def cmd_mark(args) -> int:
    d = _resolve_dir(args)
    if d is None or not d.exists():
        print(json.dumps({"ok": False, "error": "找不到稿件目录"}, ensure_ascii=False))
        return 3
    if args.publish_appmsgid or args.published_at:
        # 走专用的 published 路径，保证 ledger 字段完整
        st.write_draft(d, {"appmsgid": (st.read_draft(d) or {}).get("appmsgid")})
        ok, msg = st.mark_state(ROOT, d, "published", args.note, force=True)
        st.write_draft(
            d,
            {
                "publish_appmsgid": args.publish_appmsgid,
                "published_at": args.published_at or st.now_iso(),
            },
        )
        st.append_ledger_once(
            ROOT,
            {
                "date": (args.published_at or st.now_iso())[:10],
                "ts": st.now_iso(),
                "state": "published",
                "event": "published(manual)",
                "appmsgid": (st.read_draft(d) or {}).get("appmsgid"),
                "publish_appmsgid": args.publish_appmsgid,
                "dir": str(d.relative_to(ROOT).as_posix()),
                "title": (st.read_draft(d) or {}).get("title"),
            },
        )
        print(json.dumps({"ok": True, "state": "published", "detail": msg}, ensure_ascii=False, indent=2))
        return 0
    ok, msg = st.mark_state(ROOT, d, args.state, args.note, args.force)
    print(json.dumps({"ok": ok, "state": args.state, "detail": msg}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_prune(args) -> int:
    today = parse_day(args.today)
    items, _ = collect(today, args.expire_days)
    todo = [i for i in items if i["expired"] and i["state"] in ("written", "drafted")]
    if not todo:
        print(json.dumps({"ok": True, "pruned": 0, "detail": "没有过期稿"}, ensure_ascii=False))
        return 0
    for i in todo:
        st.mark_state(ROOT, ROOT / i["dir"], "stale", f"expired>{args.expire_days}d")
    payload = {
        "ok": True,
        "pruned": len(todo),
        "items": [{"dir": i["dir"], "title": i["title"], "age_days": i["age_days"]} for i in todo],
        "draft_cleanup_cmd": "python scripts/gzh_cleanup.py --drafts "
        + " ".join(str(i["appmsgid"]) for i in todo if i["appmsgid"])
        + " --yes"
        if any(i["appmsgid"] for i in todo)
        else None,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.delete_drafts:
        print("提示：--delete-drafts 需要浏览器与登录态，请手动执行上面的 draft_cleanup_cmd", file=sys.stderr)
    return 0


def cmd_sync(args) -> int:
    """补出缺失的 draft.json；可顺手补记历史发表（人工扫码那类）。"""
    created = 0
    fixed = 0
    for d, rec in st.iter_drafts(ROOT):
        pass  # 已有 draft.json 的跳过

    # 1) 有目录没 draft.json 的，从 manifest + briefing 重建
    base = ROOT / "稿件"
    if base.exists():
        for d in sorted(base.iterdir()):
            if not d.is_dir() or st.draft_path(d).exists():
                continue
            man = d / "manifest.json"
            brief = d / "briefing.json"
            meta = {}
            if man.exists():
                try:
                    meta = json.loads(man.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            job = {}
            if brief.exists():
                try:
                    job = json.loads(brief.read_text(encoding="utf-8"))
                except Exception:
                    job = {}
            has_body = (d / "body.html").exists()
            st.write_draft(
                d,
                {
                    "state": "written",
                    "date": job.get("date") or d.name[:10],
                    "title": meta.get("title") or job.get("title_hint"),
                    "pool_key": job.get("pool_key"),
                    "form": job.get("form"),
                    "rating": job.get("rating"),
                    "score": job.get("score"),
                    "note": "sync: 重建",
                },
            )
            created += 1

    # 1.5) 与台账对账：目录刚建出来时不知道 appmsgid/状态，台账里有就以台账为准
    ledger = st.read_ledger(ROOT)
    reconciled = 0
    for d, rec in st.iter_drafts(ROOT):
        rel = str(d.relative_to(ROOT).as_posix())
        rows = [
            r
            for r in ledger
            if (r.get("dir") == rel)
            or (rec.get("title") and r.get("title") == rec.get("title"))
        ]
        if not rows:
            continue
        latest = sorted(rows, key=lambda r: str(r.get("ts") or ""))[-1]
        want = latest.get("state")
        patch: dict = {}
        if want in ("drafted", "published") and rec.get("state") != want:
            patch["state"] = want
        if latest.get("appmsgid") and not rec.get("appmsgid"):
            patch["appmsgid"] = str(latest["appmsgid"])
        if latest.get("publish_appmsgid") and not rec.get("publish_appmsgid"):
            patch["publish_appmsgid"] = str(latest["publish_appmsgid"])
        if patch:
            st.write_draft(d, patch)
            reconciled += 1

    # 2) 手工补记历史发表（不依赖浏览器，把事实写进台账）
    if args.record_published:
        st.append_ledger_once(
            ROOT,
            {
                "date": args.date or st.today(),
                "ts": st.now_iso(),
                "state": "published",
                "event": "published(backfill)",
                "appmsgid": args.record_published,
                "publish_appmsgid": args.record_published,
                "dir": None,
                "title": args.title or "(backfill)",
            },
        )
        fixed = 1

    print(
        json.dumps(
            {"ok": True, "created": created, "recorded": fixed, "published_today": st.published_today(ROOT)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="稿件队列")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="盘点")
    p.add_argument("--json", action="store_true")
    p.add_argument("--today", default=None, help="按指定日期算年龄（回测用）")
    p.add_argument("--max-age", type=int, default=EXPIRE_DAYS)
    p.set_defaults(fn=cmd_list, need_lock=False)

    p = sub.add_parser("next", help="下一篇发哪个")
    p.add_argument("--json", action="store_true")
    p.add_argument("--today", default=None)
    p.add_argument("--max-age", type=int, default=EXPIRE_DAYS)
    p.set_defaults(fn=cmd_next, need_lock=False)

    p = sub.add_parser("mark", help="改状态")
    p.add_argument("--dir", default=None)
    p.add_argument("--appmsgid", default=None)
    p.add_argument("--state", default="published",
                   choices=["written", "drafted", "published", "stale", "dropped"])
    p.add_argument("--publish-appmsgid", default=None)
    p.add_argument("--published-at", default=None)
    p.add_argument("--note", default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_mark, need_lock=True)

    p = sub.add_parser("prune", help="过期清理")
    p.add_argument("--expire-days", type=int, default=EXPIRE_DAYS)
    p.add_argument("--today", default=None)
    p.add_argument("--delete-drafts", action="store_true")
    p.set_defaults(fn=cmd_prune, need_lock=True)

    p = sub.add_parser("sync", help="补 draft.json / 补记发表")
    p.add_argument("--json", action="store_true")
    p.add_argument("--record-published", default=None)
    p.add_argument("--date", default=None)
    p.add_argument("--title", default=None)
    p.set_defaults(fn=cmd_sync, need_lock=True)

    args = ap.parse_args(argv)

    if (ROOT / ".cache" / "STOP_AUTO").exists():
        print(json.dumps({"ok": False, "halted": "STOP_AUTO"}, ensure_ascii=False))
        return 3

    lock_path = None
    if args.need_lock:
        ok, lock_path = gzh_lock.acquire(ROOT)
        if not ok:
            print(json.dumps({"ok": False, "halted": "locked", "detail": lock_path}, ensure_ascii=False))
            return 10
    try:
        return args.fn(args)
    finally:
        gzh_lock.release(lock_path)


if __name__ == "__main__":
    sys.exit(main())
