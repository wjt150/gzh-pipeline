#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从源文章正文里自动挑一张封面图。

用法：
    python gzh_cover.py --url "https://www.infoq.cn/article/xxx"
    python gzh_cover.py --url "..." --min-width 900 --save-dir "../../.cache/covers"

输出：stdout 打一段 JSON（含 cover_path / source_url / 尺寸 / 候选数 / 不选中的原因）。
找不到合适图时**不报错**（退出码 0，`ok=false`），让上游决定是否跳过封面——
微信在未设封面时会自动从正文首图取封面，所以"没有封面"不等于"发不出去"。

设计取舍（都是为了不引入依赖、不中断流水线）：
  * **纯标准库**。不做裁切/缩放：公众号后台接受各种尺寸，会按 2.35:1 自动裁；
    只要挑出的图本身就是横向宽图（宽高比 1.2–2.8），裁出来就不会难看。
    这样也免掉 Pillow 依赖（本机 managed python 没装）。
  * 只接受 png / jpeg / gif（后台封面 file input 的 accept 就这几种，webp 会被拒）。
  * 图片可能来自任意域名（官网源是 qbitai.com / infoq.cn，公众号源是 mmbiz.qpic.cn），
    因此 Referer 一律设成 mp.weixin.qq.com —— 这是 mmbiz 图床的防盗链要求，对别的站也无害。
  * 懒加载图片常把真实地址放在 data-src / data-original，优先取这些。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA_PROFILES = [
    {"User-Agent": "Mozilla/5.0"},
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    },
]

IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
# 通用属性（og:image 用的是 property/content，不在 SRC_PRIORITY 里）
ATTR_ANY_RE = re.compile(r"""([\w:-]+)\s*=\s*["']([^"']*)["']""", re.I)
ATTR_RE = re.compile(r"""(data-src|data-original|data-actualsrc|src)\s*=\s*["']([^"']+)["']""", re.I)
# 顺序即优先级。懒加载站常写成 <img src="缩略图" data-original="真图">，
# 若直接 re.search 会先匹配到靠左的 src，拿到的是小图（实测量子位 16 张全 <600px）。
SRC_PRIORITY = ("data-src", "data-original", "data-actualsrc", "src")

# 封面 file input 只收这几种；webp/svg 一律跳过
OK_EXT = (".jpg", ".jpeg", ".png", ".gif")

RATIO_MIN, RATIO_MAX = 1.2, 2.8


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def fetch(url: str, timeout: float = 25.0, referers: list[str] | None = None) -> bytes:
    """抓取。referers 按顺序尝试：mp.weixin.qq.com 是 mmbiz 图床的防盗链要求，
    但有些站只认自己域名的 Referer，所以失败后回退到源站 origin。"""
    refs = referers or ["https://mp.weixin.qq.com/"]
    last: Exception | None = None
    for referer in refs:
        for profile in UA_PROFILES:
            headers = {"Referer": referer, "Accept": "text/html,image/*,*/*"}
            headers.update(profile)
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
            except Exception as exc:  # noqa: BLE001
                last = exc
    raise last if last else RuntimeError("fetch failed")


def origin_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def image_dims(data: bytes) -> tuple[int, int, str] | None:
    """从图片头部读宽高，支持 png / jpeg / gif。返回 (w, h, kind)。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h, "png"

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w = int.from_bytes(data[6:8], "little")
        h = int.from_bytes(data[8:10], "little")
        return w, h, "gif"

    if data[:2] == b"\xff\xd8":  # JPEG：扫 SOF 段
        i = 2
        n = len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
            if 0xC0 <= marker <= 0xC3 or 0xC5 <= marker <= 0xC7 or 0xC9 <= marker <= 0xCB:
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                w = int.from_bytes(data[i + 7 : i + 9], "big")
                return w, h, "jpeg"
            i += 2 + seg_len
        return None

    return None


def extract_meta_images(html_text: str, base_url: str) -> list[str]:
    """优先取 og:image / twitter:image。

    为什么放在最前面：这两张是站点编辑专门为分享选的图，通常 1200×630，比例正好适合公众号封面；
    而正文里的图经常是懒加载缩略图、SVG 或代码截图，抽出来要么太小要么没法用。
    （实测：claude.com 的博客正文抽不到可用图，但 og:image 有。）
    """
    out: list[str] = []
    for tag in META_TAG_RE.findall(html_text):
        found: dict[str, str] = {}
        for name, val in ATTR_ANY_RE.findall(tag):
            found.setdefault(name.lower(), val)
        key = (found.get("property") or found.get("name") or "").strip().lower()
        if key not in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
            continue
        raw = (found.get("content") or "").strip()
        if not raw or raw.startswith("data:"):
            continue
        absolute = urllib.parse.urljoin(base_url, raw).split("#")[0]
        if absolute not in out:
            out.append(absolute)
    return out


def extract_img_urls(html_text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for tag in IMG_TAG_RE.findall(html_text):
        found: dict[str, str] = {}
        for name, val in ATTR_RE.findall(tag):
            found.setdefault(name.lower(), val)
        raw = ""
        for key in SRC_PRIORITY:  # 按优先级取，而不是按标签里的出现顺序
            if found.get(key, "").strip():
                raw = found[key].strip()
                break
        if not raw or raw.startswith("data:"):
            continue
        absolute = urllib.parse.urljoin(base_url, raw).split("#")[0]
        path = urllib.parse.urlparse(absolute).path.lower()
        if not path.endswith(OK_EXT):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)
    return urls


def decode_html(data: bytes) -> str:
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def score(width: int, height: int) -> tuple:
    """排序键：越靠前越好。要求横向宽图。"""
    ratio = width / height if height else 0
    ratio_ok = RATIO_MIN <= ratio <= RATIO_MAX
    return (
        0 if ratio_ok else 1,          # 宽高比合适的优先
        0 if width >= 900 else 1,      # 够宽的优先
        -(width * height),             # 面积大的优先
    )


def ffmpeg_exe() -> str | None:
    """只认 ffmpeg。

    ⚠ 绝不要回退到 `convert`：Windows 上 PATH 里的 convert.exe 是磁盘格式转换工具，
    调用它会有破坏性后果（ImageMagick 的才是图片工具）。
    """
    for name in ("ffmpeg", "ffmpeg.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def crop_to_ratio(
    src: Path, dst: Path, ratio: float = 2.35, size: tuple[int, int] = (900, 383)
) -> dict:
    """居中裁到指定宽高比再缩放到 size。先按已知尺寸算裁剪框，交给 ffmpeg 一次完成。"""
    try:
        data = src.read_bytes()
    except OSError as exc:
        return {"ok": False, "reason": f"读不到源图：{exc}"}
    dims = image_dims(data)
    if not dims:
        return {"ok": False, "reason": "解析不出源图尺寸"}
    w, h, _ = dims
    if w / h > ratio:  # 太宽 → 裁左右
        ch, cw = h, round(h * ratio)
    else:  # 太高 → 裁上下
        cw, ch = w, round(w / ratio)
    x, y = (w - cw) // 2, (h - ch) // 2

    exe = ffmpeg_exe()
    if not exe:
        return {"ok": False, "reason": "找不到 ffmpeg（npm/PATH 里都没有）"}
    vf = f"crop={cw}:{ch}:{x}:{y},scale={size[0]}:{size[1]}:flags=lanczos"
    try:
        proc = subprocess.run(
            [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-vf", vf, "-q:v", "2", str(dst)],
            capture_output=True, text=True, timeout=90,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"ffmpeg 调用失败：{exc}"}
    if proc.returncode != 0 or not dst.exists():
        return {"ok": False, "reason": (proc.stderr or "ffmpeg 返回非 0")[-200:]}
    return {
        "ok": True,
        "crop_box": [x, y, cw, ch],
        "from": f"{w}x{h}",
        "to": f"{size[0]}x{size[1]}",
        "cut_ratio_of_height": round(1 - ch / h, 3),
        "out": str(dst),
    }


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="从源文章里自动挑封面图（纯标准库）")
    ap.add_argument("--url", required=True, help="源文章地址")
    ap.add_argument("--min-width", type=int, default=600, help="最小宽度，默认 600")
    ap.add_argument("--max-probe", type=int, default=10, help="最多探测多少张图，默认 10")
    ap.add_argument("--save-dir", default=None, help="封面落盘目录，默认 <工作区>/.cache/covers")
    ap.add_argument(
        "--fit",
        default="none",
        choices=["none", "2.35"],
        help="裁到公众号头条比例（2.35:1 → 900×383，用 ffmpeg）",
    )
    ap.add_argument("--out", default=None, help="--fit 的输出路径，默认 <save-dir>/<digest>-fit.jpg")
    args = ap.parse_args(argv)

    workroot = Path(__file__).resolve().parents[1]
    save_dir = Path(args.save_dir).resolve() if args.save_dir else workroot / ".cache" / "covers"
    save_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "ok": False,
        "cover_path": None,
        "source_url": None,
        "width": None,
        "height": None,
        "candidates": 0,
        "reason": "",
    }

    try:
        html_text = decode_html(fetch(args.url))
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"抓取源文章失败：{type(exc).__name__}: {exc}"[:160]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    # og:image 优先，其次才是正文里的 <img>
    urls = extract_meta_images(html_text, args.url) + extract_img_urls(html_text, args.url)
    result["candidates"] = len(urls)
    if not urls:
        result["reason"] = "正文里没有 png/jpeg/gif 图片"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    probes: list[dict] = []
    article_origin = origin_of(args.url)
    for url in urls[: args.max_probe]:
        try:
            data = fetch(url, referers=["https://mp.weixin.qq.com/", article_origin])
        except Exception:  # noqa: BLE001
            continue
        dims = image_dims(data)
        if not dims:
            continue
        w, h, kind = dims
        if w < args.min_width or h < 120:
            continue
        probes.append({"url": url, "data": data, "w": w, "h": h, "kind": kind})

    if not probes:
        result["reason"] = f"探测了 {min(len(urls), args.max_probe)} 张图，没有一张达到 min_width={args.min_width}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    best = sorted(probes, key=lambda p: score(p["w"], p["h"]))[0]
    ext = ".png" if best["kind"] == "png" else (".gif" if best["kind"] == "gif" else ".jpg")
    digest = hashlib.sha1(best["url"].encode("utf-8")).hexdigest()[:16]
    cover_path = save_dir / f"{digest}{ext}"
    cover_path.write_bytes(best["data"])

    meta = {
        "source_url": best["url"],
        "article_url": args.url,
        "width": best["w"],
        "height": best["h"],
        "ratio": round(best["w"] / best["h"], 3) if best["h"] else None,
        "picked_from": len(probes),
    }
    (save_dir / f"{digest}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 裁到公众号头条比例。实测原文 og:image 常见 1.9:1（1200×630），微信要 2.35:1，
    # 偏差 18.9% 不通过，会被裁掉核心信息。用 ffmpeg 居中裁（零 pip 依赖）。
    fit_info: dict | None = None
    if args.fit != "none":
        target = Path(args.out).resolve() if args.out else save_dir / f"{digest}-fit.jpg"
        fit_info = crop_to_ratio(cover_path, target, ratio=2.35, size=(900, 383))
        if fit_info.get("ok"):
            cover_path = target

    result.update(
        {
            "ok": True,
            "cover_path": str(cover_path),
            "source_url": best["url"],
            "width": best["w"],
            "height": best["h"],
            "reason": f"从 {len(probes)} 张合格图里按（宽高比合适 > 够宽 > 面积大）选中",
        }
    )
    if fit_info is not None:
        result["fit"] = fit_info
        if fit_info.get("ok"):
            result["cover_path"] = str(cover_path)
            result["width"], result["height"] = 900, 383
            result["ratio"] = 2.35
        else:
            result["cover_warning"] = f"裁剪失败，仍用原图：{fit_info.get('reason')}"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
