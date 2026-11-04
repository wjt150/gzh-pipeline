#!/usr/bin/env python3
"""从原文页抓配图，做封面底图。

    python scripts/grab_image.py --url "https://mp.weixin.qq.com/s/xxx" --out 稿件/x/bg.jpg
    python scripts/grab_image.py --url "https://www.qbitai.com/2026/..." --out bg.jpg --dry-run

只抓**页面里声明的头图/主图**（og:image、twitter:image、微信 data-src 第一张），
不扫全站、不跟链接、不下载图集——版权风险和流量都不值得。

抓不到就老实返回失败，让上层退回纯文字版式。**不要用占位图凑**：
一张无关的配图比没有配图更糟，读者会以为那是新闻现场。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

# 量子位这类站点会拒绝完整浏览器 UA（雷达实测 403），所以轮换几个极简 UA
UAS = [
    "Mozilla/5.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
]

# 这些不是正文配图，抓了会得到 logo / 二维码 / 图标
BAD = re.compile(r"logo|icon|avatar|qrcode|qr_|sprite|placeholder|blank|1x1|pixel|emoji", re.I)


def fetch_html(url: str, timeout: int = 20) -> str:
    # 直连：这类站点走本机代理常 502（和量子位 RSS 同一个坑）
    class _Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            newurl = newurl.replace(" ", "%20")
            return urllib.request.Request(
                newurl, headers={"User-Agent": req.get_header("User-agent") or UAS[0],
                                 "Accept-Language": "zh-CN,zh;q=0.9"})

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirect())
    raw, last = b"", None
    for ua in UAS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua,
                                                       "Accept-Language": "zh-CN,zh;q=0.9"})
            with opener.open(req, timeout=timeout) as r:
                raw = r.read()
            break
        except Exception as e:  # noqa: BLE001
            last = e
    if not raw:
        raise last or RuntimeError("empty response")
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def find_image(html: str, base_url: str) -> tuple[str | None, str]:
    """返回 (图片地址, 命中的来源)。按可信度排序：og:image 最可靠。"""
    pats = [
        (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', "og:image"),
        (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', "og:image"),
        (r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', "twitter:image"),
        (r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)', "itemprop:image"),
        # 微信正文：<img data-src="..."> 第一张通常就是头图
        (r'<img[^>]+data-src=["\']([^"\']+)["\']', "wx:data-src"),
    ]
    for pat, tag in pats:
        for m in re.finditer(pat, html, re.I | re.S):
            u = m.group(1).strip()
            if not u or u.startswith("data:") or BAD.search(u):
                continue
            if u.startswith("//"):
                u = "https:" + u
            elif u.startswith("/"):
                mt = re.match(r"(https?://[^/]+)", base_url)
                if mt:
                    u = mt.group(1) + u
            elif not u.startswith("http"):
                continue
            return u, tag
    return None, "none"


def download(url: str, out: Path, timeout: int = 25) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": UAS[0], "Referer": url, "Accept": "image/*,*/*"})
    with opener.open(req, timeout=timeout) as r:
        data = r.read()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return len(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="抓原文头图做封面底图")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true", help="只报能不能抓到，不下载")
    args = ap.parse_args(argv)

    try:
        html = fetch_html(args.url)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "stage": "fetch", "reason": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    img, source = find_image(html, args.url)
    if not img:
        print(json.dumps({"ok": False, "stage": "find", "reason": "页面没有声明头图"},
                         ensure_ascii=False), file=sys.stderr)
        return 3

    if args.dry_run:
        print(json.dumps({"ok": True, "source": source, "image": img}, ensure_ascii=False, indent=2))
        return 0

    try:
        size = download(img, Path(args.out))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "stage": "download", "image": img,
                          "reason": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 4

    print(json.dumps({"ok": True, "source": source, "image": img,
                      "out": args.out, "bytes": size}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
