"""材料解析：按类型分派解析器，输出带原文定位的结构化副本。"""

from __future__ import annotations

from .base import HiddenContent, ParseResult, ext_of
from .docx_parser import parse_docx
from .pdf_parser import parse_pdf
from .text_parser import parse_csv, parse_text
from .xlsx_parser import parse_xlsx

PARSERS = {
    ".docx": parse_docx,
    ".xlsx": parse_xlsx,
    ".pdf": parse_pdf,
    ".csv": parse_csv,
    ".txt": parse_text,
    ".md": parse_text,
    ".markdown": parse_text,
}


def parse_bytes(material_id: str, filename: str, data: bytes) -> ParseResult:
    fn = PARSERS.get(ext_of(filename))
    if fn is None:
        res = ParseResult()
        res.warnings.append(f"不支持的文件类型：{ext_of(filename) or '(无扩展名)'}，未解析")
        return res
    return fn(material_id, data)


__all__ = ["HiddenContent", "ParseResult", "parse_bytes", "PARSERS"]
