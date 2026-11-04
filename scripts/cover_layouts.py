#!/usr/bin/env python3
"""封面版式库：五个骨架，按文章类型选，不再共用一套坐标。

    from cover_layouts import build
    html = build({"template": "confront", "title": "...", "sub": "...", "kick": "...", "foot": "..."})

为什么要有这个：之前所有封面共用一套绝对定位（左竖线 + 右上光晕 + 白标题 + 蓝次行），
换篇只换字，所以六张封面是同一张图。读者的第一眼分辨不出这是哪类文章。

五个骨架各自对应一种文章类型：

    confront  反差型——上下对冲，中间撕口。给「以为 A，其实 B」这类。
    steps    教程型——三个编号块。给「三步做完」这类。
    number   数据型——一个巨大数字占画面。给「70%」「四十页」这类。
    quote    观点型——一句居中，大量留白。给判断、忠告类。
    photo    图片型——原文配图铺底 + 压暗 + 文字。给有强图片的新闻。

配色也跟着换（四套），否则换骨架也只是换个形状、味道一样。
"""
from __future__ import annotations

import random
import re
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

W, H = 900, 383

# 底纹种类按「文章气质」分：工具类用电路，方法类用点阵，观点类用噪点
KIND_BY_TPL = {
    "confront": "beams",
    "steps": "grid",
    "number": "circuit",
    "quote": "grain",
    "photo": "circuit",
}

FONT = '"PingFang SC","Microsoft YaHei","Source Han Sans SC",system-ui,sans-serif'

# 四套配色。每套：底、主字、强调、弱字
PALETTES = {
    "ink":    {"bg": "#0e1520", "fg": "#ffffff", "accent": "#4a8fe0", "mute": "#7d8ea6", "second": "#1b2534"},
    "paper":  {"bg": "#f7f5f0", "fg": "#16181c", "accent": "#b5442d", "mute": "#8a8579", "second": "#ede9e1"},
    "forest": {"bg": "#12201c", "fg": "#f2f6f3", "accent": "#5fb37f", "mute": "#7d9488", "second": "#172a24"},
    "brick":  {"bg": "#1d1310", "fg": "#fbf3ea", "accent": "#e0743c", "mute": "#9b7f70", "second": "#271a15"},
}


def _wrap(text: str, width: int, max_lines: int = 2) -> list[str]:
    """按字数折行，优先在标点处断。封面宁可少说，不要挤。"""
    t = (text or "").strip()
    if len(t) <= width:
        return [t]
    lines, rest = [], t
    while rest and len(lines) < max_lines:
        if len(rest) <= width:
            lines.append(rest)
            break
        cut = width
        for sep in ("，", "：", "、", "；", " ", "·"):
            i = rest.rfind(sep, 0, width + 4)
            if i >= max(4, width // 2):
                cut = i + 1
                break
        lines.append(rest[:cut].rstrip("，：、； "))
        rest = rest[cut:].lstrip("，：、； ")
    # 末行只剩一两个字会很难看（「大纲」单独一行），从上一行回吐两个字
    if len(lines) >= 2 and 0 < len(lines[-1]) < 3 and len(lines[-2]) > 4:
        need = 3 - len(lines[-1])
        lines[-1] = lines[-2][-need:] + lines[-1]
        lines[-2] = lines[-2][:-need]
    return lines or [""]


def _texture(cfg: dict, kind: str | None = None) -> str:
    """底纹层：用 cover_bg 画 SVG，内联成 data URI 铺在底层。

    为什么内联而不是引用文件：截图那一步只 open 一个 html，
    外部 svg 走 file:// 相对路径在临时目录里会断，内联最稳。
    """
    try:
        from cover_bg import build as _bg  # noqa: PLC0415
    except Exception:
        return ""
    k = kind or cfg.get("bg_kind") or KIND_BY_TPL.get(cfg.get("template", ""), "circuit")
    seed = cfg.get("seed")
    if seed is None:
        seed = sum(ord(c) for c in (cfg.get("title") or "x"))
    svg = _bg(k, cfg.get("palette", "ink"), int(seed))
    import base64

    b64 = base64.b64encode(svg.encode("utf-8")).decode()
    return (f'<div style="position:absolute;inset:0;'
            f'background-image:url(data:image/svg+xml;base64,{b64});'
            f'background-size:cover;background-position:center"></div>')


def _base(title_id: str) -> str:
    return (
        f"*{{margin:0;padding:0;box-sizing:border-box}}"
        f"body{{width:{W}px;height:{H}px;overflow:hidden;position:relative;font-family:{FONT};"
        f"-webkit-font-smoothing:antialiased}}"
    )


# ───────────────────────── 1. 对峙：反差型 ─────────────────────────
def tpl_confront(c: dict) -> str:
    tx = _texture(c)
    p, dark_first = PALETTES[c["palette"]], c.get("first", "dark") == "dark"
    top, bot = (p["bg"], p["second"]) if dark_first else (p["second"], p["bg"])
    # 下半块是「被压暗的另一半」，所以字必须用亮色，不能按亮底配深字
    fg_top = p["fg"] if dark_first else "#16181c"
    fg_bot = p["fg"]
    a = _wrap(c["title"], 11, 2)
    b = c.get("sub") or ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_base(c["title"])}
.h1{{position:absolute;left:0;top:0;width:100%;height:196px;
   background:linear-gradient(90deg,{top}f2 0%,{top}b8 55%,{top}55 100%)}}
.h2{{position:absolute;left:0;top:196px;width:100%;height:187px;
   background:linear-gradient(90deg,{bot}f0 0%,{bot}a8 52%,{bot}4d 100%);
   clip-path:polygon(0 14px,100% 0,100% 100%,0 100%)}}
.kick{{position:absolute;left:64px;top:44px;font-size:12px;letter-spacing:4px;
   color:{p["mute"]};font-weight:500}}
.a1{{position:absolute;left:62px;top:78px;font-size:42px;font-weight:800;color:{fg_top};
   line-height:1.2;letter-spacing:.5px;max-width:790px}}
.b1{{position:absolute;right:60px;top:225px;font-size:40px;font-weight:700;color:{fg_bot};
   line-height:1.2;text-align:right}}
.mark{{position:absolute;left:0;top:186px;width:100%;height:3px;background:{p["accent"]};
   transform:rotate(-1.1deg);transform-origin:left center}}
.foot{{position:absolute;left:64px;bottom:26px;font-size:12px;color:{p["mute"]};letter-spacing:1px}}
</style></head><body>
{tx}
<div class="h1"></div><div class="h2"></div>
<div class="kick">{escape(c.get("kick",""))}</div>
<div class="a1">{escape("".join(a))}</div>
<div class="b1">{escape(b[:16] or (a[1] if len(a)>1 else ""))}</div>
<div class="mark"></div>
<div class="foot">{escape(c.get("foot",""))}</div>
</body></html>"""


# ───────────────────────── 2. 序列：教程型 ─────────────────────────
def tpl_steps(c: dict) -> str:
    tx = _texture(c)
    p = PALETTES[c["palette"]]
    items = c.get("steps") or ["第一步", "第二步", "第三步"]
    cells = ""
    for i, s in enumerate(items[:3], 1):
        cells += (
            f'<div class="cell"><div class="num">{i:02d}</div>'
            f'<div class="txt">{escape(str(s)[:12])}</div></div>'
        )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_base(c["title"])}
body{{background:{p["bg"]}}}
.scrim{{position:absolute;inset:0;background:linear-gradient(90deg,{p["bg"]}d8 0%,{p["bg"]}6a 62%,{p["bg"]}10 100%)}}
.kick{{position:absolute;left:64px;top:44px;font-size:12px;letter-spacing:4px;
   color:{p["mute"]};font-weight:500}}
.t{{position:absolute;left:62px;top:76px;font-size:40px;font-weight:800;color:{p["fg"]};
   line-height:1.2;max-width:780px}}
.row{{position:absolute;left:62px;bottom:52px;display:flex;gap:14px}}
.cell{{width:238px;height:112px;background:{p["second"]};border-radius:4px;
   padding:16px 18px;position:relative}}
.num{{font-size:26px;font-weight:800;color:{p["accent"]};line-height:1}}
.txt{{margin-top:12px;font-size:17px;color:{p["fg"]};opacity:.92;line-height:1.35}}
.foot{{position:absolute;right:60px;bottom:20px;font-size:12px;color:{p["mute"]};letter-spacing:1px}}
</style></head><body>
{tx}
<div class="kick">{escape(c.get("kick",""))}</div>
<div class="t">{escape(c["title"][:22])}</div>
<div class="scrim"></div><div class="row">{cells}</div>
<div class="foot">{escape(c.get("foot",""))}</div>
</body></html>"""


# ───────────────────────── 3. 数字：数据型 ─────────────────────────
NUM_RE = re.compile(r"(\d+(?:\.\d+)?\s*[%万亿千百十]?|[一二三四五六七八九十百千万]+(?:亿|万|千|百|十)?)")


def pick_number(text: str) -> str:
    m = NUM_RE.search(text or "")
    return m.group(1).strip() if m else ""


def tpl_number(c: dict) -> str:
    tx = _texture(c)
    p = PALETTES[c["palette"]]
    num = c.get("number") or pick_number(c["title"]) or "01"
    rest_text = (c["title"] or "").replace(num, "", 1).strip(" 　，。")
    # 数字本身超过 3 个字符（如「四十」「百分之八十」）就不适合当巨型数字，
    # 退回引语版式，否则画面会被一个字撑满、标题被劈成两半
    if len(num) > 3:
        return tpl_quote(c)
    # 中文数字不适合做视觉主元素（「四十」占地方却没张力），替换为标点式符号
    if re.fullmatch(r"[一二三四五六七八九十百千万]+", num):
        return tpl_quote(c)
    rest = _wrap(rest_text, 15, 2)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_base(c["title"])}
body{{background:{p["bg"]}}}
.wash{{position:absolute;right:-60px;bottom:-120px;width:460px;height:460px;border-radius:50%;
   background:radial-gradient(circle,{p["accent"]}22,transparent 62%)}}
.kick{{position:absolute;left:64px;top:44px;font-size:12px;letter-spacing:4px;
   color:{p["mute"]};font-weight:500}}
.num{{position:absolute;left:52px;top:88px;font-size:172px;font-weight:800;color:{p["accent"]};
   line-height:.86;letter-spacing:-4px}}
.t1{{position:absolute;right:60px;top:120px;width:330px;font-size:27px;font-weight:700;
   color:{p["fg"]};line-height:1.35;text-align:right}}
.t2{{position:absolute;right:60px;top:210px;width:330px;font-size:16px;color:{p["mute"]};
   line-height:1.5;text-align:right}}
.foot{{position:absolute;left:64px;bottom:26px;font-size:12px;color:{p["mute"]};letter-spacing:1px}}
</style></head><body>
{tx}
<div class="wash"></div>
<div class="kick">{escape(c.get("kick",""))}</div>
<div class="num">{escape(num)}</div>
<div class="t1">{escape(rest[0] if rest else "")}</div>
<div class="t2">{escape(c.get("sub") or (rest[1] if len(rest)>1 else ""))}</div>
<div class="foot">{escape(c.get("foot",""))}</div>
</body></html>"""


# ───────────────────────── 4. 引语：观点型 ─────────────────────────
def tpl_quote(c: dict) -> str:
    tx = _texture(c)
    p = PALETTES[c["palette"]]
    a = _wrap(c["title"], 14, 2)
    lines = "".join(f'<div class="ln">{escape(x)}</div>' for x in a)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_base(c["title"])}
body{{background:{p["bg"]}}}
.scrim{{position:absolute;inset:0;background:radial-gradient(ellipse at 50% 50%,{p["bg"]}55 0%,{p["bg"]}cc 70%)}}
.q{{position:absolute;left:58px;top:44px;font-size:96px;color:{p["accent"]};opacity:.32;
   font-family:Georgia,serif;line-height:1}}
.ln{{font-size:46px;font-weight:800;color:{p["fg"]};line-height:1.28;letter-spacing:1px}}
.wrap{{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);text-align:center;
   padding:0 96px}}
.sub{{margin-top:18px;font-size:16px;color:{p["mute"]};letter-spacing:.5px}}
.foot{{position:absolute;left:0;right:0;bottom:28px;text-align:center;font-size:12px;
   color:{p["mute"]};letter-spacing:2px}}
</style></head><body>
{tx}
<div class="scrim"></div>
<div class="q">“</div>
<div class="wrap">{lines}<div class="sub">{escape(c.get("sub",""))}</div></div>
<div class="foot">{escape(c.get("foot",""))}</div>
</body></html>"""


# ───────────────────────── 5. 蓝图暗：科技感（学 article-tools 的 blueprint） ─────────────────────────
def tpl_photo(c: dict) -> str:
    """有真图就铺图；没图就用 CSS 拼出「科技感」——双层网格 + 十字准星 + 同心圆 + 光斑。

    全套元素都是 CSS 画的（渐变 / 伪元素 / transform），没有一张位图：
      · 双层网格（4px 细 + 20px 粗）——营造纵深
      · 十字准星 + 坐标标注——工程图气质
      · 同心圆轨道 + 核心点——AI 的视觉隐喻
      · 两团径向光斑——去掉塑料感
    参考：github.com/eternityspring/article-tools 的 blueprint 模板。
    """
    p = PALETTES[c["palette"]]
    a = _wrap(c["title"], 15, 2)
    lines = "".join(f'<div class="ln">{escape(x)}</div>' for x in a)
    bg = c.get("bg_image") or ""
    if bg:
        layer = ('<div class="bg" style="background-image:url(\'' + bg + '\')"></div>'
                 '<div class="veil"></div>')
    else:
        layer = (f'<div class="grid1"></div><div class="grid2"></div>'
                 f'<div class="blob b1"></div><div class="blob b2"></div>'
                 f'<div class="ring"><div class="ring2"></div><div class="core"></div></div>'
                 f'<div class="cross v"></div><div class="cross h"></div>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_base(c["title"])}
body{{background:{p["bg"]}}}
.bg{{position:absolute;inset:0;background-size:cover;background-position:center;
   filter:saturate(.85) brightness(.45) contrast(1.08) blur(.4px)}}
.veil{{position:absolute;inset:0;background:linear-gradient(96deg,
   {p["bg"]}ff 0%,{p["bg"]}f2 42%,{p["bg"]}8c 74%,{p["bg"]}2a 100%)}}
/* 双层网格：细 22px、粗 110px，越靠右越亮 */
.grid1{{position:absolute;inset:0;background-image:
   linear-gradient({p["accent"]}12 1px,transparent 1px),
   linear-gradient(90deg,{p["accent"]}12 1px,transparent 1px);
   background-size:22px 22px;
   mask-image:linear-gradient(90deg,transparent 18%,rgba(0,0,0,.9) 62%,#000 100%)}}
.grid2{{position:absolute;inset:0;background-image:
   linear-gradient({p["accent"]}1f 1px,transparent 1px),
   linear-gradient(90deg,{p["accent"]}1f 1px,transparent 1px);
   background-size:110px 110px;
   mask-image:linear-gradient(90deg,transparent 26%,#000 78%)}}
/* 两团光斑：一大一小，右上方 */
.blob{{position:absolute;border-radius:50%}}
.b1{{right:-40px;top:-90px;width:400px;height:400px;
   background:radial-gradient(circle,{p["accent"]}40 0%,transparent 66%)}}
.b2{{right:150px;bottom:-120px;width:320px;height:320px;
   background:radial-gradient(circle,{p["accent"]}26 0%,transparent 68%)}}
/* 同心圆轨道 + 核心点 */
.ring{{position:absolute;right:96px;top:50%;transform:translateY(-50%);
   width:184px;height:184px;border-radius:50%;border:1px solid {p["accent"]}4d}}
.ring2{{position:absolute;inset:26px;border-radius:50%;border:1px dashed {p["accent"]}38}}
.core{{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
   width:7px;height:7px;border-radius:50%;background:{p["accent"]};
   box-shadow:0 0 14px {p["accent"]},0 0 30px {p["accent"]}80}}
/* 十字准星：一条竖一条横，穿过画面右侧 */
.cross{{position:absolute;background:{p["accent"]}22}}
.cross.v{{right:187px;top:0;width:1px;height:100%}}
.cross.h{{left:0;top:50%;height:1px;width:100%}}
/* 左上品牌线 + 坐标标注 */
.top{{position:absolute;left:64px;top:40px;display:flex;align-items:center;gap:10px}}
.top i{{display:block;width:26px;height:2px;background:{p["accent"]}}}
.kick{{font-size:12px;letter-spacing:4px;color:{p["mute"]};font-weight:500}}
.coord{{position:absolute;right:56px;top:38px;font-size:10px;letter-spacing:2px;
   color:{p["mute"]};opacity:.7;font-family:ui-monospace,Menlo,monospace}}
.box{{position:absolute;left:62px;bottom:72px;max-width:660px}}
.ln{{font-size:40px;font-weight:800;color:{p["fg"]};line-height:1.26;letter-spacing:.5px}}
.rule{{position:absolute;left:62px;bottom:56px;width:52px;height:2px;background:{p["accent"]}}}
.foot{{position:absolute;right:56px;bottom:24px;font-size:12px;color:{p["mute"]};letter-spacing:1px}}
</style></head><body>
{layer}
<div class="coord">900 × 383</div>
<div class="top"><i></i><div class="kick">{escape(c.get("kick",""))}</div></div>
<div class="box">{lines}</div><div class="rule"></div>
<div class="foot">{escape(c.get("foot",""))}</div>
</body></html>"""


TEMPLATES = {
    "confront": tpl_confront,
    "steps": tpl_steps,
    "number": tpl_number,
    "quote": tpl_quote,
    "photo": tpl_photo,
}


def guess_template(title: str, *, has_image: bool = False, radar_key: str = "",
                   steps: list[str] | None = None) -> str:
    """按标题猜该用哪个骨架。宁可猜得笨一点，也别每篇都长一样。"""
    t = title or ""
    if has_image:
        return "photo"
    if steps:
        return "steps"
    if re.search(r"三步|四步|几步|步走|先.{0,6}再|怎么|如何|教程", t):
        return "steps"
    if NUM_RE.search(t) and re.search(r"\d|十|百|千|万|亿|%", t):
        return "number"
    if re.search(r"别|不要|千万别|踩|坑|错了|不必|反而", t) or radar_key:
        return "confront"
    return "quote"


def guess_palette(seed: str) -> str:
    """同一篇文章固定用一个配色，不同的文章尽量岔开。"""
    keys = list(PALETTES)
    return keys[sum(ord(ch) for ch in (seed or "x")) % len(keys)]


def build(cfg: dict) -> str:
    cfg = dict(cfg)
    tpl = cfg.get("template") or guess_template(
        cfg.get("title", ""), has_image=bool(cfg.get("bg_image")),
        radar_key=cfg.get("radar_key", ""), steps=cfg.get("steps"),
    )
    if tpl == "photo" and False:
        tpl = "quote"
    cfg["template"] = tpl
    cfg.setdefault("palette", guess_palette(cfg.get("title", "")))
    if cfg["palette"] not in PALETTES:
        cfg["palette"] = "ink"
    return TEMPLATES[tpl](cfg)
