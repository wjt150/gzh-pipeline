#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单篇正文抽取 —— 「AI 选题雷达」第 1 步的补充（RSS 只给标题，要点靠这一步）。

用法示例：
    python fetch_article.py --url "https://www.infoq.cn/article/xxx" --url "https://www.qbitai.com/..."
    python fetch_article.py --url "https://mp.weixin.qq.com/s/xxx" --cache-dir "../../.cache" --chars 1200

行为：
  * 抽标题 + 正文纯文本，正文落到缓存目录（默认 <工作区根>/.cache/articles/），
    文件名 = URL 的 sha1 前 16 位，**同一篇不重复抓**（命中缓存直接返回）。
  * 每条结果在 stdout 打一段摘要（标题 + 前 --chars 字），便于批量浏览后写要点。
  * 抽取分两步：先试 WordPress REST 接口（`<origin>/wp-json/wp/v2/posts/<id>?_embed=1`，
    能解决 JS 动态加载的站点，如新智元），不适用或正文太短再退回直接抓 HTML。
  * 只依赖标准库。

实测（2026-10-16）：InfoQ 9637 字、量子位 5367 字、新智元 3395 字（走 wp-rest）。

为什么要有它：
  * RSS 通道给不出可用要点：InfoQ 的 description 只有图片/链接标记，
    量子位的 description 仅标题级。要写「要点 2–3 条」必须读正文。
  * 但正文抓取是**有风控成本**的（公众号连续访问会触发「环境异常」验证页），
    所以本脚本只对**已选中的少数条目**单篇调用，不要拿它批量刷候选。
  * 落盘缓存是硬要求：读过的正文立即存下来，后续成稿阶段直接复用，不二次抓取。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

UA_PROFILES: list[dict[str, str]] = [
    {"User-Agent": "Mozilla/5.0"},
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    },
]

# 正文容器线索，按命中优先级排列。js_content 是微信图文正文的固定 id。
CONTAINER_HINTS = (
    'id="js_content"',
    'class="rich_media_content',
    'id="js_article"',
    "<article",
    'class="article-content',
    'class="post-content',
    'class="entry-content',
    'class="content"',
    'id="content"',
    "class='content'",
)

DROP_RE = re.compile(
    r"<(script|style|noscript|iframe|svg|head|nav|footer|form)\b.*?</\1>",
    re.I | re.S,
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
NL_RE = re.compile(r"\n{2,}")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OGTITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', re.I | re.S
)
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def clean_text(raw: str) -> str:
    text = COMMENT_RE.sub(" ", raw)
    text = DROP_RE.sub(" ", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text)
    text = NL_RE.sub("\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def extract(raw_html: str) -> dict:
    title = ""
    for regex in (OGTITLE_RE, H1_RE, TITLE_RE):
        m = regex.search(raw_html)
        if m:
            candidate = html.unescape(TAG_RE.sub("", m.group(1))).strip()
            if candidate and len(candidate) > 4:
                title = candidate
                break

    body_html = raw_html
    for hint in CONTAINER_HINTS:
        pos = raw_html.find(hint)
        if pos != -1:
            # 线索可能落在开标签内部，跳到该标签结束后再取，
            # 否则会把 "class=..." 这类残片带进正文（实测量子位会出现）
            gt = raw_html.find(">", pos)
            start = gt + 1 if gt != -1 else pos
            body_html = raw_html[start : start + 120_000]
            break

    body = clean_text(body_html)
    if len(body) < 200:  # 容器线索没命中或命中错，退回全文
        body = clean_text(raw_html)
    return {"title": title, "body": body}


def wp_api_url(url: str) -> str | None:
    """若该站是 WordPress，返回其文章 REST 接口地址，否则 None。

    为什么需要：新智元（aiera.com.cn）与量子位（qbitai.com）都是 WordPress，
    正文由 JS 调 `/wp-json/wp/v2/posts/<id>?_embed=1` 取得。直接抓 HTML：
    新智元只能拿到「正在取这篇稿子…」占位符（实测 105 字），量子位文章页
    返回 403。走这个接口两站都能拿到完整正文（实测 3395 / 4441 字）。

    ID 取法：从路径**倒序**找第一个「去掉扩展名后全是数字」的片段——
    新智元形如 `.../admin/118824/<slug>/`，量子位形如 `/2026/10/503446.html`。
    """
    m = re.match(r"^(https?://[^/]+)", url)
    if not m:
        return None
    origin = m.group(1)
    path = url[len(origin) :].split("?")[0]
    for seg in reversed([s for s in path.split("/") if s]):
        stem = seg.split(".")[0]
        if stem.isdigit() and len(stem) >= 3:
            return f"{origin}/wp-json/wp/v2/posts/{stem}?_embed=1"
    return None


def from_wp_api(url: str, timeout: float, retries: int, retry_delay: float) -> dict | None:
    api = wp_api_url(url)
    if not api:
        return None
    text, ua = fetch(api, timeout, retries, retry_delay)
    payload = json.loads(text)
    title = clean_text((payload.get("title") or {}).get("rendered", "")).replace("\n", " ").strip()
    body = clean_text((payload.get("content") or {}).get("rendered", ""))
    if len(body) < 200:
        return None
    return {"title": title, "body": body, "ua": ua, "via": "wp-rest"}


def fetch(url: str, timeout: float, retries: int, retry_delay: float) -> tuple[str, str]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        profile = UA_PROFILES[min(attempt, len(UA_PROFILES) - 1)]
        headers = {"Accept": "text/html,application/xhtml+xml,*/*"}
        headers.update(profile)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            for encoding in ("utf-8", "gb18030", "gbk"):
                try:
                    return raw.decode(encoding), profile.get("User-Agent", "")
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "replace"), profile.get("User-Agent", "")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
    raise last if last else RuntimeError("unknown fetch error")


def workroot() -> Path:
    """定位工作区根目录：往上找第一个含「来源清单.md」的祖先目录。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "来源清单.md").exists():
            return parent
    return here.parents[4]  # <工作区根>/.workbuddy/skills/<技能>/scripts/本文件


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    default_cache = workroot() / ".cache" / "articles"

    ap = argparse.ArgumentParser(description="单篇正文抽取（AI 选题雷达 · 详情步）")
    ap.add_argument("--url", action="append", required=True, help="文章地址，可重复")
    ap.add_argument("--cache-dir", default=str(default_cache), help=f"正文缓存目录，默认 {default_cache}")
    ap.add_argument("--chars", type=int, default=900, help="每条在 stdout 打印多少字，默认 900")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--retry-delay", type=float, default=4.0)
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存，强制重新抓取")
    args = ap.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for idx, url in enumerate(args.url):
        if idx:
            time.sleep(1.5)  # 逐篇、低频
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        cache_file = cache_dir / f"{digest}.json"

        if cache_file.exists() and not args.no_cache:
            record = json.loads(cache_file.read_text(encoding="utf-8"))
            record["cached"] = True
        else:
            try:
                data = None
                try:
                    data = from_wp_api(url, args.timeout, args.retries, args.retry_delay)
                except Exception:
                    data = None  # WP 接口不可用（站点不是 WP / 被限流），退回抓 HTML
                if data is None:
                    raw, ua = fetch(url, args.timeout, args.retries, args.retry_delay)
                    page = extract(raw)
                    data = {
                        "title": page["title"],
                        "body": page["body"],
                        "ua": ua,
                        "via": "html",
                    }
                record = {
                    "url": url,
                    "title": data["title"],
                    "body": data["body"],
                    "chars": len(data["body"]),
                    "ua": data["ua"],
                    "via": data["via"],
                    "ok": True,
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001
                record = {
                    "url": url,
                    "title": "",
                    "body": "",
                    "chars": 0,
                    "ua": None,
                    "via": None,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:180],
                }
            cache_file.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            record["cached"] = False

        results.append(record)

        print("=" * 78)
        if record["ok"]:
            tag = "缓存" if record.get("cached") else "新抓"
            print(
                f"[{tag}/{record.get('via') or '-'}] {record['title'] or '(无标题)'}  ({record['chars']} 字)"
            )
            print(f"URL: {url}")
            print("-" * 78)
            print(record["body"][: args.chars])
        else:
            print(f"[FAIL] {url}\n       {record['error']}")
        print()

    ok = sum(1 for r in results if r["ok"])
    print(f"—— {ok}/{len(results)} 篇成功；正文缓存于 {cache_dir} ——", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
