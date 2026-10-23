#!/usr/bin/env python3
"""一键备好一篇稿子的「写作工地」：选稿 → 抓原文 → 裁封面 → 建目录 → 给写作简报。

    python scripts/gzh_prep.py                 # 自动选稿（宁缺毋滥，没达标就 exit 2）
    python scripts/gzh_prep.py --key <key>     # 指定池子里某一条
    python scripts/gzh_prep.py --url <url>     # 完全手给一条源
    python scripts/gzh_prep.py --dry-run       # 只打印会做什么

产出目录 `稿件/<日期>-<slug>/`：
    source.md        原文全文（写作时读这个，不要凭记忆写）
    briefing.json    选题元数据 + 要点 + 建议形态 + 字数区间（给写作的人/模型看）
    manifest.json    manifest 骨架，body_html_path 已指向 body.html，封面已就位
    cover.jpg        已裁成 2.35:1（900×383）的封面

之后写 `body.html`（照 组件库.md），再跑 gzh_lint → gzh_auto。
本脚本**不写稿**：写稿要判断力和文风，留给模型。

退出码：0 备好了 / 2 今日无达标选题 / 3 急停 / 1 出错
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gzh_lock  # noqa: E402
from gzh_draft import workroot  # noqa: E402

ROOT = workroot()
DRAFTS = ROOT / "稿件"
FORM_RANGE = {
    "A": (600, 800, "快讯"),
    "B": (1000, 1600, "分析"),
    "C": (1000, 1400, "监管"),
    "D": (800, 1200, "资本"),
    "E": (1500, 2000, "访谈"),
}


def slugify(s: str, limit: int = 24) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s or "")
    return (s[:limit] or "untitled")


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout or "", p.stderr or ""


def pick(dry: bool) -> dict | None:
    args = [sys.executable, str(HERE / "pick_one.py")]
    if dry:
        args.append("--dry-run")
    code, out, err = run(args, 120)
    if code == 2:
        try:
            payload = json.loads(out[out.index("{"):])
        except Exception:
            payload = {"reason": "no_candidate", "raw": out[-300:]}
        print(json.dumps({"ok": False, "stage": "pick", "detail": payload}, ensure_ascii=False, indent=2))
        return None
    if code != 0:
        print(f"选稿失败 exit={code}\n{err[-500:]}", file=sys.stderr)
        return None
    try:
        return json.loads(out[out.index("{"):])
    except Exception:
        print(f"选稿输出解析失败：{out[-400:]}", file=sys.stderr)
        return None


def build_job(p: dict) -> dict:
    """把 pick_one 的输出变成写作简报。"""
    row = p["picked"]
    form = row.get("form_hint") or "B"
    lo, hi, label = FORM_RANGE.get(form, FORM_RANGE["B"])
    return {
        "title_hint": row.get("display_title"),
        "pool_title": row.get("pool_title"),
        "pool_key": row.get("key"),  # 回滚选题池的唯一依据，必须带出来
        "rating": row.get("rating"),
        "score": row.get("score"),
        "score_parts": row.get("score_parts"),
        "form": form,
        "form_label": label,
        "chars_range": [lo, hi],
        "points": row.get("points") or [],
        "desc": row.get("desc"),
        "lang": row.get("lang"),
        "section": row.get("section"),
        "tag": row.get("tag"),
        "links": [l.get("url") for l in (row.get("links") or []) if l.get("url")],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="备好一篇稿子的写作工地")
    ap.add_argument("--key", default=None, help="池子里某条的 key（跳过自动选稿）")
    ap.add_argument("--url", default=None, help="直接给源地址（完全手动）")
    ap.add_argument("--out-dir", default=None, help="输出目录，默认 稿件/<日期>-<slug>")
    ap.add_argument("--no-fit", action="store_true", help="不裁封面（默认裁成 2.35:1）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if (ROOT / ".cache" / "STOP_AUTO").exists():
        print(json.dumps({"ok": False, "halted": "STOP_AUTO"}, ensure_ascii=False))
        return 3

    ok, lock = gzh_lock.acquire(ROOT, "prep")
    if not ok:
        print(json.dumps({"ok": False, "halted": "locked", "detail": lock}, ensure_ascii=False))
        return 10
    try:
        return _run(args)
    finally:
        gzh_lock.release(lock)


def _run(args) -> int:
    today = date.today().isoformat()

    # 0 先回填星级：雷达每天 9:00 出稿，HTML 里的 ★ 不会自动进池子，
    #   不回填的话新选题 rating=None，选稿器一律按「未评级」压到最低分。
    #   回填是幂等的（只填空值），每次跑一遍没有副作用。
    if not args.url and not args.dry_run:
        code, out, err = run([sys.executable, str(HERE / "backfill_pool.py")], 120)
        if code != 0:
            print(f"⚠ 星级回填失败（继续）：{(err or out)[-200:]}", file=sys.stderr)

    # 0.5 池子新鲜度提醒：撞车（雷达还没跑完）最典型的症状是静默的「今日无达标选题」，
    #     这里主动说一句，免得把它误当成「今天真没料」。
    if not args.url:
        try:
            import pool_store as _ps

            rows = _ps.load_pool(ROOT / "雷达" / "pool.jsonl")
            todays = [r for r in rows if str(r.get("first_seen") or "")[:10] == today]
            if not todays:
                print(
                    f"⚠ 池子里没有 {today} 新入库的选题（雷达可能还没跑完）。"
                    "如果现在早于 09:45，稍后再试一次。",
                    file=sys.stderr,
                )
        except Exception:
            pass

    # 1 定选题
    if args.url:
        job = {
            "title_hint": None, "form": "B", "form_label": "分析",
            "chars_range": [1000, 1600], "points": [], "desc": None,
            "lang": None, "links": [args.url], "rating": None, "score": None,
            "source": "manual",
        }
    elif args.key:
        import pool_store as ps

        rows = ps.load_pool(ROOT / "雷达" / "pool.jsonl")
        row = next((r for r in rows if r.get("key") == args.key), None)
        if not row:
            print(json.dumps({"ok": False, "error": f"池子里没有 key={args.key}"}, ensure_ascii=False))
            return 1
        job = build_job({"picked": {
            "display_title": row.get("display_title") or row.get("title"),
            "pool_title": row.get("title"), "key": row.get("key"),
            "rating": row.get("rating"),
            "score": None, "score_parts": None,
            "form_hint": "B", "points": row.get("points") or [],
            "desc": row.get("desc"), "lang": row.get("lang"),
            "section": row.get("section"), "tag": row.get("tag"),
            "links": row.get("links") or [],
        }})
    else:
        picked = pick(args.dry_run)
        if not picked or not picked.get("ok"):
            return 3 if picked is None else 2
        job = build_job(picked)

    if not job.get("links"):
        print(json.dumps({"ok": False, "error": "这条没有可用的源链接"}, ensure_ascii=False))
        return 1

    src_url = job["links"][0]
    slug = slugify(job.get("title_hint") or src_url)
    out_dir = Path(args.out_dir).resolve() if args.out_dir else DRAFTS / f"{today}-{slug}"

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "out_dir": str(out_dir), "job": job},
                         ensure_ascii=False, indent=2))
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    # 2 抓原文
    code, out, err = run(
        [sys.executable, str(HERE / "gzh_source.py"), "--url", src_url,
         "--out-dir", str(out_dir), "--lang", job.get("lang") or ""],
        300,
    )
    src_info = {}
    try:
        payload = json.loads(out[out.index("{"):])
        src_info = (payload.get("sources") or [{}])[0] or {}
    except Exception:
        pass
    if code != 0 or not src_info.get("ok"):
        import gzh_state

        gzh_state.write_draft(
            out_dir,
            {"state": "dropped", "date": today, "title": job.get("title_hint"),
             "pool_key": job.get("pool_key"), "note": f"source_fetch_failed:{code}"},
        )
        if job.get("pool_key"):
            gzh_state.release_pool_item(ROOT, job["pool_key"])
        print(json.dumps({"ok": False, "stage": "source", "pool_released": bool(job.get("pool_key")),
                          "detail": (err or out)[-500:]},
                         ensure_ascii=False, indent=2))
        return 1

    src_md = Path(src_info["source_md"]) if src_info.get("source_md") else None
    cover = Path(src_info["cover"]) if src_info.get("cover") else None

    # 3 封面：优先用原图裁 2.35:1；源上没图就渲染一张中文封面卡
    fit_info = None
    if cover and cover.exists() and not args.no_fit:
        from gzh_cover import crop_to_ratio

        target = out_dir / "cover.jpg"
        fit_info = crop_to_ratio(cover, target, 2.35, (900, 383))
        if fit_info.get("ok"):
            cover = target
    if (not cover or not cover.exists()) and not args.no_fit:
        from gzh_covercard import load_brand, render

        title = job.get("title_hint") or "AI 观察"
        brand = load_brand()
        card = render(title, None, brand["kick"], brand["foot"], out_dir / "cover.jpg")
        if card.get("ok"):
            cover = Path(card["cover"])
            fit_info = {"ok": True, "mode": "covercard", "size": card.get("size"), "lines": card.get("lines")}

    # 4 manifest 骨架
    manifest = {
        "title": job.get("title_hint") or "",
        "summary": "",
        "author": "",
        "body_html_path": "body.html",
        "cover_path": str(cover.relative_to(out_dir)) if cover and cover.exists() else None,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 5 写作简报
    briefing = {
        **job,
        "date": today,
        "source_url": src_url,
        "source_md": str(src_md) if src_md else None,
        "cover": str(cover) if cover else None,
        "cover_fit": fit_info,
        "out_dir": str(out_dir),
        "writer_todo": [
            "读 source_md（原文），不要凭记忆写",
            "照 组件库.md 选形态、拼结构，写 body.html（只许 section/span/strong/br/img）",
            f"字数控制在 {job['chars_range'][0]}–{job['chars_range'][1]} 字，加粗 6–10 处",
            "写 title（≤64 字）、summary（60–120 字），填进 manifest.json",
            "python scripts/gzh_lint.py --manifest <manifest> --form <形态> 直到通过",
            "python scripts/gzh_draft.py --manifest <manifest> --check-only 确认 errors 为空",
        ],
    }
    (out_dir / "briefing.json").write_text(
        json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    import gzh_state

    gzh_state.write_draft(
        out_dir,
        {
            "state": "written",
            "date": today,
            "title": manifest["title"] or None,
            "pool_key": job.get("pool_key"),
            "form": job.get("form"),
            "rating": job.get("rating"),
            "score": job.get("score"),
            "source_url": src_url,
        },
    )

    print(json.dumps({
        "ok": True,
        "out_dir": str(out_dir),
        "form": job.get("form"),
        "chars_range": job["chars_range"],
        "source_md": briefing["source_md"],
        "cover": briefing["cover"],
        "next": f"写 {out_dir / 'body.html'}，然后 gzh_lint → gzh_draft --check-only → gzh_auto",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
