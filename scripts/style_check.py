#!/usr/bin/env python3
"""文风自检：这篇读起来像人写的吗？

    python scripts/style_check.py --manifest 稿件/x/manifest.json
    python scripts/style_check.py --manifest 稿件/x/manifest.json --json

为什么要有这个脚本：lint 查的是「结构对不对」（字数、加粗、卡片），
这份查的是「像不像人话」。两件事都会让文章废掉，但后者只有人读得出来——
而且往往发出去了才发现。

检查维度（对应 写作规则.md 第八、九节）：
    1 禁用套话 / AI 味词
    2 句长节奏（长短交替，单句不过 45 字）
    3 共鸣钩子（开头 100 字内有没有「你」或具体场景）
    4 四类角度（反差 / 现象总结 / 忠告 / 反转提问）至少各有一个信号
    5 结尾行动（最后三段有没有「今天就试」这类可执行动作）
    6 具体性（品牌名、数字、时间——都是细节信号）

退出码：0 通过 / 2 有待改项 / 1 参数错误
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TAG = re.compile(r"<[^>]+>")
SPLIT = re.compile(r"[。！？!?…]+")

# 出现即扣分：一看就像机器写的，或者像在念 PPT
BANNED = [
    "赋能", "闭环", "抓手", "生态位", "颠覆", "革命性", "助力", "打造", "深度融合",
    "维度", "层面", "链路", "打法", "组合拳", "全链路", "值得注意", "综上所述",
    "总而言之", "随着", "不难发现", "首先", "其次", "最后一点", "在这个",
    "全面提升", "有效推进", "持续优化", "深度赋能",
    # 以下是 humanize-writing-zh 的 filler 词库（2026-11-04 吸收）
    "在某种程度上", "在某种意义上", "在一定程度上", "某种程度上", "某种意义上",
    "可谓是", "不得不承认", "毋庸置疑", "不可否认", "众所周知", "一言以蔽之",
    "一般来说", "通常情况下", "换句话说", "无论如何",
]

# 万能副词：不是绝对禁止，但密集出现就是机器腔
FILLER_ADV = ["非常", "十分", "极其", "格外", "颇为", "相当地", "极为", "分外", "甚是"]

# 这些英文是中文写作里的通用词，不算「中英夹用」（做PPT、用Excel 都是正常说法）
EN_OK = {"PPT", "EXCEL", "WORD", "PDF", "AI", "APP", "OK", "WIFI", "USB", "URL",
         "ID", "HR", "KPI", "OKR", "SOP", "CEO", "CTO", "PR", "UI", "UX", "API",
         # AI 产品/项目名：写这个领域躲不开，不算「中英夹用」
         "CLAUDE", "CHATGPT", "GPT", "GEMINI", "DEEPSEEK", "KIMI", "DOUBAO",
         "SORA", "CURSOR", "COPILOT", "NOTION", "FIGMA", "MIDJOURNEY", "LLAMA",
         "COWORK", "SKILL", "SKILLS", "AGENT", "PROMPT"}

# 四类角度的信号词（命中任一即算用过该角度）
ANGLES = {
    "反差": ["其实", "反而", "恰恰", "以为", "结果", "不是……而是", "原来"],
    "现象总结": ["三种", "两类", "身边", "我发现", "大部分人", "同一件事", "共同点"],
    "忠告": ["别", "不要", "千万别", "避免", "踩过", "吃过亏", "记得先"],
    "反转提问": ["？", "该不该", "要不要", "纠结", "问错了"],
}

CTA = ["今天就试", "今天就可以", "你现在", "试试", "去试", "打开你", "翻出你"]


def text_of(html: str) -> str:
    t = TAG.sub("", html)
    return re.sub(r"[ \t\r]+", "", t)


def sentences(t: str) -> list[str]:
    return [s.strip() for s in SPLIT.split(t) if s.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="文风自检")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    man = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    body = (Path(args.manifest).parent / man.get("body_html_path", "body.html")).read_text(encoding="utf-8")
    t = text_of(body)
    t_spaced = TAG.sub("", body)  # 保留空格，只给中英混杂检查用

    issues: list[str] = []
    warns: list[str] = []
    score = 100

    # 1 禁用词（先剔掉引号内的内容：那些是「反面示范」，不是正文在说套话）
    quoted = re.findall(r"「[^」]*」|“[^”]*”", t)
    t_nq = t
    for q in quoted:
        t_nq = t_nq.replace(q, "")
    hit = [w for w in BANNED if w in t_nq]
    if hit:
        issues.append(f"[套话] 命中 {len(hit)} 个：{'、'.join(hit[:6])}")
        score -= min(24, 5 * len(hit))

    # 2 句长（按段落/块切：跨块拼句会把「小标题+正文」算成一句，误报超长）
    from gzh_draft import scan_body as _scan

    # 也不能只按块切：卡片里「标题 span + 正文 span」在同一个 section 里，
    # 拼起来同样会误报。按 span 切行，一个 span 就是一行。
    blocks = _scan(body)["blocks"]
    ss = []
    for b in blocks:
        spans = re.findall(r"<span[^>]*>(.*?)</span>", b, re.S)
        for piece in (spans or [b]):
            ss += sentences(TAG.sub("", piece))
    lens = [len(s) for s in ss] or [0]
    avg = sum(lens) / len(lens)
    long_s = [s for s in ss if len(s) > 45]
    if long_s:
        issues.append(f"[句长] {len(long_s)} 句超过 45 字，最长 {max(lens)} 字")
        score -= min(15, 3 * len(long_s))
    short_ratio = sum(1 for x in lens if x <= 8) / len(lens)
    if short_ratio < 0.12:
        warns.append(f"[节奏] 短句（≤8 字）只占 {short_ratio:.0%}，读起来会像念稿，建议插几句短的")
        score -= 6
    if avg > 30:
        warns.append(f"[节奏] 平均句长 {avg:.1f} 字，偏长")
        score -= 5

    # 3 共鸣钩子（开头 120 字）
    head = t[:120]
    if "你" not in head and not re.search(r"我|我们", head):
        issues.append("[钩子] 开头 120 字里没有人称（你 / 我），读者不知道这跟自己有什么关系")
        score -= 12

    # 4 四类角度
    used = [k for k, words in ANGLES.items() if any(w in t for w in words)]
    if len(used) < 2:
        issues.append(f"[角度] 只用了 {len(used)} 类（{'、'.join(used) or '无'}），建议至少两类：反差 / 现象总结 / 忠告 / 反转提问")
        score -= 10

    # 5 结尾行动
    if not any(c in t[-260:] for c in CTA):
        issues.append("[结尾] 最后一段没有可执行动作（「今天就试…」）")
        score -= 8

    # 6 中英混杂：中文句子里夹英文单词，是人手写不出来的东西
    # 文本里的空格已被去掉，所以「Claude Cowork 工程」会连成 ClaudeCowork工；
    # 判断时要拆成单词逐个看，不能整串比对。
    mixed = []
    for m in re.findall(r"[\u4e00-\u9fa5][A-Za-z]{3,}|[A-Za-z]{3,}[\u4e00-\u9fa5]", t_spaced):
        words = re.findall(r"[A-Za-z]+", m)
        if not all(w.upper() in EN_OK for w in words):
            mixed.append(m)
    if mixed:
        issues.append(f"[混杂] {len(mixed)} 处中英夹用（如 {mixed[0]}），像是漏改的")
        score -= 8

    # 7 万能副词密度
    adv = [w for w in FILLER_ADV if w in t_nq]
    if len(adv) >= 3:
        warns.append(f"[虚词] 万能副词 {len(adv)} 个（{'、'.join(adv)}），删掉一半不影响意思")
        score -= 4

    # 8 具体性
    nums = len(re.findall(r"\d", t)) + len(
        re.findall(r"[一二三四五六七八九十百千万]+\s*(?:分钟|小时|秒|天|周|个月|年|步|行|页|块|个|次|条|篇|件|人|家)", t)
    )
    if nums < 5:
        warns.append("[具体] 全文数字很少，读起来会空（时间、次数、金额、步骤编号都算）")
        score -= 5

    score = max(0, score)
    result = {
        "ok": not issues,
        "score": score,
        "chars": len(t),
        "sentences": len(ss),
        "avg_len": round(avg, 1),
        "short_ratio": round(short_ratio, 2),
        "angles": used,
        "banned": hit,
        "issues": issues,
        "warns": warns,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not issues else 2

    print(f"文风自检  字数={len(t)}  句数={len(ss)}  平均句长={avg:.1f}  短句占比={short_ratio:.0%}")
    print(f"用到的角度：{'、'.join(used) or '（无）'}")
    for x in issues:
        print(f"  ❌ {x}")
    for x in warns:
        print(f"  ⚠️  {x}")
    print(f"得分 {score}/100  " + ("结论：✅ 可以发" if not issues else "结论：❌ 先改上面几处"))
    return 0 if not issues else 2


if __name__ == "__main__":
    sys.exit(main())
