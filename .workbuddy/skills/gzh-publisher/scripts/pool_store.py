#!/usr/bin/env python3
"""选题池读写与三源解析（update_pool / backfill_pool / pick_one 共用）。

为什么要单独一层：
- 池子是 JSONL，多个脚本都会改它，必须**原子写 + 首次备份**，否则一次中断就毁池；
- 「星级 / 要点 / 语言 / AIHOT 分」分散在 HTML、raw.json、events.json 三个产物里，
  解析逻辑只能有一份，不然回填和增量入库会算出不同结果。

三源的分工：
    雷达/<date>.html        → rating(★1-3)、section、tag、track、points、display_title
    .cache/radar-*-raw.json → lang、desc、published
    .cache/radar-*-events.json → aihot_score、aihot_reason、member_count、category

⚠ 匹配一律**先按 URL**。实测 2026-10-19/20/21 三期，pool 的 title key 与 HTML 卡片
标题命中率 0/16（入库用原文标题、卡片用编辑重写标题），但 URL 命中 16/16。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 池子读写
# --------------------------------------------------------------------------- #
def load_pool(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def save_pool(path: Path, rows: list[dict], backup: bool = True) -> Path | None:
    """原子写：先写 .tmp 再 os.replace（Windows/POSIX 都是原子的）。

    首次改写前留一份 .bak-YYYYMMDD，同一天只留一份，避免把备份目录刷爆。
    """
    bak: Path | None = None
    if backup and path.exists():
        bak = path.with_suffix(path.suffix + f".bak-{datetime.now():%Y%m%d}")
        if not bak.exists():
            shutil.copy2(path, bak)
        else:
            bak = bak if bak.exists() else None
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return bak


# --------------------------------------------------------------------------- #
# 雷达 HTML
# --------------------------------------------------------------------------- #
CARD_SPLIT = re.compile(r'<div class="card">')
H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
H3_A_RE = re.compile(r'<h3[^>]*>.*?<a href="([^"]+)"[^>]*>(.*?)</a>', re.S)
STAR_RE = re.compile(r'class="star">(★+)')
TAG_RE = re.compile(r'class="tag [^"]*">([^<]+)</span>')
TRACK_RE = re.compile(r'data-track="([^"]+)"')
LI_RE = re.compile(r"<li>(.*?)</li>", re.S)
SRC_A_RE = re.compile(r'<div class="src">.*?</div>', re.S)
A_HREF_RE = re.compile(r'<a href="([^"]+)"')
TAG_STRIP = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    s = TAG_STRIP.sub("", s)
    s = s.replace("&amp;", "&").replace("&nbsp;", " ").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def parse_radar_html(path: Path) -> dict[str, dict]:
    """把一期雷达页解析成 {卡片URL: 元数据}。

    卡片 URL 取 <h3> 里的主链接（不是「原文」或「同题」）。section 按最近的上游 <h2> 归属。
    """
    text = path.read_text(encoding="utf-8")
    out: dict[str, dict] = {}

    # 预先算出每个 h2 的位置，便于给每张卡判定所属区块
    heads = [(m.start(), _clean(m.group(1))) for m in H2_RE.finditer(text)]
    heads = [(p, re.sub(r"^[^\w\u4e00-\u9fff]+", "", name).strip()) for p, name in heads]

    for m in re.split(r'(?=<div class="card">)', text):
        if not m.lstrip().startswith('<div class="card">'):
            continue
        card = m
        h3 = H3_A_RE.search(card)
        if not h3:
            continue
        url, title = h3.group(1), _clean(h3.group(2))
        pos = text.find(card[:80])
        section = ""
        for hp, name in heads:
            if hp <= pos:
                section = name
        star = STAR_RE.search(card)
        tag = TAG_RE.search(card)
        track = TRACK_RE.search(card)
        pts = [_clean(x) for x in LI_RE.findall(card)]
        src_div = SRC_A_RE.search(card)
        extra_urls = A_HREF_RE.findall(src_div.group(0)) if src_div else []
        out[url] = {
            "display_title": title,
            "rating": len(star.group(1)) if star else None,
            "section": section or None,
            "tag": _clean(tag.group(1)) if tag else None,
            "track": track.group(1) if track else None,
            "points": [p for p in pts if p][:5],
            "member_count": max(1, len(extra_urls)),
            "rating_src": str(path).replace("\\", "/"),
        }
    return out


def scan_radar_html(radar_dir: Path) -> dict[str, dict]:
    """按文件名排序扫全部雷达页，**后面的期覆盖前面的**（星级取最新一期）。"""
    merged: dict[str, dict] = {}
    for p in sorted(radar_dir.glob("*.html")):
        if not p.stem[:4].isdigit():  # 跳过 index.html 之类
            continue
        try:
            merged.update(parse_radar_html(p))
        except Exception:
            continue
    return merged


# --------------------------------------------------------------------------- #
# raw.json / events.json
# --------------------------------------------------------------------------- #
def collect_raw_meta(cache_dir: Path) -> dict[str, dict]:
    """{link: {lang, desc, published}}"""
    out: dict[str, dict] = {}
    for p in sorted(cache_dir.glob("radar-*-raw.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("items", [])
        for it in items:
            link = (it.get("link") or "").strip()
            if not link:
                continue
            rec = out.setdefault(link, {})
            rec.setdefault("lang", it.get("lang"))
            rec.setdefault("desc", (it.get("desc") or "")[:400] or None)
            rec.setdefault("published", it.get("published"))
    return out


def collect_event_meta(cache_dir: Path) -> dict[str, dict]:
    """{member link: {aihot_score, aihot_reason, member_count, category}}"""
    out: dict[str, dict] = {}
    for p in sorted(cache_dir.glob("radar-*-events.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        events = data if isinstance(data, list) else data.get("events", [])
        for ev in events:
            members = ev.get("members") or []
            for mb in members:
                link = (mb.get("link") or "").strip()
                if not link:
                    continue
                rec = out.setdefault(link, {})
                rec.setdefault("aihot_score", ev.get("aihot_score"))
                rec.setdefault("category", ev.get("category"))
                rec.setdefault("member_count", ev.get("member_count"))
                reason = (mb.get("aihot") or {}).get("reason")
                if reason:
                    rec.setdefault("aihot_reason", reason[:300])
    return out


def row_links(row: dict) -> list[str]:
    return [l.get("url", "").strip() for l in (row.get("links") or []) if l.get("url")]


def normalize_url(u: str) -> str:
    """去掉查询串里的追踪参数与尾斜杠，提高跨产物匹配率。"""
    u = u.strip()
    u = re.sub(r"[?&](utm_[^=]+|from|spm)=[^&]*", "", u)
    return u.rstrip("/")


def release_item(path: Path, key: str) -> bool:
    """把被选中的条目放回池子（稿件作废/过期时用）。

    为什么必须做：pick_one 把条目标成 picked，而 hard_rules 会拒绝 picked。
    如果不回滚，每次「写坏」「源抓不到」都在永久消耗选题池，池子会被慢慢掏空。

    只在 status 确实是 picked 时才动（防止误放别人标的条目）；返回是否真的改了。
    """
    if not key:
        return False
    rows = load_pool(path)
    hit = False
    for r in rows:
        if r.get("key") == key and r.get("status") == "picked":
            r["status"] = "new"
            r.pop("picked_at", None)
            r["released_at"] = datetime.now().isoformat(timespec="seconds")
            hit = True
    if hit:
        save_pool(path, rows)
    return hit
