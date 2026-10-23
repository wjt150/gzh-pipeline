#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给雷达页做两件事：删掉「建议角度」，给每条加「一键发送」按钮。

为什么删建议角度：那是给人类编辑看的工作提示。现在稿子由 agent 写，
留着它等于把内部思路印在交付物上，读者/用户都不需要。

为什么加一键发送：静态 HTML 点按钮无法直接叫醒 agent，所以按钮 POST 到
`scripts/radar_server.py`（本机 127.0.0.1:8899），服务把「哪期、哪条、什么轨道」
写进 `.cache/send-queue.jsonl`。agent 下次开工读队列即可。
服务没起时按钮降级为「复制指令」，不让用户卡住。

用法：
    python scripts/radar_enrich.py 雷达/2026-10-19-2.html          # 原地改
    python scripts/radar_enrich.py 雷达/*.html --port 8899
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CARD_RE = re.compile(r'<div class="meta2">(.*?)</div>', re.S)
ONE_RE = re.compile(r'<div class="one">(.*?)</div>', re.S)
NUM_RE = re.compile(r'<span class="num">(\d+)</span>')
H3_RE = re.compile(r"<h3>.*?</h3>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
ISSUE_RE = re.compile(r"第\s*(\d+)\s*期")

CSS = """
  .sendbtn { margin-left:auto; border:0; background:var(--accent); color:#fff; border-radius:8px;
             padding:3px 12px; font-size:12px; cursor:pointer; font-family:inherit; line-height:1.6; }
  .sendbtn:hover { filter:brightness(1.06); }
  .sendbtn.sent { background:#9aa5ac; cursor:default; }
  .sendbtn.fail { background:var(--warn); }
  .sendbtn:disabled { opacity:.9; }
"""

JS = """
<div id="sendtip" style="position:fixed;left:50%;transform:translateX(-50%);bottom:18px;background:#1a1a1a;color:#fff;
     padding:8px 16px;border-radius:20px;font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:99;"></div>
<script>
const ISSUE = "%ISSUE%";
const ORIGINS = ["", "http://127.0.0.1:%PORT%"];
function tip(msg){
  const el = document.getElementById("sendtip");
  el.textContent = msg; el.style.opacity = 1;
  clearTimeout(window.__tipT);
  window.__tipT = setTimeout(() => el.style.opacity = 0, 2600);
}
async function sendOne(btn){
  if (btn.classList.contains("sent")) return;
  const payload = {issue: ISSUE, ids:[btn.dataset.id], track: btn.dataset.track, title: btn.dataset.title || ""};
  for (const origin of ORIGINS){
    try {
      const r = await fetch((origin || "") + "/send", {
        method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)
      });
      if (r.ok){ btn.classList.add("sent"); btn.textContent = "已提交 · 待处理"; tip("已入队：" + btn.dataset.id); return; }
    } catch(e){ /* 换下一个 origin */ }
  }
  const cmd = "发 " + btn.dataset.id + "（" + (btn.dataset.track === "compile" ? "编译" : "改写") + "）";
  btn.classList.add("fail"); btn.textContent = "已复制指令";
  try { await navigator.clipboard.writeText(cmd); tip("发送服务未启动，指令已复制：" + cmd); }
  catch(e){ tip("发送服务未启动，请手动告诉 agent：" + cmd); }
}
document.addEventListener("click", (e) => {
  const b = e.target.closest(".sendbtn");
  if (b) sendOne(b);
});
</script>
"""


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub("", s)).strip()


def button(num: str, track: str, title: str) -> str:
    return (
        f'<button class="sendbtn" data-id="{num}" data-track="{track}" '
        f'data-title="{title.replace(chr(34), "&quot;")[:90]}">一键发送</button>'
    )


def enrich(html: str, port: int) -> str:
    # 1) 删掉建议角度（整块）
    html = re.sub(r'\s*<div class="angle">.*?</div>', "", html, flags=re.S)

    # 2) 卡片：在 meta2 末尾加按钮。编号取该卡片里最近的 <span class="num">
    def card_sub(m: re.Match) -> str:
        inner = m.group(1)
        before = html[: m.start()]
        nums = NUM_RE.findall(before)
        if not nums:
            return m.group(0)
        num = nums[-1]
        track = "compile" if "t-cmp" in inner else "rewrite"
        h3s = H3_RE.findall(before)
        title = strip_tags(h3s[-1]) if h3s else ""
        title = re.sub(r"^\d+\s*", "", title)
        return f'<div class="meta2">{inner}{button(num, track, title)}</div>'

    html = CARD_RE.sub(card_sub, html)

    # 3) 待观察行：同样加按钮，编号取 <b>NN</b>
    def one_sub(m: re.Match) -> str:
        inner = m.group(1)
        mb = re.search(r"<b>(\d+)</b>", inner)
        if not mb:
            return m.group(0)
        track = "compile" if "t-cmp" in inner else "rewrite"
        title = strip_tags(inner)[:80]
        return f'<div class="one">{inner}{button(mb.group(1), track, title)}</div>'

    html = ONE_RE.sub(one_sub, html)

    # 4) 注入样式与脚本
    if "</style>" in html and ".sendbtn" not in html:
        html = html.replace("</style>", CSS + "</style>", 1)
    if "</body>" in html and "sendOne" not in html:
        mi = ISSUE_RE.search(html)
        issue = mi.group(1) if mi else ""
        block = JS.replace("%ISSUE%", issue).replace("%PORT%", str(port))
        html = html.replace("</body>", block + "\n</body>", 1)
    return html


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给雷达页去掉建议角度并加一键发送")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--port", type=int, default=8899, help="radar_server.py 的端口")
    args = ap.parse_args(argv)

    for name in args.files:
        p = Path(name)
        if not p.exists():
            print(f"跳过（不存在）：{p}", file=sys.stderr)
            continue
        src = p.read_text(encoding="utf-8")
        out = enrich(src, args.port)
        p.write_text(out, encoding="utf-8")
        n_btn = out.count('class="sendbtn"')
        n_ang = out.count('class="angle"')
        print(f"{p.name}: 按钮 {n_btn} 个，残留建议角度 {n_ang} 处")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
