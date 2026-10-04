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
COUNT_UNITS = {"个", "家", "人", "项", "所", "次", "台", "套", "座", "处", "条", "件", "名", "户", "间", "张", "批", "类", "支", "辆", "床", "个点", "人次", "户次", "批次", "场次"}
MEASURE_UNITS = {"平方米", "公里", "千米", "米", "吨", "天", "日", "小时", "分钟", "岁", "亩", "公顷"}

_UNIT_ALT = "|".join(sorted(COUNT_UNITS | MEASURE_UNITS, key=len, reverse=True))
# 数字 +（余/多）+ 单位；“3万人次”“1.2亿人次”中“万/亿”是数量级，后接计数或计量单位
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_.〔第])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*([余多])?\s*(亿元|万元|千元|元|个百分点|%|％|(?:亿|万)(?:余|多)?(?:" + _UNIT_ALT + r")?|" + _UNIT_ALT + r")?"
)
_RANGE_NEXT = re.compile(r"\s*[～~—\-至到]\s*\d")
_CN_NUM_RE = re.compile(r"([零〇一二两三四五六七八九十百千万亿]{1,8})(" + "|".join(sorted(COUNT_UNITS, key=len, reverse=True)) + r"|万元|亿元|元)")

# 不参与“数字来源”核验的模式：发文字号、条款号、附件编号、标准号、序号、日期等
_EXCLUDE = [
    re.compile(r"〔\d{4}〕\d+号"),
    re.compile(r"第\d+号"),
    re.compile(r"GB/?T?\s*\d+\s*[—\-–]\s*\d{4}"),
    re.compile(r"附件\s*\d+"),
    re.compile(r"(表|图|附表)\s*\d+"),
    re.compile(r"^\s*（?\d+[\.）](?!\d)"),
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
    pending_range: list[tuple[re.Match, float]] = []
    for m in _NUM_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        whole = m.group(1).replace(",", "")
        val = float(whole + (m.group(2) or ""))
        unit = m.group(4) or ""
        if unit in ("万", "亿") and text[m.end(): m.end() + 1] == "元":
            unit += "元"
        mag = re.match(r"^(亿|万)(?:余|多)?(.+)$", unit)
        if mag and mag.group(2) in COUNT_UNITS | MEASURE_UNITS:
            # “3万人次”= 30000 人次：数量级并入数值，单位取计数或计量单位
            val *= 1e8 if mag.group(1) == "亿" else 1e4
            unit = mag.group(2)
        kind = _kind(unit)
        if kind == "plain" and not unit and _RANGE_NEXT.match(text, m.end()):
            # 范围下限（“10～20个”中的 10）：单位随上限
            pending_range.append((m, val))
            continue
        for pm, pv in pending_range:
            joined = re.fullmatch(r"\s*[～~—\-至到]\s*", text[pm.end() : m.start()])
            if joined and kind != "plain":
                out.append(NumberMention(pm.group(0), pv, unit, kind, pm.start(), pm.end()))
            elif include_plain:
                out.append(NumberMention(pm.group(0), pv, "", "plain", pm.start(), pm.end()))
        pending_range = []
        if kind == "plain" and not include_plain:
            continue
        out.append(NumberMention(m.group(0), val, unit, kind, m.start(), m.end()))
    if include_plain:
        out += [NumberMention(pm.group(0), pv, "", "plain", pm.start(), pm.end()) for pm, pv in pending_range]
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
    # 计数单位须一致（“8次”不是“8个”）；账本中无单位的表格数值可与任一计数单位比对
    if au != bu and not (a.kind == "count" and not unit):
        return False
    return abs(av - bv) <= tol * max(1.0, abs(bv))


DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
YEAR2_RE = re.compile(r"(?<![\d〔])(\d{2})年(?!\d)")


def find_any(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in text]


# ---------------------------------------------------------------- 表格
_TOTAL_LABEL = re.compile(r"^(合计|总计|共计|总合计)$")
_SUBTOTAL_LABEL = re.compile(r"^(小计|分计)$")
_INDEX_HEADER = re.compile(r"^(序号|编号|序|No\.?|NO\.?|#)$", re.I)
NON_ADDITIVE_HEADER = re.compile(r"(率|占比|比例|比重|%|％|单价|均价|平均|序号|编号|年份|年度|排名|名次|同比|环比|增幅|增速|增长)")


def _label_text(cell: str) -> str:
    """“合  计”“合计（万元）”→“合计”：去掉空白与括注后再判断。"""
    return re.sub(r"[（(][^）)]*[）)]", "", re.sub(r"[\s　]+", "", cell or ""))


def is_total_row(cells: list[str]) -> bool:
    return any(_TOTAL_LABEL.match(_label_text(c)) for c in cells if c and not re.fullmatch(r"[\d.,，\s-]+", c))


def is_subtotal_row(cells: list[str]) -> bool:
    return any(_SUBTOTAL_LABEL.match(_label_text(c)) for c in cells if c and not re.fullmatch(r"[\d.,，\s-]+", c))


def is_index_header(h: str) -> bool:
    return bool(_INDEX_HEADER.match(_label_text(h)))


def label_column(header: list[str], body: list[list[str]]) -> int:
    """项目名称所在列：跳过“序号”列和全为整数的编号列。"""
    for c, h in enumerate(header):
        vals = [r[c].strip() for r in body if c < len(r) and r[c].strip()]
        if _INDEX_HEADER.match(_label_text(h)) or (vals and all(re.fullmatch(r"\d{1,3}", v) for v in vals) and not h.strip()):
            continue
        return c
    return 0


# ---------------------------------------------------------------- 小句
_CLAUSE_DELIMS = "，,；;：:。！？!?\n"


def clause_span(text: str, pos: int) -> tuple[int, int]:
    """pos 所在小句的起止位置（按逗号、分号、冒号、句末标点切分）。"""
    start = max((text.rfind(d, 0, pos) for d in _CLAUSE_DELIMS), default=-1) + 1
    ends = [i for i in (text.find(d, pos) for d in _CLAUSE_DELIMS) if i >= 0]
    return start, (min(ends) if ends else len(text))


def clauses(text: str) -> list[tuple[int, int]]:
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in _CLAUSE_DELIMS:
            if text[start:i].strip():
                out.append((start, i))
            start = i + 1
    if text[start:].strip():
        out.append((start, len(text)))
    return out


def mention_of(text: str, value: float, unit: str) -> "NumberMention | None":
    """句中与给定取值相同的数字（用于把事实定位到它所在的小句）。"""
    return next((m for m in extract_numbers(text) if same_quantity(m, value, unit)), None)
