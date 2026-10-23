#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""雷达页的「一键发送」后端。

为什么需要它：雷达是静态 HTML，浏览器点按钮没法直接叫醒 agent。
这个小服务负责接住点击，把「哪一期、哪几条、走什么轨道」写成一条队列记录，
agent 下次开工时读 `.cache/send-queue.jsonl` 就知道该写哪几篇。

用法：
    python scripts/radar_server.py                 # 默认 8899 端口，服务 雷达/ 目录
    python scripts/radar_server.py --port 8899

打开：http://127.0.0.1:8899/            （看板）
      http://127.0.0.1:8899/2026-10-19-2.html

接口：
    POST /send   {"issue":"4","ids":["01"],"track":"compile","title":"..."}
                 → 追加到 .cache/send-queue.jsonl
    GET  /queue  → 当前队列（JSON 数组）

注意：这是本机自用的小工具，只绑 127.0.0.1，不做鉴权。不要暴露到公网。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RADAR_DIR), **kwargs)

    def _cors(self) -> None:
        # 允许从 file:// 或别的端口打开页面时也能提交
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] == "/queue":
            rows = []
            if QUEUE.exists():
                for line in QUEUE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            body = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/send":
            self.send_error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self.send_error(400, f"bad json: {exc}")
            return

        ids = payload.get("ids") or []
        if isinstance(ids, str):
            ids = [ids]
        ids = [str(i).strip() for i in ids if str(i).strip()]
        if not ids:
            self.send_error(400, "ids 不能为空")
            return

        rec = {
            "queued_at": datetime.now().isoformat(timespec="seconds"),
            "issue": str(payload.get("issue") or ""),
            "ids": ids,
            "track": str(payload.get("track") or "compile"),
            "title": str(payload.get("title") or "")[:120],
        }
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        with QUEUE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        body = json.dumps({"ok": True, "queued": rec}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # 只记 POST，静态资源请求太吵
        if "/send" in (args[0] if args else ""):
            sys.stderr.write(f"[send] {fmt % args}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="雷达页一键发送的本地后端")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()

    if not RADAR_DIR.exists():
        raise SystemExit(f"找不到雷达目录：{RADAR_DIR}")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"雷达服务已启动：http://127.0.0.1:{args.port}/", file=sys.stderr)
    print(f"  静态目录：{RADAR_DIR}", file=sys.stderr)
    print(f"  队列文件：{QUEUE}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
