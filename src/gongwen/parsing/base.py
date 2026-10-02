"""材料解析基础：原始材料不变，结构化副本可加工；每个抽取结果都能返回原位置。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..schemas.common import Locator
from ..schemas.sources import SourceRelation, SourceUnit, TableData

HEADING_RE = re.compile(r"^\s*(第[一二三四五六七八九十百]+[章节条]|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+\.|（\d+）)")
NOTE_RE = re.compile(r"^\s*(注|备注|说明|注释)\s*[\d一二三四五六七八九十]*\s*[：:]")
CAPTION_RE = re.compile(r"^\s*(表|附表)\s*[\d一二三四五六七八九十]+")


@dataclass
class HiddenContent:
    """准入扫描要覆盖的“看不见的内容”：批注、修订痕迹、隐藏文字、隐藏工作表、元数据等。"""

    where: str
    text: str


@dataclass
class ParseResult:
    units: list[SourceUnit] = field(default_factory=list)
    tables: list[TableData] = field(default_factory=list)
    relations: list[SourceRelation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hidden: list[HiddenContent] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    def visible_text(self) -> str:
        return "\n".join(u.text for u in self.units if u.kind not in ("comment",))


class UnitBuilder:
    def __init__(self, material_id: str):
        self.material_id = material_id
        self.n = 0
        self.result = ParseResult()

    def add(self, kind: str, text: str, path: str, parent_id: str | None = None, **attrs: str) -> SourceUnit:
        self.n += 1
        uid = f"{self.material_id}.u{self.n}"
        u = SourceUnit(
            unit_id=uid,
            material_id=self.material_id,
            kind=kind,
            text=text,
            locator=Locator(material_id=self.material_id, kind=kind, path=path, excerpt=text[:60]),
            parent_id=parent_id,
            attrs={k: str(v) for k, v in attrs.items()},
        )
        self.result.units.append(u)
        return u


def classify_line(text: str) -> str:
    if HEADING_RE.match(text) and len(text) < 60:
        return "heading"
    return "paragraph"


def table_from_rows(material_id: str, table_id: str, rows: list[list[str]], prefix: str, title: str = "") -> TableData:
    rows = [[(c or "").strip() for c in r] for r in rows]
    rows = [r for r in rows if any(c for c in r)]
    header = rows[0] if rows else []
    return TableData(table_id=table_id, material_id=material_id, title=title, header=header, rows=rows[1:], locator_prefix=prefix)


def ext_of(filename: str) -> str:
    return Path(filename).suffix.lower()
