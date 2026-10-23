#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多源 RSS 抓取与归一化 —— 「AI 选题雷达」第 1 步（采集）用。

用法示例：
    python fetch_feeds.py --hours 36 --out "../../.cache/radar-2026-10-16-raw.json"
    python fetch_feeds.py --feed https://example.com/feed --limit 20

行为：
  * 源清单来自 <技能目录>/sources.json 里 channel=rss 或 channel=aihot 的条目；找不到配置文件才用内置兜底。
  * channel=aihot 走 AIHOT（aihot.news）的公开 JSON 接口，返回里自带中文摘要、AI 评分和推荐理由，
    归一化成与 RSS 相同的候选结构，额外挂在 item["aihot"] 下。
  * 每个源独立抓取，单源失败只记进报告，不影响其他源。
  * 归一化后的 JSON 打到 stdout（给了 --out 就写文件）；
    人类可读的抓取报告打到 stderr，便于一眼看清哪个源挂了。
  * 只依赖标准库（urllib + xml.etree），不需要 pip 装任何东西。

设计约束（对应 SKILL.md 的硬规则）：
  * 不猜测、不拼接链接：link 一律取自 feed 里的原始字段。
  * 不跨源去重：同一事件被多家报道是有价值的信息（雷达要合并成一张卡片），
    只做「完全同链接去重」和「标题归一化打组」，把模糊合并留给人工判断。
  * 失败重试带退避，并**轮换 User-Agent**：实测（2026-10-16）量子位会把完整浏览器 UA
    判为爬虫返回 403，而极简 UA 三个源全放行，因此先用极简 UA、失败再换浏览器 UA。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

CST = timezone(timedelta(hours=8))

# 配置文件缺失时的兜底源（2026-10-16 实测可用）
FALLBACK_FEEDS = [
    {"name": "量子位", "url": "https://www.qbitai.com/feed"},
    {"name": "新智元", "url": "https://aiera.com.cn/feed"},
    {"name": "AI前线·InfoQ", "url": "https://www.infoq.cn/feed"},
]

# 请求身份按顺序尝试。实测（2026-10-16）量子位会把完整浏览器 UA 判为爬虫返回 403，
# 而极简 UA 三个源全放行，所以极简 UA 排第一；失败再换浏览器 UA 兜底。
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

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

# --- AIHOT（aihot.news）公开接口 ---------------------------------------------
# 2026-10-19 实测：匿名只读、无需 Key，同 IP 约 60 次/分，超限返回 429 + Retry-After。
# 官方要求程序化取数时 UA 用 aihot-api/2.0.0；旧域名 aihot.virxact.com 于 2026-10-31 停用。
AIHOT_DEFAULT_URL = "https://aihot.news/api/v1/items"
AIHOT_UA = "aihot-api/2.0.0"
# 它只有 24h / 7d 两个原生窗口；要 36h、72h 这类窗口，先取 7d 再本地按 publishedAt 截断。
AIHOT_WINDOW = "7d"
# 只清跟踪参数，不动业务参数（如新智元 asi-item.html?id=xxx 里的 id）
TRACK_RE = re.compile(r"([?&])(utm_[^=&#]+|from|spm|share_token)=[^&#]*", re.I)
# 标题归一化：去掉一切非字母数字汉字的字符，用于打组
KEY_RE = re.compile(r"[^\w\u4e00-\u9fff]+")

ATOM = "{http://www.w3.org/2005/Atom}"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"

DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def clean_text(raw: str | None) -> str:
    """去掉 HTML 标签、反转义实体、压缩空白。"""
    if not raw:
        return ""
    text = html.unescape(raw)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return WS_RE.sub(" ", text).strip()


def strip_tracking(link: str) -> str:
    link = (link or "").strip()
    if not link:
        return ""
    link = TRACK_RE.sub(r"\1", link)
    return link.rstrip("?&#")


def title_key(title: str) -> str:
    return KEY_RE.sub("", title).lower()


def parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        for fmt in DATE_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def fetch(
    url: str,
    timeout: float,
    retries: int,
    retry_delay: float,
    profiles: list[dict[str, str]] | None = None,
    bypass_proxy: bool = False,
) -> tuple[bytes, str]:
    """抓取单个 URL，返回 (内容, 实际生效的 User-Agent)。

    重试时**轮换 UA**：实测量子位会把完整浏览器 UA 判成爬虫返回 403，而极简 UA 放行。
    重试之间等待 retry_delay * n 秒（退避），避免把限流打成永久封禁。

    bypass_proxy=True 时绕开环境代理：实测（2026-10-19）本机代理对 qbitai.com 返回
    502 Tunnel connection failed，直连则 200，属代理侧问题而非目标站封禁。
    """
    chain = profiles or UA_PROFILES
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if bypass_proxy
        else None
    )
    last: Exception | None = None
    for attempt in range(retries + 1):
        profile = chain[min(attempt, len(chain) - 1)]
        headers = {
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
        }
        headers.update(profile)
        try:
            req = urllib.request.Request(url, headers=headers)
            if opener is not None:
                with opener.open(req, timeout=timeout) as resp:
                    return resp.read(), profile.get("User-Agent", "")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), profile.get("User-Agent", "")
        except Exception as exc:  # noqa: BLE001 - 统一收集后一次性抛出
            last = exc
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
    raise last if last else RuntimeError("unknown fetch error")


def fetch_json(
    url: str,
    timeout: float,
    retries: int,
    retry_delay: float,
    ua: str = AIHOT_UA,
) -> dict:
    """取 JSON。失败按退避重试；429 时优先按 Retry-After 等待。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": ua, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if attempt < retries:
                wait = retry_delay * (attempt + 1)
                if exc.code == 429:
                    try:
                        wait = max(wait, float(exc.headers.get("Retry-After") or 0))
                    except (TypeError, ValueError):
                        pass
                time.sleep(min(wait, 60))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
    raise last if last else RuntimeError("unknown fetch error")


def fetch_aihot(
    feed: dict,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> list[dict]:
    """抓 AIHOT 的 items 接口，归一化成与 parse_feed 同构的行。

    额外保留 item["aihot"]：它自带的中文摘要、评分、推荐理由与原文链接。
    注意 summary/reason 是 AIHOT 的原创文字，只能当选题线索，不能搬进成稿。
    """
    base = (feed.get("url") or AIHOT_DEFAULT_URL).strip()
    mode = str(feed.get("mode") or "selected").lower()
    limit = int(feed.get("limit") or 30)
    query = urllib.parse.urlencode(
        {"window": AIHOT_WINDOW, "limit": limit, "mode": mode}
    )
    payload = fetch_json(
        f"{base}?{query}", timeout, retries, retry_delay, feed.get("ua") or AIHOT_UA
    )

    rows: list[dict] = []
    for it in payload.get("items") or []:
        title = clean_text(it.get("title"))
        if not title:
            continue
        links = it.get("links") or {}
        link = strip_tracking(links.get("original") or links.get("aihot") or "")
        published_iso = ""
        raw_pub = it.get("publishedAt") or ""
        if raw_pub:
            try:
                published_iso = (
                    datetime.fromisoformat(str(raw_pub).replace("Z", "+00:00"))
                    .astimezone(CST)
                    .isoformat()
                )
            except ValueError:
                published_iso = ""
        rows.append(
            {
                "title": title,
                "link": link,
                "date_raw": raw_pub,
                "published_iso": published_iso,
                "desc": clean_text(it.get("summary"))[:300],
                "aihot": {
                    "item_url": links.get("aihot") or "",
                    "source_name": (it.get("source") or {}).get("name") or "",
                    "original_title": it.get("originalTitle") or "",
                    "category": it.get("category"),
                    "score": it.get("score"),
                    "selected": it.get("selected"),
                    "reason": it.get("reason") or "",
                },
            }
        )
    return rows


def parse_feed(data: bytes) -> list[dict]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # 实测（2026-10-19）新智元 feed 偶发混入非法字节/截断的多字节字符，
        # 直接解析会整源失败。按 UTF-8 有损解码并剔除 XML 非法控制字符后重解析，
        # 代价只是个别文字出现替换符，不丢整源。
        text = data.decode("utf-8", errors="replace")
        text = "".join(
            ch for ch in text if ch in "\t\n\r" or ord(ch) >= 0x20
        )
        root = ET.fromstring(text)
    entries = root.findall(".//item")
    if not entries:
        entries = root.findall(f".//{ATOM}entry")

    rows: list[dict] = []
    for entry in entries:
        title = entry.findtext("title") or entry.findtext(f"{ATOM}title") or ""

        link = entry.findtext("link") or ""
        if not link:
            le = entry.find(f"{ATOM}link")
            if le is not None:
                link = le.get("href") or ""

        date_raw = (
            entry.findtext("pubDate")
            or entry.findtext("date")
            or entry.findtext(DC_DATE)
            or entry.findtext(f"{ATOM}updated")
            or entry.findtext(f"{ATOM}published")
        )

        desc = (
            entry.findtext("description")
            or entry.findtext(CONTENT)
            or entry.findtext("summary")
            or entry.findtext(f"{ATOM}summary")
        )

        rows.append(
            {
                "title": clean_text(title),
                "link": strip_tracking(link),
                "date_raw": (date_raw or "").strip(),
                "desc": clean_text(desc)[:220],
            }
        )
    return rows


def hn_url(feed: dict) -> str:
    """Hacker News 的 Algolia 公开检索接口地址（无需 Key）。"""
    query = str(feed.get("query") or "AI")
    min_points = int(feed.get("min_points") or 80)
    lookback = float(feed.get("lookback_hours") or 168)
    since = int(time.time()) - int(lookback * 3600)
    params = urllib.parse.urlencode(
        {
            "query": query,
            "tags": "story",
            "numericFilters": f"points>{min_points},created_at_i>{since}",
            "hitsPerPage": str(int(feed.get("limit") or 30)),
        }
    )
    return f"https://hn.algolia.com/api/v1/search?{params}"


def parse_hn(data: bytes) -> list[dict]:
    """把 HN 检索结果转成与 RSS 一致的 row 结构。

    为什么值得单独做一个通道（2026-10-19 实测）：RSS 按**发布时间**排序，
    HN 按**社区投票**排序。对「哪篇值得写」这个判断，热度是更强的信号——
    72 小时内 points>80 的 AI 帖只有十几条，等于替我们做了一轮筛选。
    """
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    rows: list[dict] = []
    for hit in obj.get("hits") or []:
        title = clean_text(hit.get("title") or "")
        if not title:
            continue
        link = (hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}").strip()
        created = re.sub(r"\.\d+Z$", "Z", (hit.get("created_at") or "").strip())
        points = hit.get("points") or 0
        desc = clean_text(hit.get("story_text") or "")[:200]
        prefix = f"[HN {points} 分] " if points else ""
        rows.append(
            {
                "title": title,
                "link": strip_tracking(link),
                "date_raw": created,
                "desc": (prefix + desc).strip()[:220],
            }
        )
    return rows


def load_feeds(
    config_path: Path | None, explicit: list[str]
) -> tuple[list[dict], str]:
    """返回 ([{name, url, adjust, channel, ...}], 来源说明)。

    adjust 是该源的发布时间校正小时数（见 sources.json 的 tz_adjust_hours）。
    只收 channel=rss 与 channel=aihot 的条目；web / manual 只作登记，不进采集。
    --feed 优先于配置文件，配置文件优先于内置兜底。
    """
    if explicit:
        return [
            {"name": f"自定义源{i + 1}", "url": u, "adjust": 0.0, "channel": "rss"}
            for i, u in enumerate(explicit)
        ], "--feed 参数"

    if config_path and config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            entries = [
                {
                    "name": str(e.get("name") or e.get("url") or ""),
                    "url": str(e.get("url") or ""),
                    "adjust": float(e.get("tz_adjust_hours") or 0),
                    "channel": str(e.get("channel") or "rss").lower(),
                    "mode": e.get("mode"),
                    "limit": e.get("limit"),
                    "query": e.get("query"),
                    "min_points": e.get("min_points"),
                    "lookback_hours": e.get("lookback_hours"),
                    "ua": e.get("ua"),
                    "bypass_proxy": bool(e.get("bypass_proxy")),
                    "lang": str(e.get("lang") or "zh").lower(),
                }
                for e in cfg.get("feeds", [])
                if str(e.get("channel", "rss")).lower() in ("rss", "aihot", "hn")
                and (e.get("url") or e.get("channel") == "rss")
            ]
            if entries:
                return entries, str(config_path)
        except Exception as exc:  # noqa: BLE001
            print(f"  [WARN] 配置文件读取失败，改用内置源：{type(exc).__name__}: {exc}", file=sys.stderr)

    return [{**e, "adjust": 0.0} for e in FALLBACK_FEEDS], "内置兜底源"


def collect(
    feeds: list[dict],
    hours: float,
    timeout: float,
    retries: int,
    retry_delay: float,
    profiles: list[dict[str, str]] | None = None,
) -> tuple[list[dict], list[dict]]:
    now = datetime.now(CST)
    cutoff = now - timedelta(hours=hours)
    report: list[dict] = []
    items: list[dict] = []
    seen_links: set[str] = set()

    for idx, feed in enumerate(feeds):
        source, url, adjust = feed["name"], feed["url"], float(feed.get("adjust") or 0)
        if idx:
            time.sleep(retry_delay)  # 源与源之间留间隔，别连发
        entry = {
            "source": source,
            "url": url,
            "ok": False,
            "fetched": 0,
            "kept": 0,
            "ua": None,
            "error": None,
        }
        try:
            if str(feed.get("channel") or "rss").lower() == "aihot":
                rows = fetch_aihot(feed, timeout, retries, retry_delay)
                entry["ua"] = feed.get("ua") or AIHOT_UA
            elif str(feed.get("channel") or "rss").lower() == "hn":
                data, used_ua = fetch(
                    hn_url(feed),
                    timeout,
                    retries,
                    retry_delay,
                    profiles,
                    bool(feed.get("bypass_proxy")),
                )
                rows = parse_hn(data)
                entry["ua"] = used_ua
            else:
                data, used_ua = fetch(
                    url,
                    timeout,
                    retries,
                    retry_delay,
                    profiles,
                    bool(feed.get("bypass_proxy")),
                )
                rows = parse_feed(data)
                entry["ua"] = used_ua
            entry["fetched"] = len(rows)
            for row in rows:
                if not row["title"]:
                    continue
                link = row["link"]
                if link and link in seen_links:
                    continue  # 完全同链接：同一篇文章重复出现
                if link:
                    seen_links.add(link)

                if row.get("published_iso"):
                    # AIHOT 的时间已按 UTC 正确带时区解析，不再走 date_raw 的时区猜测
                    try:
                        published = datetime.fromisoformat(row["published_iso"])
                    except ValueError:
                        published = parse_date(row["date_raw"])
                else:
                    published = parse_date(row["date_raw"])
                    if published is not None and adjust:
                        # 校正源侧时区标注错误（如 InfoQ 把北京时间标成 GMT）
                        published = published + timedelta(hours=adjust)
                age = None if published is None else (now - published).total_seconds() / 3600.0
                if published is not None and published < cutoff:
                    continue

                item = {
                    "source": source,
                    "title": row["title"],
                    "link": link,
                    "published": None if published is None else published.isoformat(),
                    "age_hours": None if age is None else round(age, 1),
                    "desc": row["desc"],
                    "title_key": title_key(row["title"]),
                    "same_title_in": [],
                    "lang": feed.get("lang") or "zh",
                }
                if row.get("aihot"):
                    item["aihot"] = row["aihot"]
                items.append(item)
                entry["kept"] += 1
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"[:180]
        report.append(entry)

    # 标注「同一标题出现在多个源」——事件聚合最省事的抓手
    by_key: dict[str, list[dict]] = {}
    for item in items:
        by_key.setdefault(item["title_key"], []).append(item)
    for group in by_key.values():
        sources = {g["source"] for g in group}
        if len(group) < 2 or len(sources) < 2:
            continue
        for item in group:
            item["same_title_in"] = sorted(s for s in sources if s != item["source"])

    items.sort(
        key=lambda x: (x["published"] is None, -(x["age_hours"] if x["age_hours"] is not None else 0))
    )
    return items, report


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    default_config = Path(__file__).resolve().parent.parent / "sources.json"

    ap = argparse.ArgumentParser(description="多源 RSS 抓取与归一化（AI 选题雷达 · 采集步）")
    ap.add_argument("--feed", action="append", default=[], help="临时追加 RSS 地址，可重复；给了就只抓这些")
    ap.add_argument("--config", default=None, help=f"源清单 JSON 路径，默认 {default_config}")
    ap.add_argument("--hours", type=float, default=36.0, help="时间窗（小时），默认 36")
    ap.add_argument("--limit", type=int, default=200, help="最多输出多少条，默认 200")
    ap.add_argument("--timeout", type=float, default=25.0, help="单请求超时秒数，默认 25")
    ap.add_argument("--retries", type=int, default=2, help="单源失败重试次数（每轮换一次 UA），默认 2")
    ap.add_argument("--retry-delay", type=float, default=4.0, help="重试/换源之间的间隔秒数，默认 4")
    ap.add_argument("--ua", default=None, help="指定首选 User-Agent，覆盖内置的极简 UA")
    ap.add_argument("--out", default=None, help="结果 JSON 的落盘路径；不给则打到 stdout")
    args = ap.parse_args(argv)

    config_path = Path(args.config) if args.config else default_config
    feeds, origin = load_feeds(config_path, args.feed)
    profiles = [
        {"User-Agent": args.ua},
        *[p for p in UA_PROFILES if p.get("User-Agent") != args.ua],
    ] if args.ua else UA_PROFILES

    items, report = collect(feeds, args.hours, args.timeout, args.retries, args.retry_delay, profiles)
    items = items[: args.limit]

    generated = datetime.now(CST).isoformat(timespec="seconds")
    payload = {
        "generated_at": generated,
        "window_hours": args.hours,
        "feed_source": origin,
        "feeds": report,
        "count": len(items),
        "items": items,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.out:
        parent = os.path.dirname(os.path.abspath(args.out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)

    print(f"—— 采集报告（{generated}）源清单：{origin} ——", file=sys.stderr)
    for entry in report:
        if entry["ok"]:
            print(
                f"  [OK]   {entry['source']}：抓到 {entry['fetched']} 条，窗内保留 {entry['kept']} 条"
                f"（UA: {entry['ua']}）",
                file=sys.stderr,
            )
        else:
            print(f"  [FAIL] {entry['source']}：{entry['error']}", file=sys.stderr)
    grouped = sum(1 for i in items if i["same_title_in"])
    print(f"  合计 {len(items)} 条候选；其中 {grouped} 条存在跨源同名（应合并成一张卡片）", file=sys.stderr)
    if args.out:
        print(f"  明细已写入 {args.out}", file=sys.stderr)

    return 0 if any(e["ok"] for e in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
