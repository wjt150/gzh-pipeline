#!/usr/bin/env python3
"""scan_body / split_blocks 的本地单测（不联网、不开浏览器）。

跑法：python -m unittest scripts.test_body_scan -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gzh_draft import scan_body, split_blocks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
V2 = ROOT / ".cache" / "sources" / "81e43f0350012225.body.v2.html"


def msgs(report: dict) -> str:
    return " | ".join(p["msg"] for p in report["errors"])


class TestSplitBlocks(unittest.TestCase):
    def test_flat_sections(self) -> None:
        html = '<section style="a">x</section><section style="b">y</section>'
        self.assertEqual(len(split_blocks(html)), 2)

    def test_nested_section_is_one_block(self) -> None:
        """时间线/参数卡内部有嵌套 section，必须算 1 块（旧实现会给 2）。"""
        html = (
            '<section style="a">'
            '<section style="b">x</section>'
            '<section style="c">y</section>'
            "</section>"
        )
        self.assertEqual(len(split_blocks(html)), 1)

    def test_v2_sample_blocks(self) -> None:
        if not V2.exists():
            self.skipTest("v2 样例不存在")
        html = V2.read_text(encoding="utf-8")
        self.assertEqual(len(split_blocks(html)), 19)


class TestScanBody(unittest.TestCase):
    def test_v2_sample_clean(self) -> None:
        if not V2.exists():
            self.skipTest("v2 样例不存在")
        r = scan_body(V2.read_text(encoding="utf-8"))
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["stats"]["section"], 19)
        self.assertEqual(r["stats"]["strong"], 10)
        self.assertGreaterEqual(r["stats"]["styled"], 50)

    def test_nested_timeline_ok(self) -> None:
        html = (
            '<section style="border-left:2px solid #e6e6e6;">'
            '<section style="margin:0 0 16px;"><span style="color:#333;">a</span></section>'
            '<section style="margin:0;"><span style="color:#333;">b</span></section>'
            "</section>"
        )
        r = scan_body(html)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["stats"]["blocks"], 1)

    def test_inline_block_counted(self) -> None:
        html = (
            '<section style="font-size:0;">'
            '<span style="display:inline-block;width:48%;">1</span>'
            '<span style="display:inline-block;width:48%;">2</span>'
            "</section>"
        )
        self.assertEqual(scan_body(html)["stats"]["iblock"], 2)

    def test_p_tag_rejected(self) -> None:
        self.assertIn("不许用", msgs(scan_body("<p>老写法</p>")))

    def test_table_rejected(self) -> None:
        self.assertIn("不许用", msgs(scan_body("<section><table><tr><td>x</td></tr></table></section>")))

    def test_strong_inside_span_rejected(self) -> None:
        errs = msgs(scan_body('<section><span style="color:#333;"><strong>x</strong></span></section>'))
        self.assertIn("嵌在 <span> 里", errs)

    def test_unclosed_section_rejected(self) -> None:
        self.assertIn("没有闭合", msgs(scan_body('<section style="a"><span>x</span>')))

    def test_bare_text_at_top_rejected(self) -> None:
        errs = msgs(scan_body('引言文字<section style="a">x</section>'))
        self.assertIn("裸露文本", errs)


if __name__ == "__main__":
    unittest.main()
