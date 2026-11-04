#!/usr/bin/env python3
"""生成封面底纹（SVG，纯代码画，不依赖版权图片）。

    python scripts/cover_bg.py --kind circuit --palette ink --out .cache/covers/bg.svg

为什么用代码画而不是找图：新智元那种封面靠的是「底纹有内容」——发光的电路走线、
点阵、光晕。这些本质上是几何图形，用 SVG 画比找图更可控：
尺寸精确、颜色能对齐品牌、不会撞版权、每篇随机种子不同就不会重样。

六类图案：
    circuit  电路走线 + 发光节点（最像新智元那种科技感）
    grid     网格 + 径向光
    dots     点阵疏密渐变
    waves    等高线波纹
    beams    斜向光带
    grain    细噪点叠深色渐变（最安静，适合文字多的封面）
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

W, H = 900, 383

PALETTES = {
    "ink":    {"bg": "#0a1018", "glow": "#4a8fe0", "line": "#4a8fe0", "fg": "#ffffff"},
    "paper":  {"bg": "#f2eee6", "glow": "#b5442d", "line": "#c4a89a", "fg": "#16181c"},
    "forest": {"bg": "#0d1a15", "glow": "#5fb37f", "line": "#3f7f5c", "fg": "#f2f6f3"},
    "brick":  {"bg": "#170f0c", "glow": "#e0743c", "line": "#8a4a2a", "fg": "#fbf3ea"},
}
KINDS = ["circuit", "grid", "dots", "waves", "beams", "grain"]


def _circuit(rng: random.Random, p: dict) -> str:
    """电路走线：从右侧随机起点向左/上折线走，末端点发光。"""
    parts = []
    for _ in range(rng.randint(7, 10)):
        x0 = rng.randint(420, W)
        y0 = rng.randint(20, H)
        d = [f"M{x0},{y0}"]
        x, y = x0, y0
        for _ in range(rng.randint(3, 5)):
            step = rng.choice([-1, 1]) * rng.randint(50, 150)
            x = max(20, min(W - 10, x + step))
            d.append(f"L{x},{y}")
            step = rng.choice([-1, 1]) * rng.randint(24, 70)
            y = max(10, min(H - 10, y + step))
            d.append(f"L{x},{y}")
        parts.append(
            f'<path d="{" ".join(d)}" fill="none" stroke="{p["line"]}" '
            f'stroke-opacity="{rng.uniform(.16, .38):.2f}" stroke-width="1.1"/>'
            f'<circle cx="{x}" cy="{y}" r="{rng.uniform(1.4, 2.6):.1f}" '
            f'fill="{p["glow"]}" fill-opacity="{rng.uniform(.45, .85):.2f}"/>'
        )
    # 散点微光
    for _ in range(rng.randint(28, 46)):
        parts.append(
            f'<circle cx="{rng.randint(380, W)}" cy="{rng.randint(0, H)}" '
            f'r="{rng.uniform(.8, 1.8):.1f}" fill="{p["glow"]}" '
            f'fill-opacity="{rng.uniform(.12, .42):.2f}"/>'
        )
    return "".join(parts)


def _grid(rng: random.Random, p: dict) -> str:
    g = []
    step = rng.choice([22, 26, 30])
    for x in range(0, W + step, step):
        g.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{H}"/>')
    for y in range(0, H + step, step):
        g.append(f'<line x1="0" y1="{y}" x2="{W}" y2="{y}"/>')
    return (f'<g stroke="{p["line"]}" stroke-opacity=".13" stroke-width="1">{"".join(g)}</g>'
            f'<rect x="{W*0.45}" y="0" width="{W*0.55}" height="{H}" fill="url(#fade)"/>')


def _dots(rng: random.Random, p: dict) -> str:
    parts = []
    step = 18
    for y in range(6, H, step):
        for x in range(int(W * 0.38), W, step):
            # 越靠右越亮，形成疏密层次
            t = (x - W * 0.38) / (W * 0.62)
            if rng.random() < 0.18 + t * 0.55:
                r = 0.9 + t * 1.6
                parts.append(f'<circle cx="{x}" cy="{y}" r="{r:.1f}" '
                             f'fill="{p["glow"]}" fill-opacity="{0.08 + t*0.5:.2f}"/>')
    return "".join(parts)


def _waves(rng: random.Random, p: dict) -> str:
    parts = []
    for i in range(rng.randint(8, 12)):
        base = H * (0.1 + i * 0.075)
        amp = rng.uniform(6, 22)
        d = [f"M0,{base:.0f}"]
        x = 0
        while x < W:
            x += rng.randint(60, 120)
            d.append(f"Q{x-30},{base + rng.uniform(-amp, amp):.0f} {x},{base:.0f}")
        parts.append(f'<path d="{" ".join(d)}" fill="none" stroke="{p["line"]}" '
                     f'stroke-opacity="{rng.uniform(.08, .26):.2f}" stroke-width="1"/>')
    return "".join(parts)


def _beams(rng: random.Random, p: dict) -> str:
    parts = []
    for i in range(rng.randint(4, 7)):
        x = rng.randint(int(W * 0.35), W + 120)
        w = rng.randint(50, 170)
        op = rng.uniform(.05, .16)
        parts.append(f'<polygon points="{x},0 {x+w},0 {x+w-rng.randint(60,140)},{H} '
                     f'{x-rng.randint(60,140)},{H}" fill="{p["glow"]}" fill-opacity="{op:.2f}"/>')
    return "".join(parts)


def _grain(rng: random.Random, p: dict) -> str:
    """细噪点：用上千个极小矩形做质感，比 flat 色块耐看。"""
    parts = []
    for _ in range(1400):
        parts.append(f'<rect x="{rng.uniform(0, W):.0f}" y="{rng.uniform(0, H):.0f}" '
                     f'width="1" height="1" fill="{p["fg"]}" '
                     f'fill-opacity="{rng.uniform(.02, .07):.3f}"/>')
    return "".join(parts)


DRAW = {"circuit": _circuit, "grid": _grid, "dots": _dots,
        "waves": _waves, "beams": _beams, "grain": _grain}


def build(kind: str, palette: str, seed: int) -> str:
    p = PALETTES.get(palette, PALETTES["ink"])
    rng = random.Random(seed)
    body = DRAW.get(kind, _circuit)(rng, p)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<defs>
  <radialGradient id="glow" cx="78%" cy="46%" r="62%">
    <stop offset="0%" stop-color="{p['glow']}" stop-opacity=".30"/>
    <stop offset="55%" stop-color="{p['glow']}" stop-opacity=".07"/>
    <stop offset="100%" stop-color="{p['glow']}" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="fade" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0%" stop-color="{p['bg']}" stop-opacity=".92"/>
    <stop offset="100%" stop-color="{p['bg']}" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="scrim" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0%" stop-color="{p['bg']}" stop-opacity=".96"/>
    <stop offset="42%" stop-color="{p['bg']}" stop-opacity=".72"/>
    <stop offset="72%" stop-color="{p['bg']}" stop-opacity=".18"/>
    <stop offset="100%" stop-color="{p['bg']}" stop-opacity="0"/>
  </linearGradient>
</defs>
<rect width="{W}" height="{H}" fill="{p['bg']}"/>
{body}
<rect width="{W}" height="{H}" fill="url(#glow)"/>
<rect width="{W}" height="{H}" fill="url(#scrim)"/>
</svg>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成封面底纹 SVG")
    ap.add_argument("--kind", default="circuit", choices=KINDS)
    ap.add_argument("--palette", default="ink", choices=list(PALETTES))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    seed = args.seed if args.seed is not None else random.randint(1, 10**6)
    svg = build(args.kind, args.palette, seed)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    print(f'{{"ok": true, "kind": "{args.kind}", "palette": "{args.palette}", '
          f'"seed": {seed}, "out": "{out}"}}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
