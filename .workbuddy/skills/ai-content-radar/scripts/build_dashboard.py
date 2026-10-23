#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「AI 选题雷达 · 持续看板」—— 一个固定地址、每天刷新的 HTML。

用法：
    python build_dashboard.py --pool ../../雷达/pool.jsonl \
                              --radar-dir ../../雷达 \
                              --out ../../雷达/index.html

它只读 pool.jsonl，不抓任何网络，随时可重跑。页面分三块：
  1. 持续升温：被 2 个以上日期报到的事件（单期雷达看不到这个信号）
  2. 近三天新增：按 AIHOT 评分与新鲜度排序
  3. 历史各期：链接到 雷达/*.html

设计取向：
  * 单文件、零依赖、无 JS —— 发到手机上看也不会塌；
  * 每条都给原文链接，不给摘要段落，避免把别人的文字搬成自己的；
  * 只读池子，不改池子；池子的写入在 update_pool.py。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))

CSS = """
  :root { --ink:#1a1a1a; --sub:#666; --line:#ececec; --accent:#07c160; --warn:#e6a23c; --hi:#f56c6c; --blue:#3a5bd0; }
  * { box-sizing: border-box; }
  body { margin:0; background:#f5f6f7; color:var(--ink); font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; line-height:1.7; }
  .wrap { max-width:820px; margin:0 auto; padding:20px 14px 60px; }
  header { background:linear-gradient(135deg,#0f2027,#203a43,#2c5364); color:#fff; border-radius:14px; padding:22px 20px; }
  header h1 { margin:0 0 6px; font-size:22px; }
  header .meta { font-size:13px; opacity:.85; }
  .stats { display:flex; gap:10px; margin:14px 0; flex-wrap:wrap; }
  .stat { flex:1; min-width:120px; background:#fff; border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
  .stat b { display:block; font-size:22px; line-height:1.3; }
  .stat span { font-size:12px; color:var(--sub); }
  h2 { font-size:17px; margin:26px 0 10px; padding-left:10px; border-left:4px solid var(--accent); }
  .hint { font-size:12px; color:var(--sub); margin:-4px 0 10px 14px; }
  .row { background:#fff; border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin:8px 0; }
  .row .t { font-size:15px; font-weight:600; line-height:1.5; }
  .row .t a { color:var(--ink); text-decoration:none; }
  .row .t a:hover { color:var(--accent); }
  .row .m { font-size:12px; color:var(--sub); margin-top:4px; word-break:break-all; }
  .row .m a { color:#576b95; text-decoration:none; }
  .chip { display:inline-block; border-radius:6px; padding:0 7px; font-size:12px; margin-right:6px; }
  .c-hot { background:#fdecec; color:#a32d2d; }
  .c-new { background:#e8f7ef; color:#0b7a46; }
  .c-iss { background:#eef3ff; color:#3a5bd0; }
  .c-sc { background:#fff4e5; color:#b25e09; }
  .arch { background:#fff; border:1px solid var(--line); border-radius:12px; padding:8px 14px; }
  .arch a { color:#576b95; text-decoration:none; display:block; padding:6px 0; border-bottom:1px dashed var(--line); font-size:14px; }
  .arch a:last-child { border-bottom:0; }
  .foot { margin-top:26px; background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px 16px; font-size:12px; color:var(--sub); }
  .empty { background:#fff; border:1px dashed var(--line); border-radius:12px; padding:18px; color:var(--sub); font-size:13px; }
"""


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def load_pool(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def link_html(row: dict, limit: int = 3) -> str:
    links = [l for l in (row.get("links") or []) if l.get("url")]
    parts = []
    for i, link in enumerate(links[:limit]):
        name = link.get("source") or "原文"
        parts.append(f'<a href="{esc(link["url"])}">{esc(name)}</a>')
    more = ""
    if len(links) > limit:
        more = f" 等 {len(links)} 条"
    return " ・ ".join(parts) + more


def row_html(row: dict, latest: str) -> str:
    days = len(row.get("dates") or [])
    issues = row.get("issues") or []
    chips = []
    if days >= 2:
        chips.append(f'<span class="chip c-hot">累计 {days} 天</span>')
    if row.get("score"):
        chips.append(f'<span class="chip c-sc">AIHOT {row["score"]}</span>')
    if issues:
        chips.append(
            '<span class="chip c-iss">第 ' + "、".join(str(i) for i in issues) + " 期</span>"
        )
    title = esc(row.get("title", ""))
    return (
        '<div class="row">'
        f'<div class="t">{title}</div>'
        f'<div class="m">{"".join(chips)}{link_html(row)}</div>'
        "</div>"
    )


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="生成持续看板（AI 选题雷达）")
    ap.add_argument("--pool", required=True, help="pool.jsonl 路径")
    ap.add_argument("--radar-dir", required=True, help="雷达 HTML 所在目录")
    ap.add_argument("--out", required=True, help="输出 HTML 路径")
    ap.add_argument("--fresh-days", type=int, default=3, help="「近期新增」看最近几天，默认 3")
    args = ap.parse_args(argv)

    rows = load_pool(Path(args.pool))
    radar_dir = Path(args.radar_dir)

    latest = max((r.get("last_seen") or "" for r in rows), default="")
    if not latest:
        latest = datetime.now(CST).date().isoformat()

    try:
        latest_dt = datetime.strptime(latest, "%Y-%m-%d")
    except ValueError:
        latest_dt = datetime.now(CST)
    fresh_cut = (latest_dt - timedelta(days=args.fresh_days - 1)).date().isoformat()

    # 持续升温：被多日报到，按天数、评分、最近出现排序
    hot = [r for r in rows if len(r.get("dates") or []) >= 2]
    hot.sort(
        key=lambda r: (
            -len(r.get("dates") or []),
            -(r.get("score") or 0),
            r.get("last_seen") or "",
        )
    )

    fresh = [r for r in rows if (r.get("first_seen") or "") >= fresh_cut]
    fresh.sort(key=lambda r: (-(r.get("score") or 0), r.get("first_seen") or "", r.get("title") or ""))

    hot_html = "".join(row_html(r, latest) for r in hot) or (
        '<div class="empty">还没有被 2 天以上报到的事件。池子里的日期越多，这块越有用——'
        "同一件事连续几天冒出来，才是真热点；只冒一次的，多数是噪音。</div>"
    )
    fresh_html = "".join(row_html(r, latest) for r in fresh[:40]) or (
        '<div class="empty">最近几天没有新增。</div>'
    )

    # 历史各期
    issues = sorted(
        [p for p in radar_dir.glob("*.html") if p.name != Path(args.out).name],
        key=lambda p: p.name,
        reverse=True,
    )
    arch = []
    for p in issues:
        text = p.read_text(encoding="utf-8", errors="replace")[:4000]
        m = re.search(r"第\s*(\d+)\s*期", text)
        c = re.search(r"共\s*(\d+)\s*条", text)
        label = p.stem
        if m:
            label = f"第 {m.group(1)} 期 · {p.stem}"
        if c:
            label += f" · {c.group(1)} 条"
        arch.append(f'<a href="{esc(p.name)}">{esc(label)}</a>')
    arch_html = "".join(arch) or '<div class="empty">还没有历史期。</div>'

    n_days = len({d for r in rows for d in (r.get("dates") or [])})
    n_multi = len(hot)
    n_issues = len(issues)

    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 选题雷达 · 持续看板</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>AI 选题雷达 · 持续看板</h1>
  <div class="meta">数据截至 {esc(latest)} ・ 生成于 {esc(now)} ・ 每次跑完雷达自动刷新本页</div>
</header>

<div class="stats">
  <div class="stat"><b>{len(rows)}</b><span>池内事件总数</span></div>
  <div class="stat"><b>{n_multi}</b><span>被 2 天以上报到</span></div>
  <div class="stat"><b>{n_days}</b><span>累计覆盖天数</span></div>
  <div class="stat"><b>{n_issues}</b><span>已出期数</span></div>
</div>

<h2>🔥 持续升温</h2>
<div class="hint">同一事件被多个日期报到 —— 这是单期雷达看不见的信号：连报三天的是真热点，只冒一次的多半是噪音。</div>
{hot_html}

<h2>🆕 近 {args.fresh_days} 天新增</h2>
<div class="hint">按 AIHOT 评分与新鲜度排序；没有分数的一手 RSS 条目排在后面，不代表不重要。</div>
{fresh_html}

<h2>🗂 历史各期</h2>
<div class="arch">{arch_html}</div>

<div class="foot">
  <b>这个页面从哪来</b>：由 <code>build_dashboard.py</code> 读取 <code>雷达/pool.jsonl</code> 生成，池子由 <code>update_pool.py</code> 逐期写入，只增不删。<br>
  <b>怎么用</b>：每天跑完雷达后依次执行「入库 → 重绘看板」，本页地址不变，收藏一个就够。<br>
  <b>注意</b>：跨期换了措辞的同一事件不会自动合并（合并只认相同链接与相同标题），判断仍靠人。
</div>
</div>
</body>
</html>
"""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")

    print(
        f"—— 看板已生成 ——\n"
        f"  池内 {len(rows)} 条事件，{n_multi} 条被多日报到，覆盖 {n_days} 天、{n_issues} 期\n"
        f"  近 {args.fresh_days} 天新增 {len(fresh)} 条\n"
        f"  输出：{out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
