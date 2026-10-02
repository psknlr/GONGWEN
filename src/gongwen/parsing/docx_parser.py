"""DOCX 解析：正文段落与表格按文档顺序抽取，并单独收集批注、修订痕迹、隐藏文字、
页眉页脚、脚注和文档属性（这些都属于准入扫描范围）。直接读取 OOXML，不依赖渲染。"""

from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

from ..schemas.sources import SourceRelation
from .base import CAPTION_RE, NOTE_RE, HiddenContent, ParseResult, UnitBuilder, classify_line, table_from_rows

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
CP = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _q(tag: str) -> str:
    p, t = tag.split(":")
    return f"{{{W}}}{t}" if p == "w" else tag


def _run_hidden(r: ET.Element) -> bool:
    rpr = r.find("w:rPr", NS)
    if rpr is None:
        return False
    v = rpr.find("w:vanish", NS)
    return v is not None and v.get(_q("w:val"), "true") not in ("0", "false")


def _para_text(p: ET.Element) -> tuple[str, str, bool, bool]:
    """返回 (可见文字, 隐藏文字, 是否含插入修订, 是否含删除修订)。"""
    visible, hidden = [], []
    has_ins = p.find(".//w:ins", NS) is not None
    has_del = p.find(".//w:del", NS) is not None
    for r in p.iter(_q("w:r")):
        parts = []
        for child in r:
            if child.tag == _q("w:t"):
                parts.append(child.text or "")
            elif child.tag == _q("w:tab"):
                parts.append("\t")
            elif child.tag in (_q("w:br"), _q("w:cr")):
                parts.append("\n")
        txt = "".join(parts)
        if not txt:
            continue
        (hidden if _run_hidden(r) else visible).append(txt)
    return "".join(visible).strip(), "".join(hidden).strip(), has_ins, has_del


def _deleted_text(root: ET.Element) -> list[str]:
    out = []
    for d in root.iter(_q("w:del")):
        t = "".join(x.text or "" for x in d.iter(_q("w:delText")))
        if t.strip():
            out.append(t.strip())
    return out


def _cell_text(tc: ET.Element) -> str:
    return "\n".join(t for t in (_para_text(p)[0] for p in tc.findall("w:p", NS)) if t)


def _plain_text(xml_bytes: bytes) -> str:
    root = ET.fromstring(xml_bytes)
    return "\n".join(t for t in (_para_text(p)[0] for p in root.iter(_q("w:p"))) if t)


def parse_docx(material_id: str, data: bytes) -> ParseResult:
    b = UnitBuilder(material_id)
    res = b.result
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        res.warnings.append("文件不是有效的 DOCX（ZIP）结构")
        return res
    names = set(zf.namelist())
    root = ET.fromstring(zf.read("word/document.xml"))
    body = root.find("w:body", NS)
    para_no, table_no = 0, 0
    last_table: str | None = None
    caption = ""
    any_ins = any_del = False
    for el in list(body) if body is not None else []:
        if el.tag == _q("w:p"):
            text, hidden, ins, dele = _para_text(el)
            any_ins |= ins
            any_del |= dele
            if hidden:
                res.hidden.append(HiddenContent("隐藏文字", hidden))
            if not text:
                continue
            para_no += 1
            style = el.find("w:pPr/w:pStyle", NS)
            style_val = style.get(_q("w:val"), "") if style is not None else ""
            kind = "heading" if style_val.lower().startswith(("heading", "title")) or style_val.isdigit() else classify_line(text)
            u = b.add(kind, text, f"p{para_no}", style=style_val)
            if NOTE_RE.match(text) and last_table:
                res.relations.append(SourceRelation(kind="footnote_of", src=u.unit_id, dst=last_table, note=text))
                for t in res.tables:
                    if t.table_id == last_table:
                        t.notes.append(text)
            elif CAPTION_RE.match(text):
                caption = text
                last_table = None
            else:
                last_table = None
        elif el.tag == _q("w:tbl"):
            table_no += 1
            tid = f"{material_id}.t{table_no}"
            rows: list[list[str]] = []
            for r_idx, tr in enumerate(el.findall("w:tr", NS)):
                row = []
                for c_idx, tc in enumerate(tr.findall("w:tc", NS)):
                    ct = _cell_text(tc)
                    row.append(ct)
                    if ct:
                        b.add("table_cell", ct, f"table{table_no}.r{r_idx + 1}.c{c_idx + 1}", table=tid)
                rows.append(row)
            res.tables.append(table_from_rows(material_id, tid, rows, f"table{table_no}", title=caption))
            last_table, caption = tid, ""
    if any_ins or any_del:
        dels = _deleted_text(root)
        res.hidden.append(HiddenContent("修订痕迹", "；".join(dels) if dels else "（含插入修订）"))
        res.warnings.append("文档包含未接受的修订痕迹，处理前请确认以哪个版本为准")
    # 批注
    if "word/comments.xml" in names:
        croot = ET.fromstring(zf.read("word/comments.xml"))
        for i, c in enumerate(croot.findall("w:comment", NS), 1):
            t = "\n".join(x for x in (_para_text(p)[0] for p in c.iter(_q("w:p"))) if x)
            if t:
                author = c.get(_q("w:author"), "")
                b.add("comment", t, f"comment{i}", author=author)
                res.hidden.append(HiddenContent("批注", t))
    # 页眉页脚、脚注尾注
    for name in sorted(names):
        if name.startswith(("word/header", "word/footer")) and name.endswith(".xml"):
            t = _plain_text(zf.read(name))
            if t:
                res.hidden.append(HiddenContent("页眉页脚", t))
        if name in ("word/footnotes.xml", "word/endnotes.xml"):
            t = _plain_text(zf.read(name))
            if t:
                b.add("footnote", t, name.split("/")[-1].replace(".xml", ""))
    # 文档属性
    if "docProps/core.xml" in names:
        core = ET.fromstring(zf.read("docProps/core.xml"))
        for key, tag in (("title", "dc:title"), ("subject", "dc:subject"), ("creator", "dc:creator"), ("keywords", "cp:keywords"), ("description", "dc:description"), ("category", "cp:category")):
            el = core.find(tag, CP)
            if el is not None and (el.text or "").strip():
                res.metadata[key] = el.text.strip()
        meta_text = "；".join(f"{k}={v}" for k, v in res.metadata.items())
        if meta_text:
            res.hidden.append(HiddenContent("文档属性", meta_text))
    if "docProps/custom.xml" in names:
        t = ET.fromstring(zf.read("docProps/custom.xml"))
        vals = [x.text for x in t.iter() if x.text and x.text.strip()]
        if vals:
            res.hidden.append(HiddenContent("自定义属性", "；".join(vals)))
    return res
