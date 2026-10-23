#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令跑完「成稿 → 草稿箱 → 封面 →（可选）二维码」。

它**不写稿**。输入是一份已经写好的 manifest，它只负责把这份稿子可靠地送进后台。
写稿那一步需要判断力和文风，放在对话里由人+模型完成，不塞进脚本。

用法：
    # 只存草稿（含自动封面）
    python scripts/gzh_auto.py --manifest article.json
    # 续编辑已有草稿
    python scripts/gzh_auto.py --manifest article.json --appmsgid 100000042
    # 存完草稿后继续点到发表二维码（最后一步仍需人扫）
    python scripts/gzh_auto.py --manifest article.json --appmsgid 100000042 --publish qr

闸门（任何一条不过就停，不写入后台）：
    1. 急停文件 .cache/STOP_AUTO 不存在
    2. 当日草稿数未超 --daily-cap
    3. 登录态有效（失效直接报错，不重试）

产物：
    .cache/auto-runs/<时间戳>-auto.json     结构化日志（含各步结果）
    .cache/auto-runs/<时间戳>-auto.md       给人看的一页摘要
    发布到二维码时另有 -qr.png 截图

退出码：0 = 正常（含「闸门没过」「停在二维码」这类预期结果）；1 = 环境/登录异常。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gzh_lock  # noqa: E402
import gzh_state  # noqa: E402

TOKEN_RE = re.compile(r"[?&]token=(\d+)")
AB_ARGS = "--no-sandbox,--do-not-de-elevate"
HOME_URL = "https://mp.weixin.qq.com/"


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


def run_script(script: Path, args: list[str], timeout: int = 420) -> tuple[int, str, str]:
    """跑子脚本，返回 (退出码, stdout, stderr)。

    子脚本自身已经把 agent-browser 的输出重定向到临时文件了，所以这里可以安全地捕获管道。
    """
    cmd = [sys.executable, str(script)] + args
    # 本进程已持 gzh 锁，子脚本不能再抢同一把，用环境变量透传所有权
    env = dict(os.environ, GZH_LOCK_HELD="1")
    try:
        proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", f"子脚本超时（{timeout}s）：{script.name}"


def parse_json(text: str) -> dict | None:
    """从脚本输出里抠出最后一个 JSON 对象（前面可能混着进度日志）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def check_login(ab: str | None = None) -> tuple[bool, str, str]:
    """开一次后台首页，从跳转 URL 里取 token。失效就返回 False + 原因。"""
    from shutil import which  # 局部导入，避免顶层开销

    exe = ab or which("agent-browser") or which("agent-browser.cmd") or which("agent-browser.exe")
    if not exe:
        return False, "", "找不到 agent-browser"
    root = workroot()
    profile = root / ".cache" / "browser-profile"
    payload = json.dumps([["open", HOME_URL], ["wait", "4000"], ["eval", "location.href"]])

    fd, logname = tempfile.mkstemp(prefix="gzh-auto-login-", suffix=".log")
    try:
        with os.fdopen(fd, "wb") as logf:
            try:
                subprocess.run(
                    [exe, "--args", AB_ARGS, "--profile", str(profile), "--max-output", "2000", "batch"],
                    input=payload.encode("utf-8"),
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                )
            except subprocess.TimeoutExpired:
                return False, "", "登录检查超时"
        out = Path(logname).read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            os.unlink(logname)
        except OSError:
            pass

    m = TOKEN_RE.search(out)
    if m:
        return True, m.group(1), "OK"
    return (
        False,
        "",
        "登录态已失效（后台停在登录页）。"
        f'重扫：agent-browser --args "{AB_ARGS}" --profile "{profile}" --headed open {HOME_URL}',
    )


def count_today_drafts(root: Path) -> int:
    """今天真正存进草稿箱的篇数。

    旧版数 `.cache/auto-runs/{today}*-auto.json` 的文件个数——可是失败、急停、掉登录态
    也会写这个文件，等于「失败一次就消耗一次配额」，一天掉三次线就再也不能跑了。
    现在只认台账里 state ∈ {drafted, published} 的记录。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    ledger = root / "运行台账" / "ledger.jsonl"
    if not ledger.exists():
        return 0
    n = 0
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("date") == today and row.get("state") in ("drafted", "published"):
            n += 1
    return n


def append_ledger(root: Path, row: dict) -> None:
    """台账：一行一次状态变更，是最可靠的「今天到底发没发」的依据。"""
    d = root / "运行台账"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_summary(path: Path, report: dict) -> None:
    lines = [
        f"# 本次自动运行 · {report['started_at']}",
        "",
        f"- 稿件：{report.get('title') or '（未读到）'}",
        f"- 草稿 appmsgid：{report.get('appmsgid') or '—'}",
        f"- 封面：{'已设置' if report.get('cover_applied') else '未确认'}",
        f"- 发表：{report.get('publish_state') or '未执行'}",
    ]
    if report.get("editor_url"):
        lines.append(f"- 编辑链接：{report['editor_url']}")
    if report.get("qr_screenshot"):
        lines.append(f"- 二维码截图：`{report['qr_screenshot']}`")
    if report.get("halted_reason"):
        lines += ["", f"**停在这里**：{report['halted_reason']}"]
    if report.get("steps"):
        lines += ["", "## 步骤"]
        for s in report["steps"]:
            mark = "✅" if s.get("ok") else "❌"
            lines.append(f"- {mark} {s['name']}：{s.get('detail', '')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(description="成稿 → 草稿箱 → 封面 →（可选）二维码")
    ap.add_argument("--manifest", required=True, help="文章 manifest（含标题/摘要/正文/封面）")
    ap.add_argument("--appmsgid", default=None, help="续编辑已有草稿")
    ap.add_argument("--plan-only", action="store_true",
                    help="只判断要不要续编辑/能不能存，不开浏览器")
    ap.add_argument("--publish", choices=["none", "qr", "qr-wait"], default="none",
                    help="none 只存草稿 / qr 推到二维码就停 / qr-wait 推到码并等扫码（推荐）")
    ap.add_argument("--wait-scan", type=int, default=120, help="qr-wait 等扫码的秒数，默认 120")
    ap.add_argument("--daily-cap", type=int, default=1, help="每日最多存几篇草稿（读台账计数），默认 1")
    ap.add_argument("--ab", default=None)
    args = ap.parse_args(argv)

    root = workroot()
    scripts_dir = root / "scripts"
    runs_dir = root / ".cache" / "auto-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")

    report: dict = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "manifest": str(Path(args.manifest).resolve()),
        "steps": [],
        "appmsgid": None,
        "title": None,
        "cover_applied": None,
        "publish_state": None,
        "editor_url": None,
        "qr_screenshot": None,
        "halted_reason": None,
    }

    HALT_CODES = {
        "急停文件存在": 3,
        "当日已达上限": 4,
        "登录态无效": 5,
        "存草稿失败": 6,
        "已发表": 7,
        "写稿自检不通过": 9,
        "锁被占用": 10,
    }
    lock_holder: dict = {"path": None}

    def halt(reason: str, detail: str) -> int:
        """停止并落盘。**失败不再占用当日配额**，且返回语义化退出码。

        旧版无论成败都写 `-auto.json` 且返回 0：既让 count_today_drafts 把失败算成
        「已用一篇」，外部调度也看不出到底成没成。
        """
        code = HALT_CODES.get(reason, 1)
        report["halted_reason"] = reason
        report["exit_code"] = code
        report["steps"].append({"name": reason, "ok": False, "detail": detail})
        # 只有「配额已满」属于预期状态，算 auto；其它都是失败，写 failed 不参与计数
        suffix = "auto" if code == 4 else "failed"
        (runs_dir / f"{stamp}-{suffix}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_summary(runs_dir / f"{stamp}-{suffix}.md", report)
        gzh_lock.release(lock_holder["path"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code

    # ---------- 闸门 0：进程锁 ----------
    ok, lock = gzh_lock.acquire(root)
    if not ok:
        return halt("锁被占用", lock)
    lock_holder["path"] = lock
    report["steps"].append({"name": "进程锁", "ok": True, "detail": lock})

    # ---------- 闸门 1：急停 ----------
    stop = root / ".cache" / "STOP_AUTO"
    if stop.exists():
        return halt("急停文件存在", str(stop))
    report["steps"].append({"name": "急停检查", "ok": True, "detail": "无 STOP_AUTO"})

    # ---------- 闸门 2：当日上限 ----------
    today_count = count_today_drafts(root)
    if today_count >= args.daily_cap and not args.plan_only:
        return halt("当日已达上限", f"今天已存 {today_count} 篇，上限 {args.daily_cap}")
    report["steps"].append({"name": "当日上限", "ok": True, "detail": f"{today_count}/{args.daily_cap}"})

    # ---------- 闸门 3：登录态 ----------
    ok, token, why = check_login(args.ab)
    if not ok:
        return halt("登录态无效", why)
    report["steps"].append({"name": "登录态", "ok": True, "detail": f"token={token}"})

    # ---------- 第 1 步：续编辑判定 + 存草稿（含封面） ----------
    # 幂等：同一份 manifest 重跑**不能新建第二篇草稿**。稿件目录里的 draft.json 记着上次的 appmsgid。
    man_dir = Path(args.manifest).resolve().parent
    prev = gzh_state.read_draft(man_dir) if man_dir.exists() else None
    resume_id = args.appmsgid or (prev or {}).get("appmsgid")
    if not args.appmsgid and prev and prev.get("state") == "published":
        print(json.dumps({"ok": False, "skipped": "已发表", "appmsgid": prev.get("appmsgid")},
                         ensure_ascii=False, indent=2))
        gzh_lock.release(lock_holder["path"])
        return 7
    if args.plan_only:
        print(json.dumps({
            "ok": True, "plan_only": True,
            "resume_appmsgid": resume_id,
            "reason": "draft.json state=" + str((prev or {}).get("state")) if prev else "首次存稿",
            "would_run": "gzh_draft.py --manifest ..." + (f" --appmsgid {resume_id}" if resume_id else ""),
        }, ensure_ascii=False, indent=2))
        gzh_lock.release(lock_holder["path"])
        return 0

    draft_args = ["--manifest", args.manifest]
    if resume_id:
        draft_args += ["--appmsgid", str(resume_id)]
        report["steps"].append({"name": "续编辑", "ok": True, "detail": f"复用 appmsgid={resume_id}"})
    code, out, err = run_script(scripts_dir / "gzh_draft.py", draft_args, 420)
    draft = parse_json(out)
    if code != 0 or not draft or not draft.get("appmsgid"):
        print(err[-1200:], file=sys.stderr)
        return halt("存草稿失败", (err[-300:] or out[-300:] or "无输出"))
    report["appmsgid"] = draft["appmsgid"]
    report["title"] = draft.get("title")
    report["cover_applied"] = draft.get("cover_applied")
    report["editor_url"] = draft.get("editor_url")

    # 状态落地：拿到 appmsgid 才算 drafted（队列靠这个判断能不能发）
    if man_dir.exists() and (man_dir / "manifest.json").exists():
        gzh_state.write_draft(
            man_dir,
            {
                "state": "drafted",
                "appmsgid": str(draft["appmsgid"]),
                "title": draft.get("title"),
                "stored_at": gzh_state.now_iso(),
            },
        )
    report["steps"].append(
        {
            "name": "存草稿",
            "ok": True,
            "detail": f"appmsgid={draft['appmsgid']}，正文 {draft.get('body_chars')} 字",
        }
    )
    report["steps"].append(
        {
            "name": "设封面",
            "ok": bool(draft.get("cover_applied")),
            "detail": "已设置" if draft.get("cover_applied") else "未确认（见草稿）",
        }
    )

    # ---------- 第 2 步（可选）：推到二维码并等扫码 ----------
    if args.publish in ("qr", "qr-wait"):
        mode = "qr-wait" if args.publish == "qr-wait" else "qr"
        extra = ["--wait-scan", str(args.wait_scan)] if mode == "qr-wait" else []
        code2, out2, err2 = run_script(
            scripts_dir / "gzh_publish.py",
            ["--appmsgid", draft["appmsgid"], "--mode", mode, *extra],
            args.wait_scan + 300,
        )
        pub = parse_json(out2)
        if code2 == 8:
            # qr-wait 等超时：不是错误，人没扫，草稿还在
            report["publish_state"] = "等扫码超时（草稿仍在，可重跑）"
            report["halted_reason"] = "扫码超时"
            report["steps"].append({"name": "等扫码", "ok": False, "detail": f"等了 {args.wait_scan} 秒"})
        elif code2 != 0 or not pub:
            print(err2[-1200:], file=sys.stderr)
            report["publish_state"] = "执行失败"
            report["steps"].append({"name": "推到二维码", "ok": False, "detail": (err2[-300:] or "无输出")})
        else:
            report["qr_screenshot"] = pub.get("screenshot")
            if pub.get("published"):
                report["publish_state"] = "已发表"
                report["steps"].append(
                    {"name": "扫码并收尾", "ok": True, "detail": pub.get("evidence", "")[:120]}
                )
            elif pub.get("needs_scan"):
                report["publish_state"] = "已到二维码，待人扫码"
                report["halted_reason"] = "微信验证扫码（平台硬限制，只能人工完成）"
                report["steps"].append({"name": "点到二维码", "ok": True, "detail": "已弹出微信验证"})
            else:
                report["publish_state"] = "未识别到二维码，需人工确认"
                report["steps"].append({"name": "点到二维码", "ok": False, "detail": pub.get("stage_text", "")[:120]})
    else:
        report["publish_state"] = "未执行（--publish none）"

    # 台账：只有真的拿到 appmsgid 才记 drafted，避免失败也占配额
    if report.get("appmsgid"):
        state = "published" if report.get("publish_state") == "已发表" else "drafted"
        append_ledger(
            root,
            {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "ts": datetime.now().isoformat(timespec="seconds"),
                "state": state,
                "appmsgid": report["appmsgid"],
                "title": report.get("title"),
                "manifest": report["manifest"],
                "publish_state": report.get("publish_state"),
            },
        )
        report["ledger_state"] = state

    (runs_dir / f"{stamp}-auto.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(runs_dir / f"{stamp}-auto.md", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("qr_screenshot"):
        print(f"\n二维码截图：{report['qr_screenshot']}", file=sys.stderr)
    gzh_lock.release(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
