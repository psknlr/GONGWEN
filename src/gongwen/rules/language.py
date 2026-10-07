"""语言文字检查：标点（GB/T 15834）、数字（GB/T 15835）、层次序数（GB/T 9704 7.3.3）、
高频易混字词与相对时间。多数问题给出确定性修订提示，供定向修订直接使用。"""

from __future__ import annotations

import re

from ..knowledge import kb
from ..schemas.review import IssueType, ReviewIssue
from .base import CheckContext

CJK = r"一-鿿"
_HALF = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}
_HALF_RE = re.compile(rf"(?<=[{CJK}”）])([,;:?!])|([,;:?!])(?=[{CJK}“（])")
_HALF_PAREN_RE = re.compile(rf"\(([^()]*[{CJK}][^()]*)\)")
_HALF_DOT_RE = re.compile(rf"(?<=[{CJK}”）])\.(?!\d)")
_ELLIPSIS_RE = re.compile(r"\.{3,}|。{2,}|…(?!…)|(?<!…)…{3,}")
_DUP_RE = re.compile(r"([，。、；：])\1+")
_DENG_RE = re.compile(r"、(等|等等)")
_YEAR_RANGE_RE = re.compile(r"(\d{4})\s*[-－~～至到]\s*(\d{4})\s*年")
_NUM_RANGE_RE = re.compile(r"(?<![\d年月])(\d+(?:\.\d+)?)\s*[-－~]\s*(\d+(?:\.\d+)?)\s*(天|个|人|次|项|家|万元|元|小时|分钟|岁|米|公里|%|％)")
_PCT_RANGE_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[～~—\-至到]\s*(\d+(?:\.\d+)?)\s*[%％]")
_WAN_RANGE_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*[～~—\-至到]\s*(\d+(?:\.\d+)?)\s*(万|亿)(元)?")
# 简写年份：“26年3月”“25年底”“截至25年”；“工作12年”等时长不在此列
_YEAR2_RE = re.compile(r"(?<![\d〔第])(\d{2})年(?=\d{1,2}月|底|末|初|度|上半年|下半年)|(?<=截至)(\d{2})年|(?<=截止)(\d{2})年")
_LEAD_DOT_RE = re.compile(rf"(?<=[\s{CJK}，：])\.(\d+)")
_PAD_DATE_RE = re.compile(r"\d{4}年(0\d)月|月(0\d)日")
_MIXED_DATE_RE = re.compile(r"[〇零一二三四五六七八九]{4}年\d{1,2}月|\d{4}年[一二三四五六七八九十]{1,2}月")
_BAD_LABELS = [
    (re.compile(r"^（([一二三四五六七八九十]+)）[、，.．]"), "（{0}）", "“（一）”后不加任何点号"),
    (re.compile(r"^(\d+)[、，．]"), "{0}.", "阿拉伯数字序号后用下脚点“.”，不用顿号或全角点"),
    (re.compile(r"^([一二三四五六七八九十]+)[.．，]"), "{0}、", "汉字数字序号后用顿号"),
    (re.compile(r"^\(([一二三四五六七八九十]+)\)"), "（{0}）", "序号括号应为全角"),
    (re.compile(r"^\((\d+)\)"), "（{0}）", "序号括号应为全角"),
]
LEVEL_RE = {
    1: re.compile(r"^[一二三四五六七八九十]+、$"),
    2: re.compile(r"^（[一二三四五六七八九十]+）$"),
    3: re.compile(r"^\d+\.$"),
    4: re.compile(r"^（\d+）$"),
}
CN_SEQ = "一二三四五六七八九十"


def cn_ordinal(n: int) -> str:
    if n <= 10:
        return CN_SEQ[n - 1]
    if n < 20:
        return "十" + (CN_SEQ[n - 11] if n > 10 else "")
    tens, ones = divmod(n, 10)
    return CN_SEQ[tens - 1] + "十" + (CN_SEQ[ones - 1] if ones else "")


def expected_label(level: int, n: int) -> str:
    return {1: f"{cn_ordinal(n)}、", 2: f"（{cn_ordinal(n)}）", 3: f"{n}.", 4: f"（{n}）"}[level]


def _texts(ctx: CheckContext):
    for b, s in ctx.ir.iter_sentences(include_attachments=True):
        yield b, s, s.text


def check_punctuation(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    for b, s, t in _texts(ctx):
        if "http" in t or "@" in t:
            continue
        m = _HALF_RE.search(t)
        if m:
            ch = m.group(1) or m.group(2)
            out.append(ctx.issue("GW-PUNC-001", IssueType.PUNCTUATION, f"中文语境中的半角“{ch}”应改为全角“{_HALF[ch]}”", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "fullwidth_punct"}))
        if _HALF_PAREN_RE.search(t):
            out.append(ctx.issue("GW-PUNC-001", IssueType.PUNCTUATION, "中文语境中的半角括号应改为全角“（）”", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "fullwidth_paren"}))
        if _HALF_DOT_RE.search(t):
            out.append(ctx.issue("GW-PUNC-001", IssueType.PUNCTUATION, "句末应使用中文句号“。”", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "fullwidth_period"}))
        if _ELLIPSIS_RE.search(t):
            out.append(ctx.issue("GW-PUNC-005", IssueType.PUNCTUATION, "省略号应为六连点“……”", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "ellipsis"}))
        m = _DUP_RE.search(t)
        if m:
            out.append(ctx.issue("GW-PUNC-001", IssueType.PUNCTUATION, f"标点“{m.group(1)}”重复", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "dedupe_punct"}))
        if _DENG_RE.search(t):
            out.append(ctx.issue("GW-PUNC-006", IssueType.PUNCTUATION, "顿号连接的并列成分末尾用“等”时，“等”前不加点号", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "deng"}))
        for left, right in (("“", "”"), ("《", "》"), ("（", "）"), ("〔", "〕")):
            if t.count(left) != t.count(right):
                out.append(ctx.issue("GW-PUNC-003", IssueType.PUNCTUATION, f"标号“{left}{right}”未成对使用", block=b, sentence=s))
                break
        if _YEAR_RANGE_RE.search(t):
            out.append(ctx.issue("GW-PUNC-004", IssueType.PUNCTUATION, "起止年份之间用一字线“—”（如“2026—2030年”）", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "year_range"}))
        elif _NUM_RANGE_RE.search(t):
            out.append(ctx.issue("GW-PUNC-004", IssueType.PUNCTUATION, "数值范围用浪纹线“～”（如“15～30天”）", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "num_range"}))
    return out


_FULLWIDTH_DIGIT = re.compile(r"[０-９]+")
_DATE_HAO = re.compile(r"\d{1,2}月\d{1,2}号")


def check_numbers(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    for b, s, t in _texts(ctx):
        m = _FULLWIDTH_DIGIT.search(t)
        if m:
            half = m.group(0).translate({ord("０") + i: ord("0") + i for i in range(10)})
            out.append(ctx.issue("GW-NUM-006", IssueType.NUMBER_USAGE, f"全角数字“{m.group(0)}”应改为半角“{half}”", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "halfwidth_digits"}))
        m = _DATE_HAO.search(t)
        if m:
            out.append(ctx.issue("GW-NUM-007", IssueType.NUMBER_USAGE, f"“{m.group(0)}”应写作“{m.group(0)[:-1]}日”", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "date_ri"}))
        m = _YEAR2_RE.search(t)
        if m:
            yy = next(g for g in m.groups() if g)
            out.append(ctx.issue("GW-NUM-001", IssueType.NUMBER_USAGE, f"年份“{yy}年”不应简写，应写全称（如“20{yy}年”）", block=b, sentence=s, needs_human=True))
        m = _PCT_RANGE_RE.search(t)
        if m and not re.search(r"\d\s*[%％]\s*[～~—\-至到]", m.group(0)):
            out.append(ctx.issue("GW-NUM-002", IssueType.NUMBER_USAGE, f"百分数范围的百分号不能省略（应为“{m.group(1)}%～{m.group(2)}%”）", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "pct_range"}))
        m = _WAN_RANGE_RE.search(t)
        if m and not re.search(r"\d\s*(万|亿)\s*元?\s*[～~—\-至到]", m.group(0)):
            u = m.group(3) + (m.group(4) or "")
            out.append(ctx.issue("GW-NUM-003", IssueType.NUMBER_USAGE, f"“万”“亿”不能跨数省略（应为“{m.group(1)}{u}～{m.group(2)}{u}”）", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "wan_range"}))
        if _LEAD_DOT_RE.search(t):
            out.append(ctx.issue("GW-NUM-004", IssueType.NUMBER_USAGE, "纯小数应写出定位“0”", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "lead_zero"}))
        if _PAD_DATE_RE.search(t):
            out.append(ctx.issue("GW-NUM-005", IssueType.NUMBER_USAGE, "日期中的月、日不编虚位（如“8月1日”，不写“08月01日”）", block=b, sentence=s, auto_fixable=True, fix_hint={"op": "unpad_date"}))
        if _MIXED_DATE_RE.search(t):
            out.append(ctx.issue("GW-NUM-005", IssueType.NUMBER_USAGE, "同一日期中汉字数字与阿拉伯数字混用", block=b, sentence=s))
    return out


def check_relative_time(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    words = kb.lexicon()["relative_time"]
    for b, s, t in _texts(ctx):
        t = re.sub(r"[\d一二三四五六七八九十]日前", "", t)  # “6月30日前”是期限，不是相对时间“日前”
        hit = next((w for w in words if w in t), None)
        if hit:
            out.append(ctx.issue("GW-STYLE-003", IssueType.RELATIVE_TIME, f"“{hit}”为相对时间，公文中应写明具体的年、月、日", block=b, sentence=s, needs_human=True))
    return out


def check_wording(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    rules = kb.lexicon()["wording"]
    for b, s, t in _texts(ctx):
        for w in rules:
            if "wrong" in w and w["wrong"] in t:
                out.append(ctx.issue("GW-STYLE-004", IssueType.WORDING, f"“{w['wrong']}”宜改为“{w['right']}”：{w['note']}", block=b, sentence=s, auto_fixable=True, fix_hint={"replace": w["wrong"], "with": w["right"]}))
            elif "pattern" in w and re.search(w["pattern"], t):
                out.append(ctx.issue("GW-STYLE-004", IssueType.WORDING, f"宜改为“{w['right']}”：{w['note']}", block=b, sentence=s, auto_fixable=True, fix_hint={"regex": w["pattern"], "with": w["right"]}))
    return out


def check_structure(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    counters = {1: 0, 2: 0, 3: 0, 4: 0}
    scopes = [ctx.ir.blocks] + [a.blocks for a in ctx.ir.attachments]
    for blocks in scopes:
        counters = {1: 0, 2: 0, 3: 0, 4: 0}
        last_level = 0
        for b in blocks:
            if b.kind != "heading" or b.level not in LEVEL_RE:
                # 段首序号标点
                text = b.text()
                for rx, fmt, why in _BAD_LABELS:
                    m = rx.match(text)
                    if m:
                        out.append(ctx.issue("GW-PUNC-002", IssueType.NUMBERING, f"{why}（应为“{fmt.format(m.group(1))}”）", block=b, sentence=b.sentences[0] if b.sentences else None, auto_fixable=True, fix_hint={"op": "label_punct"}))
                        break
                continue
            if b.level > last_level + 1:
                names = {1: "“一、”", 2: "“（一）”", 3: "“1.”", 4: "“（1）”"}
                out.append(
                    ctx.issue(
                        "GW-STRUCT-001",
                        IssueType.NUMBERING,
                        f"层次跳级：{names[b.level]}之上缺少{names[b.level - 1]}一级（结构层次序数依次为“一、”“（一）”“1.”“（1）”）",
                        block=b,
                        needs_human=True,
                    )
                )
            last_level = b.level
            counters[b.level] += 1
            for deeper in range(b.level + 1, 5):
                counters[deeper] = 0
            exp = expected_label(b.level, counters[b.level])
            if not LEVEL_RE[b.level].match(b.label or ""):
                out.append(ctx.issue("GW-STRUCT-001", IssueType.NUMBERING, f"第{b.level}层序数格式应为“{exp}”，当前为“{b.label}”", block=b, auto_fixable=True, fix_hint={"set_label": exp}))
            elif b.label != exp:
                out.append(ctx.issue("GW-STRUCT-002", IssueType.NUMBERING, f"同级序数应连续编号：此处应为“{exp}”，当前为“{b.label}”", block=b, auto_fixable=True, fix_hint={"set_label": exp}))
    return out


CHECKERS = [check_punctuation, check_numbers, check_relative_time, check_wording, check_structure]
