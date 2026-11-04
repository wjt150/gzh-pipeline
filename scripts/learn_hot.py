#!/usr/bin/env python3
"""每日样本：今天大众在点什么标题，我们的稿子差在哪。

    python scripts/learn_hot.py              # 用当天 雷达/public-heat-<date>.json
    python scripts/learn_hot.py --date 2026-11-03
    python scripts/learn_hot.py --json

它不做的事：不抓小红书（要登录、易风控）、不猜阅读量（拿不到）。
它做三件事：
    1 把当天热榜里 AI 相关标题抽出来，打「结构标签」（数字 / 疑问 / 反差 / 身份 / 利害）
    2 追加进 学习/样本库.jsonl（长期积累，可回溯）
    3 对照我们自己文章的 style_check 分数，指出差在哪一维

为什么这么设计：平台不给推流数据，能观察的只有「哪些标题被顶到了热榜上」。
样本攒够 30 天，才能说「哪种结构在我们这个号上有效」——在那之前都是猜。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

HEAT_DIR = ROOT / "雷达"
LEARN_DIR = ROOT / "学习"

# 结构标签：热榜上能打的标题，通常至少命中两个
TAGS = {
    "数字": r"\d|[一二三四五六七八九十]+[个条种步页天年]",
    "疑问": r"[？?]|怎么|为什么|如何|该不该|要不要",
    "反差": r"反而|其实|居然|竟然|没想到|不是.*而是|越.*越",
    "身份": r"普通人|打工人|新手|小白|中年|宝妈|老板|同事|领导|学生",
    "利害": r"省钱|赚钱|失败|赔|坑|翻车|被裁|涨薪|避坑|后悔|血亏",
}


def tag_of(title: str) -> list[str]:
    return [k for k, pat in TAGS.items() if re.search(pat, title)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="每日标题样本")
    ap.add_argument("--date", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    day = args.date or _date.today().isoformat()
    src = HEAT_DIR / f"public-heat-{day}.json"
    if not src.exists():
        print(f"没有 {src.name}。先跑：python scripts/public_heat.py", file=sys.stderr)
        return 2

    heat = json.loads(src.read_text(encoding="utf-8"))
    hits = heat.get("ai_hits") or []
    rows = []
    for h in hits:
        tags = tag_of(h.get("title", ""))
        rows.append({
            "date": day, "source": h.get("source"), "rank": h.get("rank"),
            "title": h.get("title"), "tags": tags, "n_tags": len(tags),
        })
    rows.sort(key=lambda r: (-r["n_tags"], r["rank"]))

    LEARN_DIR.mkdir(exist_ok=True)
    with (LEARN_DIR / "样本库.jsonl").open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    md = [f"# 标题样本 {day}", "", f"来源：微博/知乎热榜，AI 相关 {len(rows)} 条。", ""]
    if rows:
        md += ["| 平台 | 位次 | 标题 | 结构标签 |", "|---|---|---|---|"]
        for r in rows:
            md.append(f"| {r['source']} | {r['rank']} | {r['title'][:38]} | {'、'.join(r['tags']) or '—'} |")
        best = rows[0]
        md += ["", "## 今天可借用的结构", "",
               f"命中标签最多的一条：**{best['title'][:40]}**（{'、'.join(best['tags'])}）", "",
               "用法：不是抄它的题目，是抄它的**结构**——换成我们自己的题材再套一遍。"]
    else:
        md += ["今天大众热榜里没有 AI 话题。别硬蹭，按行业源选。"]

    # 自己的文章分数（同一批比对）
    drafts = sorted((ROOT / "稿件").glob("*/manifest.json"))[-3:]
    if drafts:
        md += ["", "## 我们自己的分数", ""]
        import subprocess

        for m in drafts:
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "style_check.py"),
                                "--manifest", str(m), "--json"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            try:
                d = json.loads(r.stdout)
            except Exception:
                continue
            md.append(f"- {m.parent.name}：{d.get('score')} 分，角度 {'/'.join(d.get('angles') or []) or '—'}")

    out = LEARN_DIR / f"样本-{day}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps({"date": day, "rows": rows, "file": str(out)}, ensure_ascii=False, indent=2))
    else:
        print(f"✅ {out.relative_to(ROOT)}  AI 相关 {len(rows)} 条")
        for r in rows[:5]:
            print(f"   [{r['source']}] {r['title'][:36]}  ← {'、'.join(r['tags']) or '无标签'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
