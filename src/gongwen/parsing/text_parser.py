"""纯文本 / Markdown / CSV 解析。"""

from __future__ import annotations

import csv
import io

from ..schemas.sources import SourceRelation
from .base import CAPTION_RE, NOTE_RE, ParseResult, UnitBuilder, classify_line, table_from_rows


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_text(material_id: str, data: bytes) -> ParseResult:
    text = decode(data)
    b = UnitBuilder(material_id)
    lines = text.splitlines()
    i = 0
    para_no = 0
    table_no = 0
    last_table_id: str | None = None
    caption: str = ""
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # Markdown 表格
        if line.startswith("|") and line.endswith("|"):
            rows = []
            start = i
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    rows.append(cells)
                i += 1
            table_no += 1
            tid = f"{material_id}.t{table_no}"
            t = table_from_rows(material_id, tid, rows, f"table{table_no}", title=caption)
            b.result.tables.append(t)
            for r_idx, row in enumerate(rows):
                for c_idx, cell in enumerate(row):
                    if cell:
                        b.add("table_cell", cell, f"table{table_no}.r{r_idx + 1}.c{c_idx + 1}", table=tid, line=str(start + r_idx + 1))
            last_table_id = tid
            caption = ""
            continue
        para_no += 1
        kind = classify_line(line)
        u = b.add(kind, line, f"p{para_no}", line=str(i + 1))
        if NOTE_RE.match(line) and last_table_id:
            b.result.relations.append(SourceRelation(kind="footnote_of", src=u.unit_id, dst=last_table_id, note=line))
            for t in b.result.tables:
                if t.table_id == last_table_id:
                    t.notes.append(line)
        elif CAPTION_RE.match(line):
            caption = line
            last_table_id = None
        else:
            last_table_id = None
        i += 1
    return b.result


def parse_csv(material_id: str, data: bytes) -> ParseResult:
    text = decode(data)
    rows = list(csv.reader(io.StringIO(text)))
    b = UnitBuilder(material_id)
    tid = f"{material_id}.t1"
    b.result.tables.append(table_from_rows(material_id, tid, rows, "table1"))
    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row):
            if cell.strip():
                b.add("table_cell", cell.strip(), f"table1.r{r_idx + 1}.c{c_idx + 1}", table=tid)
    return b.result
