#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一篇外文/中文原文，准备成「可以动笔」的素材包。

它**不写稿**。做的是三件确定性的事：
    1. 抓正文（复用雷达技能的 fetch_article.py，带缓存，同一 URL 不重复抓）
    2. 抽一张横图当封面（复用 gzh_cover.py）
    3. 落一份素材包：原文全文 .md + manifest 骨架 .json

之后由人（或上层的模型）读原文，写出中文稿的 body.html，再把 manifest 补全，
交给 gzh_auto.py 上传。

用法：
    python scripts/gzh_source.py --url "https://example.com/post"
    python scripts/gzh_source.py --url "https://a.com/1" --url "https://b.com/2" --lang en

产物（默认 .cache/sources/）：
    <hash>.md              原文全文（标题 / URL / 正文），直接用 Read 读
    <hash>.manifest.json   manifest 骨架，title 已填，summary/body 待补
    <hash>.cover.jpg       封面候选（若抽到）

边界（重要，别指望它替你决定能不能用）：
    * 全文翻译他人文章 = 涉及翻译权，即使注明来源也不等于已获授权。
    * 编译 / 摘译（用自己的话写要点、数据、背景，注明信息来源）风险显著更低。
    * 直接转载公众号文章 = 需原作者授权或「转载白名单」，未声明原创 ≠ 允许转载。
    本脚本只负责取素材，**用哪种方式成文由使用者判断**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def workroot() -> Path:
    """定位工作区根（可移植版）。

    优先级：环境变量 GZH_WORKROOT → 往上找带标记文件的祖先 → 当前目录 → 脚本上两级。
    标记文件用 3 个任选其一，这样换电脑/换目录名都不用改代码。
    """
    env = os.environ.get("GZH_WORKROOT")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    markers = ("来源清单.md", "排版规范.md", "组件库.md")
    here = Path(__file__).resolve()
    for base in (Path.cwd().resolve(), *here.parents):
        if any((base / m).exists() for m in markers):
            return base
    return here.parents[1] if len(here.parents) > 1 else Path.cwd()


def slug_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def load_cached_article(root: Path, url: str) -> dict | None:
    """fetch_article.py 的缓存：<工作区>/.cache/articles/<sha1前16>.json"""
    p = root / ".cache" / "articles" / f"{slug_for(url)}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def fetch_article(root: Path, url: str, timeout: int = 120) -> dict | None:
    """先查缓存，没有再调雷达技能里的抽取脚本。"""
    cached = load_cached_article(root, url)
    if cached:
        return cached

    script = root / ".workbuddy" / "skills" / "ai-content-radar" / "scripts" / "fetch_article.py"
    if not script.exists():
        raise SystemExit(f"找不到正文抽取脚本：{script}")
    try:
        subprocess.run(
            [sys.executable, str(script), "--url", url, "--chars", "120"],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    return load_cached_article(root, url)


def fetch_cover(root: Path, url: str, dest: Path) -> Path | None:
    """抽封面并复制到素材目录。失败返回 None（不阻断流程）。"""
    script = root / "scripts" / "gzh_cover.py"
    if not script.exists():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--url", url],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"(\S+\.(?:jpg|jpeg|png))", proc.stdout or "")
    if not m:
        # 脚本可能把路径打在 stderr
        m = re.search(r"(\S+\.(?:jpg|jpeg|png))", proc.stderr or "")
    if not m:
        return None
    src = Path(m.group(1))
    if not src.exists():
        return None
    dest.write_bytes(src.read_bytes())
    return dest


def build_markdown(article: dict, cover_name: str | None) -> str:
    body = (article.get("body") or "").strip()
    lines = [
        f"# {article.get('title') or '(无标题)'}",
        "",
        f"- 原文：{article.get('url')}",
        f"- 抓取字数：{len(body)}",
        f"- 封面候选：{cover_name or '未抽到'}",
        "",
        "> 以下是原文全文，供编译/摘译使用。成文时请用自己的话写，并在正文末尾注明信息来源。",
        "",
        "---",
        "",
        body,
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="把原文准备成可动笔的素材包（不写稿）")
    ap.add_argument("--url", action="append", required=True, help="原文地址，可重复")
    ap.add_argument("--out-dir", default=None, help="默认 <工作区>/.cache/sources")
    ap.add_argument("--lang", default="", help="源语言提示（en/zh…），只写进素材包，便于成文时参考")
    ap.add_argument("--no-cover", action="store_true", help="跳过封面抽取")
    args = ap.parse_args(argv)

    root = workroot()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else root / ".cache" / "sources"
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for url in args.url:
        slug = slug_for(url)
        art = fetch_article(root, url)
        if not art or not (art.get("body") or "").strip():
            results.append({"url": url, "ok": False, "error": "正文抓取失败或为空"})
            continue

        cover_path = None
        if not args.no_cover:
            cover_path = fetch_cover(root, url, out_dir / f"{slug}.cover.jpg")

        md_path = out_dir / f"{slug}.md"
        md_path.write_text(build_markdown(art, cover_path.name if cover_path else None), encoding="utf-8")

        manifest = {
            "title": art.get("title") or "",
            "summary": "",
            "body_html_path": "body.html",
            "cover_path": cover_path.name if cover_path else "",
        }
        man_path = out_dir / f"{slug}.manifest.json"
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        results.append(
            {
                "url": url,
                "ok": True,
                "chars": len(art.get("body") or ""),
                "source_md": str(md_path),
                "manifest": str(man_path),
                "cover": str(cover_path) if cover_path else None,
                "lang": args.lang,
            }
        )

    ok = sum(1 for r in results if r.get("ok"))
    print(json.dumps({"ok": ok == len(results), "sources": results}, ensure_ascii=False, indent=2))
    print(
        "\n下一步：读 <hash>.md 写中文稿 body.html（放在同目录），补全 manifest 的 summary，"
        "然后跑 gzh_auto.py。编译/摘译请注明信息来源。",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
