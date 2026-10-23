#!/usr/bin/env python3
"""没有可用原图时，用 HTML 渲染一张中文封面卡（900×383，2.35:1）。

    python scripts/gzh_covercard.py --title "删掉 80% 系统提示词" --sub "模型反而更强"
    python scripts/gzh_covercard.py --text "DeepSeek 开源的推理引擎" --out 稿件/x/cover.jpg

为什么不用 AI 生图：中文经常糊、字会错。用浏览器渲染 HTML 再截图，
字是绝对清晰的，尺寸也精确等于 900×383（微信头条要求 2.35:1）。

依赖 agent-browser（和主流程一样），不需要 Pillow。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from gzh_cover import image_dims  # noqa: E402
from gzh_draft import workroot  # noqa: E402


def load_brand() -> dict:
    """品牌文案（封面卡左上/左下的小字）从工作区的 品牌.json 读。

    为什么不在代码里写死：这是作者自己的号名，别人用不该看到「到了早上.」。
    没有这个文件就留空，封面卡少两行小字，不影响出图。
    """
    try:
        f = workroot() / "品牌.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            return {"kick": data.get("kick") or "", "foot": data.get("foot") or ""}
    except Exception:
        pass
    return {"kick": "", "foot": ""}

TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:900px;height:383px;overflow:hidden;background:#0e1520;position:relative;
     font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif}}
.glow{{position:absolute;right:-90px;top:-120px;width:420px;height:420px;border-radius:50%;
      background:radial-gradient(circle,rgba(74,143,224,.22),transparent 65%)}}
.bar{{position:absolute;left:56px;top:56px;width:3px;height:64px;background:#4a8fe0}}
.kick{{position:absolute;left:74px;top:56px;font-size:13px;letter-spacing:3px;color:#7d8ea6;font-weight:500}}
.t1{{position:absolute;left:74px;top:{t1top}px;font-size:{fs}px;font-weight:700;color:#fff;
    line-height:1.28;letter-spacing:.5px;max-width:740px}}
.t2{{position:absolute;left:74px;top:{t2top}px;font-size:{fs}px;font-weight:700;color:#4a8fe0;
    line-height:1.28;letter-spacing:.5px;max-width:740px}}
.rule{{position:absolute;left:74px;top:{ruletop}px;width:56px;height:2px;background:#2a3a52}}
.sub{{position:absolute;left:74px;top:{subtop}px;font-size:16px;color:#98a7bd;letter-spacing:.3px}}
.foot{{position:absolute;left:74px;bottom:44px;font-size:13px;color:#5c6b80;letter-spacing:1px}}
</style></head><body>
<div class="glow"></div><div class="bar"></div>
{lines}
{rule}
{sub}
<div class="foot">{foot}</div>
</body></html>"""


def find_ab() -> str:
    for n in ("agent-browser", "agent-browser.cmd", "agent-browser.exe"):
        hit = shutil.which(n)
        if hit:
            return hit
    raise SystemExit("找不到 agent-browser，无法渲染封面卡")


def split_title(title: str, sub: str | None) -> tuple[str, str | None]:
    """两行：主行白色、次行深蓝。

    封面能容纳的字数很有限（44px 下约 16 字/行）。所以：
    1. 给了 sub 就直接两行；
    2. 标题里有「：」「，」等停顿，优先在第一个停顿处断开（语义最自然）；
    3. 还是太长就按字数切，并截断到 26 字以内——宁可少说，不要在封面上挤成一团。
    """
    t = (title or "").strip()
    if sub:
        return t[:24], sub.strip()[:18]
    if len(t) <= 14:
        return t, None
    for sep in ("：", ":", "，", ",", "；", "|"):
        i = t.find(sep)
        if 4 <= i <= 20:
            return t[:i], t[i + 1:].strip()[:18]
    if len(t) <= 26:
        cut = 13
        return t[:cut], t[cut:].lstrip("，,。 　")
    cut = 13
    return t[:cut], t[cut:].lstrip("，,。 　")[:13]


def build_html(title: str, sub: str | None, kick: str, foot: str) -> str:
    a, b = split_title(title, sub)
    total = len(a) + len(b or "")
    fs = 44 if total <= 18 else (38 if total <= 26 else 32)
    two = b is not None
    t1top = 96 if two else 118
    t2top = t1top + int(fs * 1.28)
    ruletop = (t2top + fs + 26) if two else (t1top + fs + 26)
    subtop = ruletop + 20
    lines = f'<div class="kick">{kick}</div>\n<div class="t1">{a}</div>'
    if two:
        lines += f'\n<div class="t2">{b}</div>'
    rule = '<div class="rule"></div>' if ruletop < 300 else ""
    subdiv = f'<div class="sub">{sub and ""}</div>' if False else ""
    return TEMPLATE.format(
        t1top=t1top, t2top=t2top, fs=fs, ruletop=ruletop, subtop=subtop,
        lines=lines, rule=rule, sub=subdiv, foot=foot,
    )


def render(title: str, sub: str | None, kick: str, foot: str, out: Path) -> dict:
    html = build_html(title, sub, kick, foot)
    work = Path(tempfile.gettempdir()) / f"covercard-{hashlib.sha1(html.encode()).hexdigest()[:10]}"
    work.mkdir(parents=True, exist_ok=True)
    html_path = work / "card.html"
    html_path.write_text(html, encoding="utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    shot = work / "card.png"
    batch = json.dumps(
        [
            ["open", html_path.as_uri()],
            ["set", "viewport", "900", "383"],
            ["wait", "1200"],
            ["screenshot", str(shot)],
        ],
        ensure_ascii=False,
    )
    # 必须带 --no-sandbox：当前 shell 是管理员权限，Chrome 会自我去提权后退出（实测）
    argv = [
        find_ab(),
        "--args",
        "--no-sandbox,--do-not-de-elevate",
        "--max-output",
        "2000",
        "batch",
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write(batch)
        batch_file = fh.name
    # ⚠ 不能用 capture_output / PIPE：浏览器守护进程会继承管道并长期占用，
    #   父进程永远等不到 EOF，实测必卡死。必须重定向到文件后再读。
    out_file = work / "ab.out"
    err_file = work / "ab.err"
    with open(batch_file, "rb") as stdin, open(out_file, "wb") as fo, open(err_file, "wb") as fe:
        subprocess.run(argv, stdin=stdin, stdout=fo, stderr=fe, timeout=200)
    ab_out = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""

    if not shot.exists():
        detail = err_file.read_text(encoding="utf-8", errors="replace")[-300:] or ab_out[-300:] or "截图失败"
        return {"ok": False, "reason": detail.strip()}

    # 转成 jpg（微信封面用 jpg 更稳），ffmpeg 可选；没有就直接用 png
    final = out
    ff = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ff and out.suffix.lower() in (".jpg", ".jpeg"):
        with open(out_file, "ab") as fo, open(err_file, "ab") as fe:
            subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(shot),
                            "-q:v", "2", str(out)], stdout=fo, stderr=fe, timeout=90)
    else:
        final = out.with_suffix(".png")
        shutil.copy2(shot, final)

    dims = image_dims(final.read_bytes()) if final.exists() else None
    return {
        "ok": bool(dims),
        "cover": str(final),
        "size": f"{dims[0]}x{dims[1]}" if dims else None,
        "ratio": round(dims[0] / dims[1], 3) if dims and dims[1] else None,
        "lines": [split_title(title, sub)[0], split_title(title, sub)[1]],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="渲染中文封面卡（900×383）")
    ap.add_argument("--title", required=True, help="主标题；较长会自动折成两行")
    ap.add_argument("--sub", default=None, help="第二行（深蓝色）")
    ap.add_argument("--kick", default=None, help="左上角小字（默认读 品牌.json）")
    ap.add_argument("--foot", default=None, help="左下角署名（默认读 品牌.json）")
    ap.add_argument("--out", default=".cache/covers/card.jpg")
    args = ap.parse_args(argv)

    brand = load_brand()
    res = render(
        args.title,
        args.sub,
        args.kick if args.kick is not None else brand["kick"],
        args.foot if args.foot is not None else brand["foot"],
        Path(args.out).resolve(),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
