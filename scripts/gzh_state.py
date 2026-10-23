#!/usr/bin/env python3
"""稿件状态与台账：整个队列机制的唯一写入层。

两种载体，职责严格分离：
    稿件/<dir>/draft.json   物化的当前状态（原子写，供队列扫描）
    运行台账/ledger.jsonl   只追加的事件流（唯一事实源，崩了能重建 draft.json）

为什么不建一个 queue.json 汇总快照：两份可写状态必然漂移。汇总用 gzh_queue.py 现算。

写入顺序铁律：**先 ledger 后 draft.json**。ledger 是事实源，draft 可以由 sync 从 ledger 重建；
反过来一旦崩在中间，就会留下「说发过但没记录」的孤儿状态。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pool_store as ps

DRAFT_NAME = "draft.json"
LEDGER_DIR = "运行台账"
LEDGER_NAME = "ledger.jsonl"
NOTICE_DIR = "notice"

# 允许的状态迁移（其余一律拒绝，需 --force）
ALLOWED = {
    "written": {"drafted", "dropped"},
    "drafted": {"published", "stale", "dropped"},
    "published": set(),
    "stale": {"dropped", "written"},
    "dropped": {"written"},
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _ledger_path(root: Path) -> Path:
    return root / LEDGER_DIR / LEDGER_NAME


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def read_ledger(root: Path) -> list[dict]:
    p = _ledger_path(root)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_ledger_once(root: Path, row: dict) -> bool:
    """幂等追加：同样的 (date, state, appmsgid) 已经存在就跳过。返回是否真的写入。

    重跑同一篇（扫码超时后再扫）会走到这里，不能每次重试都往台账里刷一行，
    否则「今天发了几篇」会被自己污染。
    """
    rows = read_ledger(root)
    key = (row.get("date"), row.get("state"), str(row.get("appmsgid") or ""))
    for r in rows:
        if (r.get("date"), r.get("state"), str(r.get("appmsgid") or "")) == key:
            return False
        if row.get("state") == "published" and row.get("publish_appmsgid"):
            if r.get("publish_appmsgid") == row["publish_appmsgid"]:
                return False
    d = root / LEDGER_DIR
    d.mkdir(parents=True, exist_ok=True)
    with (d / LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def published_today(root: Path) -> dict:
    """今天发过没有。查 ledger（事实源）+ draft.json（人工发表后补记的两头都算）。"""
    t = today()
    hits: list[str] = []
    for r in read_ledger(root):
        if r.get("state") == "published" and r.get("date") == t:
            hits.append(f"ledger:{r.get('publish_appmsgid') or r.get('appmsgid')}")
    for d, rec in iter_drafts(root):
        if rec.get("state") == "published" and str(rec.get("published_at") or "")[:10] == t:
            hits.append(f"draft:{rec.get('appmsgid')}")
    hits = sorted(set(hits))
    return {
        "hit": bool(hits),
        "detail": ("今日已发表：" + "，".join(hits)) if hits else f"ledger/draft 均无 {t} 的 published 记录",
        "hits": hits,
    }


def published_row_for(root: Path, appmsgid: str) -> dict | None:
    """这一篇（按草稿 id 或发表后 id）是否已经发过。"""
    for r in read_ledger(root):
        if r.get("state") != "published":
            continue
        if str(r.get("appmsgid")) == str(appmsgid) or str(r.get("publish_appmsgid")) == str(appmsgid):
            return r
    for _, rec in iter_drafts(root):
        if rec.get("state") == "published" and str(rec.get("appmsgid")) == str(appmsgid):
            return rec
    return None


# --------------------------------------------------------------------------- #
# draft.json
# --------------------------------------------------------------------------- #
def draft_path(d: Path) -> Path:
    return d / DRAFT_NAME


def read_draft(d: Path) -> dict | None:
    p = draft_path(d)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_draft(d: Path, data: dict, merge: bool = True) -> dict:
    """原子写。merge=True 时与现有内容合并（只改传入字段）。"""
    d.mkdir(parents=True, exist_ok=True)
    cur = read_draft(d) or {}
    rec = {**cur, **data} if merge else dict(data)
    rec.setdefault("version", 1)
    rec["dir"] = str(d.as_posix())
    rec["updated_at"] = now_iso()
    rec.setdefault("created_at", rec["updated_at"])
    tmp = d / (DRAFT_NAME + ".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, draft_path(d))
    return rec


def iter_drafts(root: Path) -> list[tuple[Path, dict]]:
    base = root / "稿件"
    out: list[tuple[Path, dict]] = []
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        rec = read_draft(d)
        if rec:
            out.append((d, rec))
    return out


def find_dir_by_appmsgid(root: Path, appmsgid: str) -> Path | None:
    for d, rec in iter_drafts(root):
        if str(rec.get("appmsgid")) == str(appmsgid):
            return d
    return None


def can_transition(src: str, dst: str) -> bool:
    return dst in ALLOWED.get(src, set())


# --------------------------------------------------------------------------- #
# 状态变更
# --------------------------------------------------------------------------- #
def mark_published(
    root: Path, d: Path, publish_appmsgid: str | None, evidence: str = ""
) -> dict:
    """标为已发表。先 ledger 后 draft。幂等：已 published 时只补空的 publish_appmsgid。"""
    rec = read_draft(d) or {}
    if rec.get("state") == "published":
        if publish_appmsgid and not rec.get("publish_appmsgid"):
            rec = write_draft(d, {"publish_appmsgid": publish_appmsgid})
        return rec
    ts = now_iso()
    append_ledger_once(
        root,
        {
            "date": today(),
            "ts": ts,
            "state": "published",
            "event": "published",
            "appmsgid": rec.get("appmsgid"),
            "publish_appmsgid": publish_appmsgid,
            "dir": rec.get("dir"),
            "title": rec.get("title"),
            "evidence": evidence,
            "attempt": rec.get("attempts", 0),
        },
    )
    return write_draft(
        d,
        {
            "state": "published",
            "publish_appmsgid": publish_appmsgid,
            "published_at": ts,
            "last_error": None,
        },
    )


def mark_attempt_failed(root: Path, d: Path, error: str, extra: dict | None = None) -> dict:
    """失败但**不改状态**（草稿还在）。只累加 attempts，供队列做「重试优先」。"""
    rec = read_draft(d) or {}
    append_ledger_once(
        root,
        {
            "date": today(),
            "ts": now_iso(),
            "state": error,
            "event": error,
            "appmsgid": rec.get("appmsgid"),
            "dir": rec.get("dir"),
            "title": rec.get("title"),
        },
    )
    return write_draft(
        d,
        {
            "attempts": int(rec.get("attempts") or 0) + 1,
            "last_attempt_at": now_iso(),
            "last_error": error,
            **(extra or {}),
        },
    )


def mark_state(
    root: Path, d: Path, state: str, note: str | None = None, force: bool = False
) -> tuple[bool, str]:
    """通用状态迁移。返回 (成功, 说明)。迁移非法时拒绝，除非 force。"""
    rec = read_draft(d)
    if rec is None:
        return False, f"没有 draft.json：{d}"
    cur = rec.get("state", "written")
    if cur != state and not can_transition(cur, state) and not force:
        return False, f"非法迁移 {cur}→{state}（需 --force）"
    append_ledger_once(
        root,
        {
            "date": today(),
            "ts": now_iso(),
            "state": state,
            "event": f"{cur}->{state}",
            "appmsgid": rec.get("appmsgid"),
            "dir": rec.get("dir"),
            "title": rec.get("title"),
            "note": note,
        },
    )
    write_draft(d, {"state": state, "note": note} if note else {"state": state})
    # 作废/过期 → 把选题池的条目放回去，否则池子会被慢慢掏空
    if state in ("dropped", "stale") and cur in ("written", "drafted"):
        key = rec.get("pool_key")
        if key and release_pool_item(root, key):
            append_ledger_once(
                root,
                {
                    "date": today(),
                    "ts": now_iso(),
                    "state": "pool_released",
                    "event": "pool_released",
                    "appmsgid": rec.get("appmsgid"),
                    "dir": rec.get("dir"),
                    "title": rec.get("title"),
                    "note": f"pool_key={key}",
                },
            )
    return True, f"{cur}→{state}"


def release_pool_item(root: Path, key: str) -> bool:
    pool = root / "雷达" / "pool.jsonl"
    if not pool.exists() or not key:
        return False
    return ps.release_item(pool, key)


# --------------------------------------------------------------------------- #
# notice（给人看的一页纸）
# --------------------------------------------------------------------------- #
def write_notice(root: Path, payload: dict) -> Path:
    d = root / LEDGER_DIR / NOTICE_DIR
    d.mkdir(parents=True, exist_ok=True)
    date = payload.get("date") or today()
    (d / f"{date}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [f"# {date} 公众号运行结果", ""]
    for k, label in (
        ("stock_before", "运行前库存"),
        ("stock_after", "运行后库存"),
        ("written", "本次写稿"),
        ("stop_reason", "停止原因"),
    ):
        if payload.get(k) is not None:
            lines.append(f"- {label}：{payload[k]}")
    pub = payload.get("publish") or {}
    if pub.get("attempted"):
        lines += [
            "",
            f"## 发表：{pub.get('state')}",
            f"- 标题：{pub.get('title')}",
            f"- 草稿 id：{pub.get('appmsgid')}",
        ]
        if pub.get("publish_appmsgid"):
            lines.append(f"- 发表后 id：{pub['publish_appmsgid']}")
        if pub.get("screenshot"):
            lines.append(f"- 二维码截图：`{pub['screenshot']}`")
    need = payload.get("need_human") or []
    if need:
        lines += ["", "## 需要人做的事"]
        lines += [f"{i+1}. {x}" for i, x in enumerate(need)]
    if payload.get("next_action"):
        lines += ["", f"下一步：{payload['next_action']}"]
    md = d / f"{date}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md
