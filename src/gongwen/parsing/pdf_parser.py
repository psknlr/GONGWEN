"""PDF 解析：逐页抽取文本并保留页码与行号；无文本层的扫描件不做猜测，提示人工核对。"""

from __future__ import annotations

import io

from pypdf import PdfReader

from .base import HiddenContent, ParseResult, UnitBuilder, classify_line


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
    for p_idx, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            empty_pages += 1
            continue
        for l_idx, ln in enumerate(lines, 1):
            b.add(classify_line(ln), ln, f"page{p_idx}.l{l_idx}", page=str(p_idx))
    if empty_pages:
        res.warnings.append(
            f"有 {empty_pages} 页没有文本层（可能是扫描件）：本系统不对扫描件做自动识别后直接采信，"
            "金额、日期、人名等关键字段须人工录入并保留原页供复核"
        )
    return res
