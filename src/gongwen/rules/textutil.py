"""文本工具：句子切分、数字抽取与归一、中文数字转换。"""

from __future__ import annotations

import re
from dataclasses import dataclass

SENT_END = "。！？；"
_SENT_RE = re.compile(r"[^。！？；\n]+[。！？；]?|\n")

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}


def split_sentences(text: str) -> list[str]:
    out = []
    buf = ""
    depth = 0
    for ch in text:
        buf += ch
        if ch in "“（《〔":
            depth += 1
        elif ch in "”）》〕" and depth:
            depth -= 1
        if ch in SENT_END and depth == 0:
            out.append(buf.strip())
            buf = ""
        elif ch == "\n":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return [s for s in out if s]


def cn_to_number(s: str) -> float | None:
    """把“二十”“一百二十”“三千五百万”等中文数字转为数值。"""
    if not s or any(c not in CN_DIGITS and c not in CN_UNITS for c in s):
        return None
    total, section, number = 0, 0, 0
    for c in s:
        if c in CN_DIGITS:
            number = CN_DIGITS[c]
        else:
            unit = CN_UNITS[c]
            if unit >= 10_000:
                section = (section + number) * unit
                total += section
                section = 0
            else:
                section += (number or 1) * unit
            number = 0
    return float(total + section + number)


MONEY_UNITS = {"元": 1e-4, "千元": 1e-1, "万元": 1.0, "亿元": 1e4}
COUNT_UNITS = {"个", "家", "人", "项", "所", "次", "台", "套", "座", "处", "条", "件", "名", "户", "间", "张", "批", "类", "支", "辆", "床", "个点"}
MEASURE_UNITS = {"平方米", "公里", "千米", "米", "吨", "天", "日", "小时", "分钟", "岁", "亩", "公顷"}

_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.〔第])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(亿元|万元|千元|元|个百分点|%|％|亿|万|"
    + "|".join(sorted(COUNT_UNITS | MEASURE_UNITS, key=len, reverse=True))
    + r")?"
)
_CN_NUM_RE = re.compile(r"([零〇一二两三四五六七八九十百千万亿]{1,8})(" + "|".join(sorted(COUNT_UNITS, key=len, reverse=True)) + r"|万元|亿元|元)")

# 不参与“数字来源”核验的模式：发文字号、条款号、附件编号、标准号、序号、日期等
_EXCLUDE = [
    re.compile(r"〔\d{4}〕\d+号"),
    re.compile(r"第\d+号"),
    re.compile(r"GB/?T?\s*\d+\s*[—\-–]\s*\d{4}"),
    re.compile(r"附件\s*\d+"),
    re.compile(r"(表|图|附表)\s*\d+"),
    re.compile(r"^\s*（?\d+[\.）]"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{4}年\d{1,2}月"),
    re.compile(r"\d{4}年(度)?"),
    re.compile(r"\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{4}\s*[—\-–～~]\s*\d{4}年"),
    re.compile(r"第[一二三四五六七八九十百]+[条款项章节]"),
    re.compile(r"\d+:\d+"),
]


@dataclass
class NumberMention:
    raw: str
    value: float
    unit: str
    kind: str  # money/count/percent/measure/plain
    start: int
    end: int

    def normalized(self) -> tuple[float, str]:
        if self.kind == "money":
            return round(self.value * MONEY_UNITS.get(self.unit, 1.0), 6), "万元"
        if self.unit in ("%", "％"):
            return round(self.value, 6), "%"
        return round(self.value, 6), self.unit


def _kind(unit: str) -> str:
    if unit in MONEY_UNITS:
        return "money"
    if unit in ("%", "％", "个百分点"):
        return "percent"
    if unit in COUNT_UNITS:
        return "count"
    if unit in MEASURE_UNITS:
        return "measure"
    return "plain"


def _excluded_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for rx in _EXCLUDE:
        spans += [(m.start(), m.end()) for m in rx.finditer(text)]
    return spans


def extract_numbers(text: str, include_plain: bool = False) -> list[NumberMention]:
    spans = _excluded_spans(text)
    out: list[NumberMention] = []
    for m in _NUM_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        whole = m.group(1).replace(",", "")
        val = float(whole + (m.group(2) or ""))
        unit = m.group(3) or ""
        if unit in ("万", "亿") and text[m.end(): m.end() + 1] == "元":
            unit += "元"
        if unit == "万":
            val *= 1  # “万”作数量级（如“3万人”）按计数处理时保留原值
        kind = _kind(unit)
        if kind == "plain" and not include_plain:
            continue
        out.append(NumberMention(m.group(0), val, unit, kind, m.start(), m.end()))
    for m in _CN_NUM_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        cn = m.group(1)
        if m.start() > 0 and (text[m.start() - 1].isdigit() or text[m.start() - 1] == "."):
            continue  # “100万元”中的“万”是单位而不是数字
        if set(cn) <= set("万亿千百"):
            continue
        val = cn_to_number(cn)
        if val is None:
            continue
        # 排除“一文一事”“一律”等定型词：要求后接计量单位，且不是“第X”
        if m.start() > 0 and text[m.start() - 1] == "第":
            continue
        unit = m.group(2)
        out.append(NumberMention(m.group(0), val, unit, _kind(unit), m.start(), m.end()))
    return out


def same_quantity(a: NumberMention, value: float, unit: str, tol: float = 1e-6) -> bool:
    av, au = a.normalized()
    kind = _kind(unit)
    if kind == "money":
        bv, bu = round(value * MONEY_UNITS.get(unit, 1.0), 6), "万元"
    elif unit in ("%", "％"):
        bv, bu = round(value, 6), "%"
    else:
        bv, bu = round(value, 6), unit
    if au != bu and not (kind == "count" and a.kind == "count"):
        return False
    return abs(av - bv) <= tol * max(1.0, abs(bv))


DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
YEAR2_RE = re.compile(r"(?<![\d〔])(\d{2})年(?!\d)")


def find_any(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in text]
