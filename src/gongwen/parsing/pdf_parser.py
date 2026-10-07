"""PDF 解析：逐页抽取文本并保留页码与行号；无文本层的扫描件不做猜测，提示人工核对。"""

from __future__ import annotations

import io
import re
from collections import Counter

from pypdf import PdfReader

from .base import HEADING_RE, HiddenContent, ParseResult, UnitBuilder, classify_line

_PAGE_NO = re.compile(r"^[—\-–－]\s*\d+\s*[—\-–－]$|^第\s*\d+\s*页(\s*共\s*\d+\s*页)?$|^\d+\s*/\s*\d+$")
_SYSTEM_LABEL = re.compile(r"^【(讨论稿|送审稿|排版稿)】")
_CJK = "\u4e00-\u9fff"
_END = tuple("。！？；：…”）】")
_STANDALONE = re.compile(r"^(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|附件[：:\d]|抄送[：:]|主送[：:]|（.{1,60}）$)")


def _despace(s: str) -> str:
    """去掉 PDF 文本层在汉字与数字之间插入的空格（“2026 年3 月”→“2026年3月”）；英文词间空格保留。"""
    s = re.sub(rf"(?<=[{_CJK}，。；：、（）《》“”])\s+(?=[{_CJK}0-9（《“])", "", s)
    return re.sub(rf"(?<=[0-9])\s+(?=[{_CJK}，。；：、）》”%％])", "", s)


def _width(s: str) -> float:
    return sum(0.5 if ord(c) < 128 else 1.0 for c in s)


def _reflow(pages: list[list[str]]) -> list[tuple[str, str]]:
    """把 PDF 的视觉行还原为段落：去掉页码与各页重复的页眉页脚，排满一行且未以句末标点结束的行与下一行合并
    （段落跨页时同样合并）；标题、层次标题、日期、附件说明等短行单独成段。返回 (段落文字, 定位)。"""
    edges = Counter(ln for lines in pages for ln in set(lines[:2] + lines[-2:]))
    repeated = {ln for ln, n in edges.items() if n >= 2 and len(pages) >= 2}
    cleaned = [[(i, _despace(ln)) for i, ln in enumerate(lines, 1) if ln not in repeated and not _PAGE_NO.match(ln) and not _SYSTEM_LABEL.match(ln)] for lines in pages]
    full = max((_width(t) for lines in cleaned for _, t in lines), default=0) * 0.85
    out: list[tuple[str, str]] = []
    prev_full = False
    for p_idx, lines in enumerate(cleaned, 1):
        for l_idx, text in lines:
            loc = f"page{p_idx}.l{l_idx}"
            joinable = out and prev_full and not out[-1][0].endswith(_END) and not HEADING_RE.match(text) and not _STANDALONE.match(text)
            if joinable:
                t0, loc0 = out[-1]
                out[-1] = (t0 + text, loc0.split("-")[0] + "-" + loc)
            else:
                out.append((text, loc))
            prev_full = _width(text) >= full
    return out


def parse_pdf(material_id: str, data: bytes) -> ParseResult:
    b = UnitBuilder(material_id)
    res = b.result
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        res.warnings.append(f"PDF 无法读取：{exc}")
        return res
    meta = reader.metadata or {}
    for k in ("/Title", "/Subject", "/Keywords", "/Author"):
        v = meta.get(k)
        if v:
            res.metadata[k.strip("/").lower()] = str(v)
    if res.metadata:
        res.hidden.append(HiddenContent("文档属性", "；".join(f"{k}={v}" for k, v in res.metadata.items())))
    empty_pages = 0
    pages: list[list[str]] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            empty_pages += 1
        pages.append(lines)
    for text, loc in _reflow(pages):
        b.add(classify_line(text), text, loc, page=loc.split(".")[0].replace("page", ""))
    if empty_pages:
        res.warnings.append(
            f"有 {empty_pages} 页没有文本层（可能是扫描件）：本系统不对扫描件做自动识别后直接采信，"
            "金额、日期、人名等关键字段须人工录入并保留原页供复核"
        )
    return res
