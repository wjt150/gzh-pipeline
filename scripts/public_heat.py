#!/usr/bin/env python3
"""大众热榜采集：微博 / 知乎。

为什么单独一个脚本：AI 选题雷达（ai-content-radar）扫的是 15 个行业源，回答
「行业发生了什么」；这个脚本回答另一个问题——**普通人此刻在关心什么**。

对「教普通人用 AI」这个定位，后者是选题校准用的：写作素材还得从行业源里来，
但题目该挂在大家的注意力上，否则写得再好也没人点。

    python scripts/public_heat.py              # 抓一次，写 雷达/public-heat-<date>.json
    python scripts/public_heat.py --json       # 只打印
    python scripts/public_heat.py --dry-run    # 只打印，不落盘

数据源用 newsnow 的公开聚合接口（TrendRadar 也是这么做的），**必须绕代理直连**，
走本机环境代理会 502——和量子位 RSS 同一个坑。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "雷达"

ENDPOINTS = {
    "weibo": "https://newsnow.busiyi.world/api/s?id=weibo",
    "zhihu": "https://newsnow.busiyi.world/api/s?id=zhihu",
}

# 命中任一即视为「AI 相关」。宁可宽一点：大众热榜里 AI 话题本来就少，
# 漏掉一个就少一次校准机会。
AI_WORDS = [
    "AI", "人工智能", "大模型", "模型", "机器人", "智能体", "算法", "算力", "芯片",
    "ChatGPT", "GPT", "DeepSeek", "豆包", "Kimi", "Claude", "Gemini", "文心", "通义",
    "生成式", "换脸", "数字人", "深度伪造", "自动驾驶", "具身智能",
]


def fetch(name: str) -> list[dict]:
    """抓一个平台。必须绕代理：本机代理对这类域名返回 502。"""
    url = ENDPOINTS[name]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    items = data.get("items") or []
    out = []
    for i, it in enumerate(items):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "rank": i + 1,
            "title": title,
            "url": it.get("url") or "",
            "extra": (it.get("extra") or {}).get("info") or "",
        })
    return out


def is_ai(title: str) -> bool:
    low = title.lower()
    return any(w.lower() in low for w in AI_WORDS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="大众热榜（微博/知乎）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="不落盘")
    ap.add_argument("--top", type=int, default=10, help="每个平台保留前 N 条")
    args = ap.parse_args(argv)

    today = datetime.now().strftime("%Y-%m-%d")
    result: dict = {"date": today, "fetched_at": datetime.now().isoformat(timespec="seconds"), "sources": {}}
    ai_hits: list[dict] = []

    for name in ENDPOINTS:
        try:
            items = fetch(name)[: args.top]
            result["sources"][name] = items
            for it in items:
                if is_ai(it["title"]):
                    ai_hits.append({**it, "source": name})
        except Exception as e:  # noqa: BLE001
            result["sources"][name] = []
            result.setdefault("errors", []).append(f"{name}: {type(e).__name__}: {e}")

    result["ai_hits"] = ai_hits

    if not args.dry_run:
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / f"public-heat-{today}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    for name, items in result["sources"].items():
        label = {"weibo": "微博热搜", "zhihu": "知乎热榜"}.get(name, name)
        print(f"\n【{label}】前 {len(items)} 条")
        for it in items:
            mark = "★" if is_ai(it["title"]) else " "
            print(f" {mark} {it['rank']:>2}. {it['title'][:40]}")

    print(f"\nAI 相关命中 {len(ai_hits)} 条")
    for it in ai_hits:
        print(f"   · [{it['source']}] {it['title'][:44]}")

    if not ai_hits:
        print("   今天大众热榜里没有明显的 AI 话题——选题更需要靠行业源，别硬蹭。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
