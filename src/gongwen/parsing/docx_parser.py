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


_CN = "零一二三四五六七八九十"
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _cn_number(n: int) -> str:
    if n <= 10:
        return "十" if n == 10 else _CN[n]
    tens, ones = divmod(n, 10)
    return ("" if tens == 1 else _CN[tens]) + "十" + (_CN[ones] if ones else "")


def _roman(n: int) -> str:
    out = ""
    for v, r in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out, n = out + r, n - v
    return out


def _format_number(n: int, fmt: str) -> str:
    if fmt in ("decimal", "decimalHalfWidth", "decimalFullWidth", ""):
        return str(n)
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt.startswith("chinese") or fmt in ("ideographTraditional", "ideographDigital", "japaneseCounting", "taiwaneseCounting", "koreanCounting"):
        return _cn_number(n)
    if fmt.startswith("decimalEnclosedCircle"):
        return _CIRCLED[n - 1] if 0 < n <= len(_CIRCLED) else str(n)
    if fmt == "upperLetter":
        return chr(64 + (n - 1) % 26 + 1)
    if fmt == "lowerLetter":
        return chr(96 + (n - 1) % 26 + 1)
    if fmt == "upperRoman":
        return _roman(n)
    if fmt == "lowerRoman":
        return _roman(n).lower()
    return str(n)


class _Numbering:
    """Word 自动编号：段落文本中没有“一、”“（一）”等序号，须按 numbering.xml 还原，
    否则层次结构与层次序数检查都会失真。编号计数按抽象列表连续，startOverride 时重新起算。"""

    def __init__(self, zf: zipfile.ZipFile, names: set[str]):
        self.levels: dict[str, dict[int, tuple[str, str, int]]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.style_num: dict[str, tuple[str, int]] = {}
        self.counters: dict[str, list[int | None]] = {}
        self.started: set[str] = set()
        if "word/numbering.xml" in names:
            root = ET.fromstring(zf.read("word/numbering.xml"))
            for a in root.findall("w:abstractNum", NS):
                aid = a.get(_q("w:abstractNumId"), "")
                lv = {}
                for l in a.findall("w:lvl", NS):
                    ilvl = int(l.get(_q("w:ilvl"), "0"))
                    fmt = l.find("w:numFmt", NS)
                    txt = l.find("w:lvlText", NS)
                    start = l.find("w:start", NS)
                    lv[ilvl] = (fmt.get(_q("w:val"), "decimal") if fmt is not None else "decimal", txt.get(_q("w:val"), "") if txt is not None else "", int(start.get(_q("w:val"), "1")) if start is not None else 1)
                self.levels[aid] = lv
            for n in root.findall("w:num", NS):
                nid = n.get(_q("w:numId"), "")
                aref = n.find("w:abstractNumId", NS)
                overrides = {}
                for o in n.findall("w:lvlOverride", NS):
                    so = o.find("w:startOverride", NS)
                    if so is not None:
                        overrides[int(o.get(_q("w:ilvl"), "0"))] = int(so.get(_q("w:val"), "1"))
                self.nums[nid] = (aref.get(_q("w:val"), "") if aref is not None else "", overrides)
        if "word/styles.xml" in names:
            sroot = ET.fromstring(zf.read("word/styles.xml"))
            for st in sroot.findall("w:style", NS):
                np_ = st.find("w:pPr/w:numPr", NS)
                if np_ is not None:
                    nid = np_.find("w:numId", NS)
                    il = np_.find("w:ilvl", NS)
                    if nid is not None:
                        self.style_num[st.get(_q("w:styleId"), "")] = (nid.get(_q("w:val"), ""), int(il.get(_q("w:val"), "0")) if il is not None else 0)

    def label(self, p: ET.Element, style_val: str) -> str:
        np_ = p.find("w:pPr/w:numPr", NS)
        num_id, ilvl = None, 0
        if np_ is not None:
            nid = np_.find("w:numId", NS)
            il = np_.find("w:ilvl", NS)
            num_id = nid.get(_q("w:val"), "") if nid is not None else None
            ilvl = int(il.get(_q("w:val"), "0")) if il is not None else 0
        if num_id is None and style_val in self.style_num:
            num_id, ilvl = self.style_num[style_val]
        if not num_id or num_id == "0" or num_id not in self.nums:
            return ""
        aid, overrides = self.nums[num_id]
        lv = self.levels.get(aid, {})
        if ilvl not in lv:
            return ""
        counters = self.counters.setdefault(aid, [None] * 9)
        if num_id not in self.started:
            self.started.add(num_id)
            for k, v in overrides.items():
                if k < 9:
                    counters[k] = v - 1
        fmt, text, start = lv[ilvl]
        counters[ilvl] = start if counters[ilvl] is None else counters[ilvl] + 1
        for k in range(ilvl + 1, 9):
            counters[k] = None
        if fmt in ("bullet", "none"):
            return ""
        out = text
        for k in range(9):
            if f"%{k + 1}" in out:
                kfmt, _, kstart = lv.get(k, ("decimal", "", 1))
                out = out.replace(f"%{k + 1}", _format_number(counters[k] if counters[k] is not None else kstart, kfmt))
        return out


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
    numbering = _Numbering(zf, names)
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
            label = numbering.label(el, style_val)
            if label and not text.startswith(label):
                text = label + text  # 自动编号还原为文字序号
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
