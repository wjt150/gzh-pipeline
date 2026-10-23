#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点「发表」——本项目唯一不可逆的动作，单独成脚本，与存草稿彻底隔离。

实测流程（2026-10-19，个人订阅号）共三层：
    1. 点编辑器里的「发表」 → 弹窗（群发通知 / 分组通知 / 定时发表 / 发表 / 取消）
    2. 点弹窗里的「发表」   → 二次确认（「已开启群发通知…」→「继续发表」/「取消」）
    3. 点「继续发表」       → **微信验证扫码**（管理员/运营者微信可直接扫）
       这一步之后内容才真正发出，且**无法由程序完成**。

所以本脚本的定位是：**把人送到二维码面前，最后一下由人扫**。

⚠ 2026-10-19 已验证过两条「可能绕过扫码」的猜想，**都绕不过**：
   ① 关掉「群发通知」开关 → 二次确认文案变成「未开启群发通知…」，但点「继续发表」照样弹二维码；
   ② 打开「定时发表」（第 3 个 switch，时间框 `input.weui-desktop-form__input` 可写入）→ 照样弹二维码。
   → 扫码绑定的是「发表」这个动作，与是否群发、是否定时无关。不要再重复这两个实验。

模式：
    --mode dry-run   只跑闸门，不点任何东西（默认）
    --mode dialog    点到第一层弹窗就停，打印弹窗内容
    --mode qr        一路点到扫码弹窗，截图存证后停止，等你扫码

退出码：0 = 正常（含"闸门没通过""停在扫码"这类预期结果）；1 = 环境/登录异常。

使用：
    python scripts/gzh_publish.py --appmsgid 100000042 --mode dry-run
    python scripts/gzh_publish.py --appmsgid 100000042 --mode qr
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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gzh_lock  # noqa: E402
import gzh_state  # noqa: E402

TOKEN_RE = re.compile(r"[?&]token=(\d+)")
HOME_URL = "https://mp.weixin.qq.com/"
AB_ARGS = "--no-sandbox,--do-not-de-elevate"

# 视口必须够大：发表弹窗与二维码弹窗的底部按钮在 1080×498 下会被切到视口外，
# 点击坐标全部打空（封面流程踩过同一个坑）。
VIEWPORT = ("1440", "1200")


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
    raise SystemExit("找不到 agent-browser")


HEADED = False  # 由 --headed 打开：二维码必须让人在窗口里扫，headless 只能截图


def run_batch(ab: str, profile: Path, commands: list[list[str]], timeout: int = 300) -> str:
    """重定向到临时文件而不是管道——浏览器守护进程会占用管道导致父进程永久等待。"""
    payload = json.dumps(commands, ensure_ascii=False)
    cmd = [ab, "--args", AB_ARGS, "--profile", str(profile), "--max-output", "8000"]
    if HEADED:
        cmd.append("--headed")
    cmd.append("batch")
    fd, logname = tempfile.mkstemp(prefix="gzh-pub-", suffix=".log")
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
                raise SystemExit(f"agent-browser batch 超时（{timeout}s）")
        return Path(logname).read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            os.unlink(logname)
        except OSError:
            pass


def get_token(ab: str, profile: Path) -> str:
    out = run_batch(ab, profile, [["open", HOME_URL], ["wait", "4000"], ["eval", "location.href"]], 120)
    m = TOKEN_RE.search(out)
    if m:
        return m.group(1)
    raise SystemExit(
        "登录态已失效（后台停在登录页）。\n"
        f'请重新扫码：agent-browser --args "{AB_ARGS}" --profile "{profile}" --headed open {HOME_URL}'
    )


# --------------------------------------------------------------------------- #
# 闸门
# --------------------------------------------------------------------------- #
def local_gates(root: Path, appmsgid: str | None = None) -> list[dict]:
    """本地闸门。**「今天还没发表过」改读台账**——旧版读 auto-runs 的 published 字段，
    而那个字段全仓没有一处置 true，闸门是死的（同一天会重复发）。
    """
    gates: list[dict] = []

    stop = root / ".cache" / "STOP_AUTO"
    gates.append(
        {"name": "急停文件不存在", "ok": not stop.exists(), "detail": str(stop) if stop.exists() else "OK"}
    )

    pub = gzh_state.published_today(root)
    gates.append({"name": "今天还没发表过", "ok": not pub["hit"], "detail": pub["detail"]})

    if appmsgid:
        dup = gzh_state.published_row_for(root, appmsgid)
        gates.append(
            {
                "name": "这一篇没发过",
                "ok": dup is None,
                "detail": "已发表过" if dup else "OK",
            }
        )
    return gates


def verify_gate(record_path: str | None) -> dict:
    """原创标核验闸门。给 --verify-record 才检查；不给则视为「本条不需要核验」（轨道 B 原创聚合）。"""
    if not record_path:
        return {"name": "原创标核验（轨道B跳过）", "ok": True, "detail": "未提供核验记录，按原创聚合处理"}
    p = Path(record_path)
    if not p.exists():
        return {"name": "原创标核验", "ok": False, "detail": f"核验记录不存在：{p}"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"name": "原创标核验", "ok": False, "detail": f"核验记录读取失败：{exc}"}
    ok = data.get("copyright_stat") == 0 or data.get("authorized") is True
    return {"name": "原创标核验", "ok": ok, "detail": json.dumps(data, ensure_ascii=False)[:160]}


def read_gates(ab: str, profile: Path, editor: str, allow_missing_cover: bool) -> tuple[list[dict], dict]:
    """打开草稿读真实内容。

    每个 eval 都返回「带前缀的字符串」——返回数字的 eval 不带引号，
    按行号+引号解析会整体错位（实测踩过）。
    封面判断必须用 `.js_cover_preview_new` 的 background-image：
    「拖拽或选择封面」那段文案始终留在 DOM 里（隐藏），用它判断永远为真。
    """
    cmds = [
        ["open", editor],
        ["set", "viewport", VIEWPORT[0], VIEWPORT[1]],
        ["wait", "8000"],
        ["eval", "var t=document.querySelectorAll('.ProseMirror')[0];'TITLE='+(t?t.innerText.replace(/\\s+/g,' ').slice(0,120):'NONE')"],
        ["eval", "var b=document.querySelectorAll('.ProseMirror')[1];'BODYLEN='+(b?b.innerText.length:0)"],
        [
            "eval",
            "var p=document.querySelector('.js_cover_preview_new');"
            "var bg=p?String(getComputedStyle(p).backgroundImage||''):'';"
            "'COVER='+(bg.indexOf('mmbiz')>=0?'yes':'no')",
        ],
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='发表'&&b.offsetParent;});"
            "'BTN='+(bs.length?'yes':'no')",
        ],
    ]
    out = run_batch(ab, profile, cmds, 200)

    def grab(pattern: str, default: str = "") -> str:
        m = re.search(pattern, out)
        return m.group(1).strip() if m else default

    title = grab(r"TITLE=([^\n\"]*)", "NONE")
    try:
        body_len = int(grab(r"BODYLEN=(\d+)", "0"))
    except ValueError:
        body_len = 0
    cover_set = grab(r"COVER=(yes|no)", "no") == "yes"
    has_btn = grab(r"BTN=(yes|no)", "no") == "yes"

    gates = [
        {"name": "标题非空且 ≤64 字", "ok": 0 < len(title) <= 64 and title != "NONE", "detail": title[:70]},
        {"name": "正文 ≥300 字", "ok": body_len >= 300, "detail": f"{body_len} 字"},
        {
            "name": "封面已设置" + ("（诊断模式已放行）" if allow_missing_cover and not cover_set else ""),
            "ok": cover_set or allow_missing_cover,
            "detail": "封面区背景图已指向微信 CDN" if cover_set else "封面区无 CDN 背景图（未设封面）",
        },
        {"name": "发表按钮存在", "ok": has_btn, "detail": "yes" if has_btn else "no"},
    ]
    return gates, {"title": title, "body_len": body_len, "cover_set": cover_set}


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def act_to_dialog(ab: str, profile: Path, editor: str) -> str:
    """点编辑器里的「发表」，停在第一层弹窗。"""
    cmds = [
        ["open", editor],
        ["set", "viewport", VIEWPORT[0], VIEWPORT[1]],
        ["wait", "8000"],
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='发表'&&b.offsetParent;});"
            "bs.length?(bs[bs.length-1].id='__pub','PUB'):'NO-PUB'",
        ],
        ["click", "#__pub"],
        ["wait", "7000"],
        ["eval", "'DIALOG='+(document.body.innerText.indexOf('群发通知')>=0?'yes':'no')"],
        [
            "eval",
            "var t=document.body.innerText.replace(/\\s+/g,' ');"
            "var i=t.indexOf('群发通知');"
            "'DLGINFO='+(i>=0?t.slice(i-10,i+120):'NO-DIALOG-TEXT')",
        ],
    ]
    return run_batch(ab, profile, cmds, 200)


def act_to_qr(ab: str, profile: Path, editor: str, shot: Path) -> str:
    """一路点到扫码弹窗。这是本脚本能自动走的最远位置。

    每一步都用「先打标记再 click」而不是 find：
    弹窗里的「发表」有两个同名按钮（编辑器一个、弹窗一个），
    必须用「是否有 dialog 祖先」区分。
    """
    cmds = [
        ["open", editor],
        ["set", "viewport", VIEWPORT[0], VIEWPORT[1]],
        ["wait", "8000"],
        # ① 编辑器里的「发表」
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='发表'&&b.offsetParent;});"
            "bs.length?(bs[bs.length-1].id='__pub','PUB'):'NO-PUB'",
        ],
        ["click", "#__pub"],
        ["wait", "7000"],
        # ② 弹窗里的「发表」
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='发表';});"
            "var d=bs.filter(function(b){var p=b;for(var i=0;i<9&&p;i++){if(String(p.className||'').indexOf('dialog')>=0)return true;p=p.parentElement;}return false;});"
            "d.length?(d[0].id='__pub2','DLG-PUB'):'NO-DLG-PUB'",
        ],
        ["click", "#__pub2"],
        ["wait", "7000"],
        # ③ 二次确认的「继续发表」
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='继续发表'&&b.offsetParent;});"
            "bs.length?(bs[0].id='__go','GO'):'NO-GO'",
        ],
        ["click", "#__go"],
        ["wait", "15000"],
        # ④ 判定到了哪一步
        [
            "eval",
            "var t=document.body.innerText.replace(/\\s+/g,' ');"
            "'SCAN='+((t.indexOf('扫码')>=0||t.indexOf('管理员')>=0)?'yes':'no')",
        ],
        [
            "eval",
            "var t=document.body.innerText.replace(/\\s+/g,' ');"
            "var i=t.indexOf('扫码');"
            "'STAGE='+(i>=0?t.slice(Math.max(0,i-60),i+140):t.slice(0,120))",
        ],
        ["screenshot", str(shot)],
    ]
    return run_batch(ab, profile, cmds, 320)


def act_to_qr_wait(
    ab: str,
    profile: Path,
    editor: str,
    shot: Path,
    wait_s: int,
    appmsgid: str,
    token: str,
    title: str | None = None,
) -> dict:
    """推到二维码后**继续等扫码，扫完自己收尾，最后回发表记录核对**。

    为什么必须在一个 batch 里做完：扫码弹窗是页面状态，跨进程就丢了。

    为什么最后要去发表记录：弹窗里的文字不可靠（10-22 实测，扫完几秒内页面仍显示「扫码」），
    只有 `app_msg_list` 里出现这条 appmsgid 才是真的发出去了。

    返回 {published, dialog_done, list_hit, rounds, evidence}
    """
    interval_s = 12
    rounds = max(1, wait_s // interval_s)
    list_url = (
        "https://mp.weixin.qq.com/cgi-bin/appmsg?begin=0&count=6&type=77"
        f"&action=list_ex&token={token}&lang=zh_CN"
    )

    pub_list_url = (
        "https://mp.weixin.qq.com/cgi-bin/appmsgpublish?sub=list&begin=0&count=6"
        f"&token={token}&lang=zh_CN"
    )
    cnt_js = (
        "var t=document.body.innerText.replace(/\\s+/g,' ');"
        "var m=t.match(/共发表了 (\\d+) 次/);"
    )
    cmds: list[list[str]] = [
        # 先记基线：发布后 appmsgid 会变（实测 100000067→100000089），
        # 所以只能用「发表总次数 +1」来判定，不能按 id 找。
        ["open", pub_list_url],
        ["wait", "6000"],
        ["eval", cnt_js + "'CNT0='+(m?m[1]:'-1')"],
        ["open", editor],
        ["set", "viewport", VIEWPORT[0], VIEWPORT[1]],
        ["wait", "8000"],
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='发表'&&b.offsetParent;});"
            "bs.length?(bs[bs.length-1].id='__pub','PUB'):'NO-PUB'",
        ],
        ["click", "#__pub"],
        ["wait", "7000"],
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='发表';});"
            "var d=bs.filter(function(b){var p=b;for(var i=0;i<9&&p;i++){if(String(p.className||'').indexOf('dialog')>=0)return true;p=p.parentElement;}return false;});"
            "d.length?(d[0].id='__pub2','DLG-PUB'):'NO-DLG-PUB'",
        ],
        ["click", "#__pub2"],
        ["wait", "7000"],
        [
            "eval",
            "var bs=[...document.querySelectorAll('button,a')].filter(function(b){return (b.innerText||'').trim()==='继续发表'&&b.offsetParent;});"
            "bs.length?(bs[0].id='__go','GO'):'NO-GO'",
        ],
        ["click", "#__go"],
        ["wait", "15000"],
        [
            "eval",
            "var t=document.body.innerText.replace(/\\s+/g,' ');"
            "'SCAN='+((t.indexOf('扫码')>=0||t.indexOf('管理员')>=0)?'yes':'no')",
        ],
        ["screenshot", str(shot)],
    ]

    # 等待扫码：每轮先判状态，再点掉可能出现的确认按钮（幂等）
    for i in range(rounds):
        cmds += [
            ["wait", str(interval_s * 1000)],
            [
                "eval",
                "var t=document.body.innerText.replace(/\\s+/g,' ');"
                f"'R{i}='+((t.indexOf('发表成功')>=0||t.indexOf('已发表')>=0)?'DONE':"
                "(t.indexOf('扫码')>=0?'WAIT':'OTHER'))",
            ],
            [
                "eval",
                "var bs=[...document.querySelectorAll('button,a')].filter(function(b){"
                "var x=(b.innerText||'').trim();"
                "return b.offsetParent&&(x==='确认'||x==='完成'||x==='我知道了'||x==='确定');});"
                f"bs.length?(bs[bs.length-1].click(),'C{i}-'+bs.length):'N{i}'",
            ],
        ]

    # 权威判定：回「发表记录」看总次数有没有 +1、标题在不在
    cmds += [
        ["open", pub_list_url],
        ["wait", "7000"],
        ["eval", cnt_js + "'CNT1='+(m?m[1]:'-1')"],
        [
            "eval",
            ("var t=document.body.innerText.replace(/\\s+/g,' ');"
             f"var tt={title!r};"
             "'TITLEHIT='+((tt&&t.indexOf(tt.slice(0,12))>=0)?'yes':'no')"),
        ],
    ]

    out = run_batch(ab, profile, cmds, wait_s + 260)
    dialog_done = bool(re.search(r"R\d+=DONE", out))
    c0 = re.search(r"CNT0=(\d+|-)1?", out)
    c1 = re.search(r"CNT1=(\d+|-)1?", out)
    title_hit = "TITLEHIT=yes" in out
    n0 = int(c0.group(1)) if c0 and c0.group(1).isdigit() else None
    n1 = int(c1.group(1)) if c1 and c1.group(1).isdigit() else None
    cnt_up = bool(n0 is not None and n1 is not None and n1 > n0)
    published = cnt_up or (title_hit and dialog_done) or title_hit
    evidence = (
        f"发表记录 {n0}→{n1}" if n0 is not None and n1 is not None else "count-unknown"
    ) + ("，标题已出现" if title_hit else "") + ("，弹窗显示成功" if dialog_done else "")
    return {
        "published": published,
        "dialog_done": dialog_done,
        "list_hit": cnt_up,
        "rounds": rounds,
        "evidence": evidence,
        "counts": {"before": n0, "after": n1},
    }


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="点发表（不可逆动作，默认只跑闸门）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--appmsgid", default=None, help="直接指定草稿 id")
    src.add_argument("--from-queue", action="store_true", help="从稿件队列取下一篇（推荐）")
    ap.add_argument("--mode", choices=["dry-run", "dialog", "qr", "qr-wait"], default="dry-run")
    ap.add_argument(
        "--wait-scan",
        type=int,
        default=120,
        help="qr-wait 模式下等扫码的秒数（默认 120；每 12 秒查一次）",
    )
    ap.add_argument("--verify-record", default=None, help="原创标核验记录 JSON（轨道 A 必给）")
    ap.add_argument(
        "--allow-missing-cover",
        action="store_true",
        help="诊断用：封面未设也放行（仅用于探明流程，日常不要开）",
    )
    ap.add_argument("--plan-only", action="store_true", help="只解析队列与闸门，不开浏览器")
    ap.add_argument("--headed", action="store_true", help="用可见窗口（二维码要让人扫，必须开）")
    ap.add_argument("--max-age", type=int, default=4, help="队列里允许多老的稿（天）")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--ab", default=None)
    args = ap.parse_args(argv)

    global HEADED
    HEADED = bool(args.headed)

    root = workroot()

    # ---------- 取稿：从队列或直给 ----------
    queue_dir: Path | None = None
    if args.from_queue:
        picked = queue_pick(root, args.max_age)
        if picked is None:
            print(json.dumps({"ok": False, "reason": "queue_empty",
                              "advice": "python scripts/gzh_queue.py next"}, ensure_ascii=False, indent=2))
            return 2
        args.appmsgid = picked["appmsgid"]
        queue_dir = root / picked["dir"]
        print(f"[队列] 选中 {picked['appmsgid']} · {picked['title']}（{picked['age_days']} 天前）",
              file=sys.stderr)

    # ---------- 锁（gzh_auto 持锁调本脚本时透传，避免自杀）----------
    held = os.environ.get("GZH_LOCK_HELD") == "1"
    lock_path = None
    if not held:
        ok, lock_path = gzh_lock.acquire(root)
        if not ok:
            print(json.dumps({"ok": False, "halted": "locked", "detail": lock_path}, ensure_ascii=False))
            return 10
    try:
        return _run(args, root, queue_dir)
    finally:
        gzh_lock.release(lock_path)


def queue_pick(root: Path, max_age: int) -> dict | None:
    """复用 gzh_queue 的排序，保证「选」和「发」是同一套规则。"""
    import gzh_queue

    items, _ = gzh_queue.collect(datetime.now().date(), max_age)
    cands = gzh_queue.rank(gzh_queue.candidates(items))
    return cands[0] if cands else None


def _run(args, root: Path, queue_dir: Path | None) -> int:
    profile = Path(args.profile).resolve() if args.profile else root / ".cache" / "browser-profile"
    ab = None if args.plan_only else find_ab(args.ab)

    runs_dir = root / ".cache" / "auto-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")

    report: dict = {
        "appmsgid": args.appmsgid,
        "mode": args.mode,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "gates": [],
        "published": False,
        "needs_scan": False,
        "observed": None,
        "screenshot": None,
        "error": None,
        "queue_dir": str(queue_dir.relative_to(root).as_posix()) if queue_dir else None,
    }

    report["gates"] += local_gates(root, args.appmsgid)
    report["gates"].append(verify_gate(args.verify_record))

    # plan-only：闸门跑完就停，不碰浏览器
    if args.plan_only:
        report["observed"] = "plan-only：只跑本地闸门"
        report["plan"] = {
            "selected": args.appmsgid,
            "queue_dir": report["queue_dir"],
            "gates_ok": all(g["ok"] for g in report["gates"]),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(g["ok"] for g in report["gates"]) else 4

    token = get_token(ab, profile)
    editor = (
        "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media_appmsg_edit&action=edit"
        f"&type=77&appmsgid={args.appmsgid}&token={token}&lang=zh_CN"
    )

    gates, info = read_gates(ab, profile, editor, args.allow_missing_cover)
    report["gates"] += gates
    report["content"] = info
    all_ok = all(g["ok"] for g in report["gates"])

    if args.mode == "dry-run" or not all_ok:
        report["observed"] = "闸门全过但按 dry-run 停在只读" if all_ok else "闸门未全通过，不点发表"
        (runs_dir / f"{stamp}-publish-{args.appmsgid}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "dialog":
        out = act_to_dialog(ab, profile, editor)
        report["observed"] = "已点开第一层发表弹窗，未继续"
        report["dialog"] = _grab(out, r"DLGINFO=([^\n\"]*)")
        report["popup_opened"] = _grab(out, r"DIALOG=(yes|no)") == "yes"
    elif args.mode == "qr-wait":
        shot = runs_dir / f"{stamp}-publish-{args.appmsgid}-qr.png"
        res = act_to_qr_wait(
            ab, profile, editor, shot, args.wait_scan, str(args.appmsgid), token,
            (report.get("content") or {}).get("title"),
        )
        report["published"] = res["published"]
        report["needs_scan"] = True
        report["rounds"] = res["rounds"]
        report["evidence"] = res["evidence"]
        report["screenshot"] = str(shot) if shot.exists() else None
        report["observed"] = (
            "已发表（发表记录里已出现这条）"
            if res["published"]
            else f"等了 {args.wait_scan} 秒没等到扫码，草稿仍在，可重跑"
        )
        # 写回：先 ledger（事实源），再 draft.json（可由 ledger 重建）
        if queue_dir and queue_dir.exists():
            if res["published"]:
                gzh_state.mark_published(
                    root, queue_dir, _sniff_publish_id(res["evidence"]), res["evidence"]
                )
            else:
                gzh_state.mark_attempt_failed(
                    root, queue_dir, "scan_timeout", {"waited_s": args.wait_scan}
                )
        else:
            gzh_state.append_ledger_once(
                root,
                {
                    "date": gzh_state.today(),
                    "ts": gzh_state.now_iso(),
                    "state": "published" if res["published"] else "scan_timeout",
                    "event": "published" if res["published"] else "scan_timeout",
                    "appmsgid": str(args.appmsgid),
                    "publish_appmsgid": _sniff_publish_id(res["evidence"]),
                    "evidence": res["evidence"],
                    "waited_s": args.wait_scan,
                },
            )
        gzh_state.write_notice(
            root,
            {
                "date": gzh_state.today(),
                "publish": {
                    "attempted": True,
                    "state": "published" if res["published"] else "scan_timeout",
                    "title": report.get("content", {}).get("title") if report.get("content") else None,
                    "appmsgid": str(args.appmsgid),
                    "publish_appmsgid": _sniff_publish_id(res["evidence"]),
                    "screenshot": report["screenshot"],
                    "waited_s": args.wait_scan,
                },
                "need_human": []
                if res["published"]
                else [
                    "二维码已过期（等了 %d 秒）" % args.wait_scan,
                    "13:00 兜底会自动重出一码；或手动：python scripts/gzh_publish.py --from-queue --mode qr-wait --wait-scan 240 --headed",
                ],
                "next_action": "无需操作" if res["published"] else "13:00 重试",
            },
        )
    else:  # qr
        shot = runs_dir / f"{stamp}-publish-{args.appmsgid}-qr.png"
        out = act_to_qr(ab, profile, editor, shot)
        report["popup_opened"] = "PUB" in out
        report["needs_scan"] = _grab(out, r"SCAN=(yes|no)") == "yes"
        report["stage_text"] = _grab(out, r"STAGE=([^\n\"]*)")
        report["screenshot"] = str(shot) if shot.exists() else None
        if report["needs_scan"]:
            report["observed"] = "已推进到微信验证扫码，等待人工扫码确认（这是平台硬限制，无法绕过）"
        else:
            report["observed"] = "未识别到扫码环节，请对照截图与日志人工确认状态"

    log_path = runs_dir / f"{stamp}-publish-{args.appmsgid}.json"
    log_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n现场日志：{log_path}", file=sys.stderr)
    if report.get("screenshot"):
        print(f"扫码截图：{report['screenshot']}", file=sys.stderr)
    # qr-wait：没发出去就是 8（可重跑），发出去了才是 0
    if args.mode == "qr-wait" and not report.get("published"):
        return 8
    return 0


def append_ledger(root: Path, row: dict) -> None:
    d = root / "运行台账"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _sniff_publish_id(evidence: str) -> str | None:
    """从 qr-wait 的 evidence（形如 LIST=100000089:0:1）里取发表后的新 appmsgid。"""
    m = re.search(r"LIST=(\d+):", evidence or "")
    return m.group(1) if m else None


def _grab(out: str, pattern: str, default: str = "") -> str:
    m = re.search(pattern, out)
    return m.group(1).strip() if m else default


if __name__ == "__main__":
    raise SystemExit(main())
