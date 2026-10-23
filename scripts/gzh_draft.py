#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公众号草稿上传器 —— 把「标题 + 摘要 + 正文 HTML + 封面」写进公众号草稿箱。

只做**确定性的上传**：不生成内容、不改写文字。内容由成稿环节产出，本脚本负责准确灌进后台并保存为草稿。
**永远只存草稿，绝不点「发表」。**

用法：
    python gzh_draft.py --manifest article.json
    python gzh_draft.py --manifest article.json --appmsgid 100000001   # 续编辑已有草稿
    python gzh_draft.py --manifest article.json --dry-run              # 只打印要执行的命令

manifest 格式（JSON，UTF-8）：
    {
      "title": "标题（≤64 字）",
      "summary": "摘要（≤120 字，选填）",
      "author": "作者（选填）",
      "source_url": "原文链接（选填，转载注明来源用）",
      "body_html_path": "body.html",        // 二选一
      "body_html": "<p>...</p>",            // 二选一
      "cover_path": "cover.jpg"             // 选填，封面（gif/jpeg/jpg/png）
    }
    相对路径按 manifest 所在目录解析。

前置条件：
  * 已安装 agent-browser 与 Chrome（`agent-browser install`）。
  * 已用 `--profile` 指定登录态目录并扫码登录过。登录态会过期，过期时本脚本会明确报错，
    提示你重新扫码（不会静默失败）。

为什么长成这样（都是实测结论，见 AgentDesign 第六节）：
  * 必须传 `--args "--no-sandbox,--do-not-de-elevate"`：当前 shell 以管理员权限运行，
    Chrome 检测到提权会自我 relaunch 后退出，报 DevToolsActivePort 相关错误。
  * 必须一次调用一个自包含 batch：浏览器守护进程活不过一次进程调用。
  * 标题写 `.ProseMirror`[0]（不能碰隐藏的 `textarea#title`，否则文字会跑到正文里）；
    正文写 `.ProseMirror`[1] 且必须用 `execCommand('insertHTML')`（直接改 innerHTML 编辑器不认）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

TOKEN_RE = re.compile(r"[?&]token=(\d+)")
APPMSGID_RE = re.compile(r"appmsgid=(\d+)")
HOME_URL = "https://mp.weixin.qq.com/"

AB_ARGS = "--no-sandbox,--do-not-de-elevate"


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


def find_ab(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("agent-browser", "agent-browser.cmd", "agent-browser.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    raise SystemExit("找不到 agent-browser。先执行：npm install -g agent-browser && agent-browser install")


def run_batch(ab: str, profile: Path, commands: list[list[str]], timeout: int = 240) -> str:
    """执行一个 agent-browser batch，返回合并后的输出文本。

    注意：**不能**用管道捕获输出（subprocess 的 stdout=PIPE / capture_output=True）。
    浏览器守护进程会继承并长期占用该管道，导致父进程永远等不到 EOF。实测必卡。
    因此统一重定向到临时文件，等子进程退出后再读文件。
    """
    payload = json.dumps(commands, ensure_ascii=False)
    cmd = [
        ab,
        "--args",
        AB_ARGS,
        "--profile",
        str(profile),
        "--max-output",
        "6000",
        "batch",
    ]
    fd, logname = tempfile.mkstemp(prefix="gzh-draft-", suffix=".log")
    try:
        with os.fdopen(fd, "wb") as logf:
            try:
                subprocess.run(
                    cmd,
                    input=payload.encode("utf-8"),
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                raise SystemExit(f"agent-browser batch 超时（{timeout}s）。检查是否卡在登录页或网络。")
        return Path(logname).read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            os.unlink(logname)
        except OSError:
            pass


def get_token(ab: str, profile: Path) -> str:
    """打开后台首页，从跳转后的 URL 里取本次会话的 token。未登录会报错。"""
    out = run_batch(ab, profile, [["open", HOME_URL], ["wait", "3500"], ["eval", "location.href"]], 120)
    m = TOKEN_RE.search(out)
    if m:
        return m.group(1)
    if "login" in out.lower() or "二维码" in out or "qrcode" in out.lower():
        raise SystemExit(
            "登录态已失效（后台停在登录页）。\n"
            f"请重新扫码：agent-browser --args \"{AB_ARGS}\" --profile \"{profile}\" --headed open {HOME_URL}"
        )
    raise SystemExit(f"没能从后台拿到 token，原始输出：\n{out[:800]}")


def js(value: str) -> str:
    """把 Python 字符串变成安全的 JS 字符串字面量。"""
    return json.dumps(value, ensure_ascii=False)


TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
STRUCT_TAGS = ("section", "p")


def split_blocks(html: str) -> list[str]:
    """把正文拆成「顶层块」列表，**嵌套 <section> 安全**。

    返回的是统计口径（顶层块数），不参与注入——注入是整篇一次 insertHTML。

    ⚠ 不能用 `html.find("</section>", ...)` 找最近的闭合标签：时间线、参数卡这类
    组件内部本来就有嵌套 <section>，那样会在内层截断，把 1 块切成 2 个残缺块，
    自检基数虚高、保存闸门误判（2026-10-21 踩过）。
    这里用标签栈计深度，深度回到 0 才算一块结束。
    """
    blocks: list[str] = []
    stack: list[str] = []
    start: int | None = None
    for m in TAG_RE.finditer(html):
        closing, tag = m.group(1) == "/", m.group(2).lower()
        if tag not in STRUCT_TAGS:
            continue
        void = m.group(3).rstrip().endswith("/")
        if not closing:
            if not stack:
                start = m.start()
            if void:
                if not stack and start is not None:
                    blocks.append(html[start:m.end()])
                    start = None
            else:
                stack.append(tag)
        else:
            if stack and stack[-1] == tag:
                stack.pop()
            if not stack and start is not None:
                blocks.append(html[start:m.end()])
                start = None
    if start is not None:  # 未闭合的尾巴保留，交给 scan_body 报错
        blocks.append(html[start:])
    return [b for b in blocks if b.strip()]


ALLOWED_TAGS = {"section", "span", "strong", "br", "img"}
VOID_TAGS = {"br", "img"}
ALLOWED_ATTRS = {"style"}
EXTRA_ATTRS = {"img": {"src", "alt"}}
ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>/]+)")
PALETTE = {
    "#111", "#333", "#3f3f3f", "#444", "#555", "#999", "#d8d8d8",
    "#e6e6e6", "#f6f7f9", "#1a4d8f", "#fff", "#ffffff",
}


def scan_body(html: str) -> dict:
    """只读扫描正文：白名单校验 + 结构统计。不联网、不写文件。

    为什么要这道闸门：保存前只对比「块数/加粗数」，组件写错标签（比如误用 <table>、
    把 <strong> 嵌进 <span>）照样会被静默存进去，等手机上发现已经晚了。
    这里在**开浏览器之前**就拦下，并给出带行号的错误。
    """
    problems: list[dict] = []
    stack: list[str] = []
    stats: Counter = Counter()
    blocks: list[str] = []
    start: int | None = None
    last_end = 0

    def line_of(p: int) -> int:
        return html.count("\n", 0, p) + 1

    for m in TAG_RE.finditer(html):
        closing, tag, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3)
        ln = line_of(m.start())

        def bad(msg: str, level: str = "error") -> None:
            problems.append({"line": ln, "tag": tag, "level": level, "msg": msg})

        if tag not in ALLOWED_TAGS:
            bad(
                f"<{tag}> 不许用（微信会剥掉或打回普通块）；"
                f"只允许 {'/'.join(sorted(ALLOWED_TAGS))}"
            )
            continue

        if not closing:
            for name, _ in ATTR_RE.findall(attrs):
                key = name.lower()
                if key.startswith("on"):
                    bad(f"事件属性 {key} 会被剥掉")
                elif key not in ALLOWED_ATTRS and key not in EXTRA_ATTRS.get(tag, set()):
                    bad(f"属性 {key} 会被微信忽略（只有 style 能留）")
            if "style=" in attrs:
                stats["styled"] += 1
                if "inline-block" in attrs:
                    stats["iblock"] += 1
            stats[tag] += 1
            if tag in VOID_TAGS:
                continue
            if tag == "strong" and "span" in stack:
                bad("<strong> 嵌在 <span> 里了，样式会被剥；让 strong 与 span 平级")
            if not stack:
                if tag != "section":
                    bad(f"顶层块必须是 <section>，这里是 <{tag}>")
                gap = html[last_end:m.start()].strip()
                if gap:
                    bad(f"顶层有裸露文本（不在任何 section 内，微信会吞掉）：{gap[:40]}")
                start = m.start()
            stack.append(tag)
        else:
            if tag in VOID_TAGS:
                continue
            if not stack or stack[-1] != tag:
                top = stack[-1] if stack else "空"
                bad(f"闭合标签不匹配：</{tag}>，栈顶是 {top}")
                continue
            stack.pop()
            if not stack and start is not None:
                blocks.append(html[start:m.end()])
                start = None
                last_end = m.end()

    if stack:
        problems.append(
            {
                "line": line_of(html.rfind("<" + stack[-1])),
                "tag": stack[-1],
                "level": "error",
                "msg": f"<{stack[-1]}> 没有闭合",
            }
        )

    # ---- 以下只警告，不阻断 ----
    for m in re.finditer(r">([^<>]{120,})<", html):
        problems.append(
            {
                "line": line_of(m.start()),
                "tag": "-",
                "level": "warning",
                "msg": f"这段 {len(m.group(1))} 字，手机上超过 3 行，拆段",
            }
        )
    if stats["strong"] > 10:
        problems.append(
            {
                "line": 1,
                "tag": "strong",
                "level": "warning",
                "msg": f"加粗 {stats['strong']} 处，超过 10 处等于没有强调",
            }
        )
    for m in re.finditer(r"color:\s*(#[0-9a-fA-F]{3,6})", html):
        if m.group(1).lower() not in PALETTE:
            problems.append(
                {
                    "line": line_of(m.start()),
                    "tag": "-",
                    "level": "warning",
                    "msg": f"颜色 {m.group(1)} 不在调色板里（唯一强调色 #1a4d8f）",
                }
            )
    if blocks and "信息来源" not in blocks[-1]:
        problems.append(
            {
                "line": line_of(html.rfind("<section")),
                "tag": "-",
                "level": "warning",
                "msg": "末块不是来源行（缺「信息来源：」）",
            }
        )

    chars = len(re.sub(r"\s", "", re.sub(r"<[^>]+>", "", html)))
    return {
        "errors": [p for p in problems if p["level"] == "error"],
        "warnings": [p for p in problems if p["level"] == "warning"],
        "blocks": blocks,
        "stats": {**stats, "blocks": len(blocks), "chars": chars},
    }



def prepare_cover_upload(cover_abs: Path) -> tuple[Path, str]:
    """把封面复制成「唯一文件名」再上传。

    为什么：封面只能从**素材库**里选，素材库列表按文件名认图。同名文件（如 cover.jpg）
    在库里会堆一堆、无法区分新旧，所以用内容哈希命名（cvr+10 位），每次内容不同名不同，
    选图时按这个前缀精确匹配。返回 (上传用临时文件, 匹配前缀)。
    """
    import hashlib

    raw = cover_abs.read_bytes()
    stem = "cvr" + hashlib.sha1(raw).hexdigest()[:10]
    ext = cover_abs.suffix.lower() or ".jpg"
    tmp = Path(tempfile.gettempdir()) / f"{stem}{ext}"
    tmp.write_bytes(raw)
    return tmp, stem


def build_commands(
    token: str,
    appmsgid: str | None,
    title: str,
    summary: str,
    author: str,
    source_url: str,
    body: str,
    cover_abs: Path | None,
    cover_upload: Path | None = None,
    cover_prefix: str = "",
    collection: str = "",
) -> list[list[str]]:
    if appmsgid:
        editor = (
            "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit"
            f"&type=77&appmsgid={appmsgid}&token={token}&lang=zh_CN"
        )
    else:
        editor = (
            "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit"
            f"&isNew=1&type=10&token={token}&lang=zh_CN"
        )

    cmds: list[list[str]] = [
        # 视口必须够大：封面弹窗（编辑封面）的底部按钮在 1080×498 下会被切到视口外，
        # 点击坐标全部打空（实测卡了很久）。1440×1200 能让弹窗完整可见。
        ["open", editor],
        ["set", "viewport", "1440", "1200"],
        # 新建草稿时 open 之后还会二次跳转（URL 多出 &timestamp=…）。
        # 等 6s 不够：注入会打在「跳转前的空页面」上，表现为 BODY_BLOCKS=1 / BODY_LEN=10。
        ["wait", "10000"],
        # 关掉首次进入的引导气泡，避免挡住后续操作
        ["eval", "[...document.querySelectorAll('button')].filter(b=>b.innerText==='我知道了').forEach(b=>b.click());'dismissed'"],
    ]

    # 标题：.ProseMirror[0]。先 selectAll 再插，兼容「续编辑已有草稿」
    cmds.append(
        [
            "eval",
            "var t=document.querySelectorAll('.ProseMirror')[0];"
            f"t.focus();document.execCommand('selectAll',false,null);document.execCommand('insertText',false,{js(title)});"
            "t.innerText.replace(/\\s+/g,' ').slice(0,80)",
        ]
    )
    # 写完标题等编辑器重渲染完再动正文，否则正文区可能还没就绪
    cmds.append(["wait", "1500"])

    # 作者
    if author:
        cmds.append(
            [
                "eval",
                "var a=document.querySelector('input#author');"
                f"if(a){{a.value={js(author)};a.dispatchEvent(new Event('input',{{bubbles:true}}));}}"
                "a?a.value:'NO-AUTHOR'",
            ]
        )

    # 正文：.ProseMirror[1]，**整篇一次性 insertHTML**，前提是标签用对。
    #
    # 2026-10-21 一整轮实测的结论（结论反直觉，别改回去）：
    #   ① 用 <p> 作段落 → 微信编辑器会把它们**合并压平成一块**，加粗和样式全丢（这就是「挤成一坨」的根因）。
    #   ② 用 <section> 作段落 + 文字样式写在 <span> 上 → 整篇一次插入，块数/加粗/样式全部保住
    #      （实测 3 块进 3 块出，BLOCKS=3 STRONG=1 STYLED=8）。
    #   ③ 拆成多次 insertHTML 反而更糟：后面的段落会被吞进前一段的 span 里（嵌套）。
    #   ④ 段落之间**不要** insertParagraph，块边界由 <section> 自己给。
    #   ⑤ 不要把块放成 JS 数组字面量（长 HTML 数组会被当成字符串，逐字符插入）。
    #
    # 所以正确写法是：模板产出「全 section + span 内联样式」的 HTML，这里整体插一次。
    body_blocks = split_blocks(body)
    # 关键：JS 的 b.click() **不会**真正移动焦点，execCommand 就打在空气上
    # （表现为 BODY_BLOCKS 还是旧值/1，BODY_LEN=10）。必须用 agent-browser 的 click 发真实鼠标事件。
    cmds.append(
        [
            "eval",
            "var ps=[...document.querySelectorAll('.ProseMirror')];"
            "var cand=ps.filter(function(p){return p.offsetHeight>120;});"
            "var b=cand.length?cand[cand.length-1]:ps[1];"
            "b?(b.id='__gzh_body','BODY-ID '+(cand.length?'vis':'fallback')):'NO-BODY'",
        ]
    )
    cmds.append(["click", "#__gzh_body"])
    cmds.append(["wait", "800"])
    cmds.append(
        [
            "eval",
            "var b=document.getElementById('__gzh_body');"
            "if(!b){'NO-BODY'}else{"
            "document.execCommand('selectAll',false,null);"
            "document.execCommand('delete',false,null);"
            f"document.execCommand('insertHTML',false,{js(body)});"
            "var nb=b.querySelectorAll('section,p').length;"
            "var ns=b.querySelectorAll('strong').length;"
            "var nst=b.querySelectorAll('[style]').length;"
            "'BODY_BLOCKS='+nb+' HAS_STRONG='+ns+' STYLED='+nst+' BODY_LEN='+b.innerText.length}",
        ]
    )

    # ⚠ 摘要必须放在正文之后写。
    # 微信的摘要字段默认「不填则自动抓取正文开头」，先写摘要再注入正文会被正文覆盖（实测踩过）。
    if summary:
        cmds.append(
            [
                "eval",
                "var d=document.querySelector('textarea#js_description');"
                f"if(d){{d.value={js(summary)};d.dispatchEvent(new Event('input',{{bubbles:true}}));d.dispatchEvent(new Event('change',{{bubbles:true}}));}}"
                "d?d.value.slice(0,40):'NO-DESC'",
            ]
        )

    # 原文链接：默认折叠，点「未添加」那块（.js_article_url_allow_click）才会展开输入框。
    # 坑 1：展开后的 input **没有 id**（实测 id 时有时无），只能按 placeholder 定位。
    # 坑 2：element.click() 会被它的自定义控件拦掉，必须用原生鼠标点击。
    # 坑 3：直接赋 .value 不生效（框架会忽略），必须用真实键盘输入（fill）。
    if source_url:
        src_sel = 'input[placeholder="输入或粘贴原文链接"]'
        cmds.append(["click", "#js_article_url_area .js_article_url_allow_click"])
        cmds.append(["wait", "1800"])
        cmds.append(["fill", src_sel, source_url])
        cmds.append(["wait", "600"])
        cmds.append(
            [
                "eval",
                f'var s=document.querySelector(\'{src_sel}\');s?("SRC="+s.value):"NO-SOURCE-INPUT"',
            ]
        )

    # ---- 封面（2026-10-19 实测打通）----
    # 通路：先把图传进**素材库** → 编辑器里点封面区 → 弹出菜单（从正文选择 / 从图片库选择 /
    # 微信扫码上传 / 一键生成封面）→ 选「从图片库选择」→ 选中刚传的图 → 下一步 → 编辑封面
    # （裁剪预览）→ 确认。
    # 关键坑：封面按钮点开的是**下拉菜单**，不是文件选择框；一直找 file input 会完全走错方向。
    if cover_abs is not None and cover_upload is not None:
        cmds.append(["scrollintoview", ".js_cover_btn_area"])
        cmds.append(["wait", "1000"])
        cmds.append(["click", ".js_cover_btn_area"])
        cmds.append(["wait", "2200"])
        cmds += [
            [
                "eval",
                "var seen=[],hit=null;"
                "[...document.querySelectorAll('ul.pop-opr__list')].forEach(function(u){"
                "if(!u.offsetParent)return;"
                "[...u.querySelectorAll('li')].forEach(function(l){"
                "var t=(l.innerText||'').trim();if(t)seen.push(t);"
                "if(!hit&&t.indexOf('图片库')>=0)hit=l;});});"
                "if(hit){hit.id='__pick_lib';'COVER-MENU-OK'}else{'NO-LIB-ITEM menu=['+seen.join('/')+']'}",
            ],
            ["click", "#__pick_lib"],
            ["wait", "6000"],
            [
                "eval",
                "var its=[...document.querySelectorAll('.weui-desktop-img-picker__item')];"
                f"var hit=its.filter(e=>(e.innerText||'').indexOf({js(cover_prefix)})>=0);"
                "if(!hit.length)'NO-LIB-MATCH';else{hit[0].id='__it';'LIB-MATCH'}",
            ],
            ["click", "#__it"],
            ["wait", "2500"],
            [
                "eval",
                "var bs=[...document.querySelectorAll('button,a')].filter(b=>(b.innerText||'').trim()==='下一步'&&b.offsetParent);"
                "bs.length?(bs[0].id='__next','COVER-NEXT'):'NO-NEXT'",
            ],
            ["click", "#__next"],
            ["wait", "8000"],
            [
                "eval",
                "var bs=[...document.querySelectorAll('button,a')].filter(b=>(b.innerText||'').trim()==='确认'&&b.offsetParent);"
                "bs.length?(bs[0].id='__ok','COVER-CONFIRM'):'NO-CONFIRM'",
            ],
            ["click", "#__ok"],
            ["wait", "6000"],
            [
                "eval",
                # 可靠的封面信号：设好之后 .js_cover_preview_new 的 background-image 指向 mmbiz CDN。
                # 千万别用「拖拽或选择封面」这段文字判断——它始终留在 DOM 里（隐藏），永远为真。
                "var p=document.querySelector('.js_cover_preview_new');"
                "var bg=p?String(getComputedStyle(p).backgroundImage||''):'';"
                "'COVER_URL='+(bg.indexOf('mmbiz')>=0?'YES':'NO')",
            ],
        ]

    # 合集（公众号的「分类栏」）。2026-10-20 实测打通，六步缺一不可：
    #   1) 勾 input.js_article_tags —— 这个复选框是活的（和「原文链接」的 source_url_checked 不一样，那个勾不动）
    #   2) 点 .js_article_tags_label（「未添加」）打开设置面板
    #   3) 点 input[placeholder="请选择合集"] —— 它是**下拉框**，不点开不列选项
    #   4) 在下拉项里按文本点中目标合集
    #   5) 点「确认」
    #   6) 随后照常「保存为草稿」
    # 限制：**每篇文章最多挂 1 个合集**（面板上明写），所以 manifest 的 collection 是单个字符串。
    if collection:
        cmds += [
            ["scrollintoview", "#js_article_tags_area"],
            ["wait", "900"],
            [
                "eval",
                # 只在没勾时点。已勾时再点会把它取消，后面下拉框就出不来了（实测踩过：
                # 草稿已挂过合集，重跑脚本反而把合集清空）。
                "var c=document.querySelector('#js_article_tags_area input.js_article_tags');"
                "c?((c.checked||c.click()),'COLL-CK='+c.checked):'NO-COLL-CK'",
            ],
            ["wait", "1800"],
            [
                "eval",
                "var l=document.querySelector('#js_article_tags_area .js_article_tags_label');"
                "l?(l.click(),'COLL-PANEL'):'NO-COLL-PANEL'",
            ],
            ["wait", "3500"],
            [
                "eval",
                "var i=document.querySelector('input[placeholder=\"请选择合集\"]');"
                "i?(i.click(),'COLL-DD'):'NO-COLL-DD'",
            ],
            ["wait", "2200"],
            [
                "eval",
                "var o=[...document.querySelectorAll('*')].filter("
                "e=>e.children.length<=1&&(e.innerText||'').trim()===" + js(collection) +
                "&&e.getBoundingClientRect().width>0);"
                "o.length?(o[o.length-1].click(),'COLL-PICK'):'NO-COLL-OPT'",
            ],
            ["wait", "1800"],
            [
                "eval",
                "var b=[...document.querySelectorAll('button,a,div')].filter(e=>{"
                "var r=e.getBoundingClientRect();"
                "return (e.innerText||'').trim()==='确认'&&r.width>30&&r.width<100&&r.height>18;});"
                "b.length?(b[b.length-1].click(),'COLL-OK'):'NO-COLL-OK'",
            ],
            ["wait", "2200"],
            [
                "eval",
                "'COLLECTION='+((document.body.innerText.match(/合集\\s*(\\S{1,14})/)||['','?'])[1])",
            ],
        ]

    # 保存为草稿（只此一步，绝不点发表）。
    # 点之前**再核对一次结构**：块数 / 加粗 / 带样式元素 / inline-block 多列，
    # 四项任一少于源文件就不保存、直接报错，避免又存出一篇「挤成一坨」或「多列塌成单列」的稿子。
    expect = scan_body(body)["stats"]
    cmds += [
        ["wait", "800"],
        [
            "eval",
            "var b=document.querySelectorAll('.ProseMirror')[1];"
            "var q=function(s){return b?b.querySelectorAll(s).length:0};"
            "var nb=q('section'),ns=q('strong'),nst=q('[style]'),"
            "nib=q('[style*=\"inline-block\"]');"
            "var s=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()==='保存为草稿');"
            "!s?'NO-SAVE-BTN':"
            f"(nb<{expect.get('section', 0)}||ns<{expect.get('strong', 0)}"
            f"||nst<{expect.get('styled', 0)}||nib<{expect.get('iblock', 0)})"
            "?('SKIP-SAVE-BAD-BODY nb='+nb+' ns='+ns+' nst='+nst+' nib='+nib)"
            ":(s.click(),'CLICKED-SAVE')",
        ],
        ["wait", "4000"],
        ["eval", "location.href.slice(0,200)"],
    ]

    # 封面图必须先躺在素材库里，才能「从图片库选择」。这一步放在整个 batch 最前面
    # （素材库页面有可用的 input[type=file]，上传是可行的；编辑器里的那个 file input 是正文插图用的）。
    if cover_abs is not None and cover_upload is not None:
        lib_url = (
            f"https://mp.weixin.qq.com/cgi-bin/filepage?type=2&begin=0&count=12&token={token}&lang=zh_CN"
        )
        cmds = (
            [
                ["open", lib_url],
                ["wait", "7000"],
                ["upload", "input[type=file]", str(cover_upload)],
                ["wait", "9000"],
                ["eval", "'LIB-UPLOADED'"],
            ]
            + cmds
        )
    return cmds


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="把一篇成稿写进公众号草稿箱（只存草稿）")
    ap.add_argument("--manifest", required=True, help="文章 manifest JSON 路径")
    ap.add_argument("--appmsgid", default=None, help="续编辑已有草稿的 appmsgid（不给则新建）")
    ap.add_argument("--profile", default=None, help="登录态目录，默认 <工作区>/.cache/browser-profile")
    ap.add_argument("--ab", default=None, help="agent-browser 可执行文件路径")
    ap.add_argument(
        "--collection",
        default=None,
        help="挂到哪个合集（公众号的分类栏）。每篇最多 1 个；不给则读 manifest 的 collection 字段",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的 batch，不实际运行")
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="只做标签/结构校验并打印统计：不开浏览器、不写入后台。写稿时随手验",
    )
    args = ap.parse_args(argv)

    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.exists():
        raise SystemExit(f"manifest 不存在：{manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent

    title = (data.get("title") or "").strip()
    if not title:
        raise SystemExit("manifest 缺少 title")

    if data.get("body_html_path"):
        body = (base / data["body_html_path"]).read_text(encoding="utf-8").strip()
    else:
        body = (data.get("body_html") or "").strip()
    if not body:
        raise SystemExit("manifest 缺少正文（body_html 或 body_html_path）")

    # 标签/结构校验放在**开浏览器之前**：错误在这里就止步，不浪费一次登录态。
    scan = scan_body(body)
    if args.check_only:
        report = {
            "manifest": str(Path(args.manifest).resolve()),
            "errors": scan["errors"],
            "warnings": scan["warnings"],
            "stats": scan["stats"],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not scan["errors"] else 2
    if scan["errors"]:
        for p in scan["errors"][:20]:
            print(f"  L{p['line']} <{p['tag']}> {p['msg']}", file=sys.stderr)
        raise SystemExit(
            f"正文有 {len(scan['errors'])} 处标签问题，没有写入后台。"
            "（只允许 section/span/strong/br/img；顶层必须是 section；strong 不能嵌在 span 里）"
        )

    cover_abs: Path | None = None
    cover_upload: Path | None = None
    cover_prefix = ""
    if data.get("cover_path"):
        cover_abs = (base / data["cover_path"]).resolve()
        if not cover_abs.exists():
            raise SystemExit(f"封面图不存在：{cover_abs}")
        cover_upload, cover_prefix = prepare_cover_upload(cover_abs)

    profile = Path(args.profile).expanduser().resolve() if args.profile else workroot() / ".cache" / "browser-profile"
    ab = find_ab(args.ab)

    print(f"[1/3] 读取登录态 token（profile: {profile}）", file=sys.stderr)
    token = get_token(ab, profile)
    print(f"      token = {token}", file=sys.stderr)

    cmds = build_commands(
        token=token,
        appmsgid=args.appmsgid,
        title=title,
        summary=(data.get("summary") or "").strip(),
        author=(data.get("author") or "").strip(),
        source_url=(data.get("source_url") or "").strip(),
        body=body,
        cover_abs=cover_abs,
        cover_upload=cover_upload,
        cover_prefix=cover_prefix,
        collection=(args.collection or data.get("collection") or "").strip(),
    )
    want_source = bool((data.get("source_url") or "").strip())

    if args.dry_run:
        print(json.dumps(cmds, ensure_ascii=False, indent=1))
        return 0

    print(f"[2/3] 写入编辑器并保存草稿（正文 {len(body)} 字符）", file=sys.stderr)
    out = run_batch(ab, profile, cmds)
    if os.environ.get("GZH_DEBUG"):
        print("----- batch 原始输出 -----", file=sys.stderr)
        print(out, file=sys.stderr)
        print("----- 结束 -----", file=sys.stderr)

    if "NO-SAVE-BTN" in out:
        print(out, file=sys.stderr)
        raise SystemExit("没找到「保存为草稿」按钮，页面结构可能变了。上面的输出里有现场信息。")
    if "SKIP-SAVE-BAD-BODY" in out:
        print(out, file=sys.stderr)
        raise SystemExit(
            "正文结构没保住，已主动放弃保存。"
            f"源文件：section={expect.get('section', 0)} strong={expect.get('strong', 0)} "
            f"styled={expect.get('styled', 0)} iblock={expect.get('iblock', 0)}；"
            "编辑器里的实际数量见上面的 SKIP-SAVE-BAD-BODY 行。"
        )
    if "BODY_LEN=" not in out:
        print(out, file=sys.stderr)
        raise SystemExit("正文注入似乎没成功（输出里没有 BODY_LEN）。")

    m = APPMSGID_RE.search(out)
    if not m:
        print(out, file=sys.stderr)
        raise SystemExit("保存动作执行了，但没拿到 appmsgid，请人工到草稿箱确认。")

    draft_id = m.group(1)
    print(f"[3/3] 完成：草稿已保存，appmsgid = {draft_id}", file=sys.stderr)

    # 「原文链接」字段的启用复选框无法程序化勾选（实测：点 label / 点 input / 直接赋 value / fill 都不生效），
    # 因此这里如实上报是否真的写进去了，不做乐观假设。
    source_applied: bool | None = None
    if want_source:
        source_applied = "SRC=https" in out
        if not source_applied:
            print(
                "      ⚠ 原文链接未写入（该字段的启用开关无法程序化勾选）。请在草稿里手动勾选「原文链接」后粘贴。",
                file=sys.stderr,
            )

    cover_ok: bool | None = None
    if cover_abs is not None:
        cover_ok = "COVER_URL=YES" in out
        if cover_ok:
            print(f"      封面已设置（素材库 {cover_prefix}，CDN 背景图已挂上）", file=sys.stderr)
        else:
            print(
                "      ⚠ 封面未确认（COVER_URL 不是 YES）。可能是：库里没匹配到图、裁剪未确认，或该草稿本来就没封面。请打开草稿看一眼。",
                file=sys.stderr,
            )

    print(
        json.dumps(
            {
                "ok": True,
                "appmsgid": draft_id,
                "title": title,
                "body_chars": len(body),
                "cover": str(cover_abs) if cover_abs else None,
                "cover_applied": cover_ok,
                "cover_library_name": cover_prefix or None,
                "source_url_applied": source_applied,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "editor_url": f"https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&type=77&appmsgid={draft_id}&token={token}&lang=zh_CN",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
