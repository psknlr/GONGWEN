"""公文 Word 模板（.dotx）导出：按公文模板的生效参数设置页面、版心行网格、奇偶页页码（7.5），并定义具名段落样式
（公文标题、主送机关、正文、各级标题、附件说明、署名、成文日期、附注、版记等），便于直接在 Word 中撰写。

* 版头（发文机关标志、发文字号与红色分隔线）按模板排好，标志、字号取模板单位信息，没有时为【……】占位；
* 正文各要素以示例段落给出，均套用具名样式：在 Word 中回车后沿用同一样式，层次标题回车后转为“正文”；
* 文件主部件的内容类型为 Word 模板（wordprocessingml.template.main+xml），双击即以模板新建文档；
* 与系统生成的 DOCX 不同，模板中的版记是普通段落（不锚定在末页版心底部），定稿时须放在最后一面底部（7.4.1）；
  加盖印章时署名应以成文日期为准居中（7.3.5.1），样式只给出右空四字的近似位置。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Twips

from ..schemas.ir import DocumentIR, Header
from .docx_compiler import PPR_SEQ, RPR_SEQ, Compiler, insert_ordered
from .fonts import ROLE_LABELS
from .profile import MM_PER_PT, LayoutProfile

DOCUMENT_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
TEMPLATE_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"


def _style(c: Compiler, sid: str, name: str, *, font: str, size: str | float, align=None, first: float = 0, left: float = 0, right: float = 0, line_pt: float | None = None, outline: int | None = None, keep_next: bool = False, color: str | None = None, spacing: bool = True) -> Any:
    doc = c.doc
    st = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    st.style_id = sid
    st.base_style = doc.styles["Normal"]
    st.quick_style = True
    pt = c.p.size(size) if isinstance(size, str) else float(size)
    st.font.size = Pt(pt)
    if color:
        st.font.color.rgb = RGBColor.from_string(color)
    fname = c.font_name(font)
    rpr = st.element.get_or_add_rPr()
    rf = rpr.get_or_add_rFonts()
    for attr in ("w:eastAsia", "w:ascii", "w:hAnsi", "w:cs"):
        rf.set(qn(attr), fname)
    rf.set(qn("w:hint"), "eastAsia")
    if spacing and abs(pt - c.p.body_pt) < 0.01:
        sp = OxmlElement("w:spacing")
        sp.set(qn("w:val"), str(c.p.data["grid"]["char_spacing_twips"]))
        insert_ordered(rpr, sp, RPR_SEQ)
    pf = st.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = Twips(round((line_pt or c.p.line_pt) * 20))
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.widow_control = False
    pf.keep_with_next = keep_next or None
    if align is not None:
        pf.alignment = align
    if first:
        pf.first_line_indent = Pt(first)
    if left:
        pf.left_indent = Pt(left)
    if right:
        pf.right_indent = Pt(right)
    ppr = st.element.get_or_add_pPr()
    snap = OxmlElement("w:snapToGrid")
    snap.set(qn("w:val"), "0")
    insert_ordered(ppr, snap, PPR_SEQ)
    if outline is not None:
        ol = OxmlElement("w:outlineLvl")
        ol.set(qn("w:val"), str(outline))
        insert_ordered(ppr, ol, PPR_SEQ)
    return st


def _borders(style, edges: list[tuple[str, float]]) -> None:
    ppr = style.element.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    for edge, mm in edges:
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(max(2, int(round(mm / MM_PER_PT * 8)))))
        el.set(qn("w:space"), "1")
        el.set(qn("w:color"), "000000")
        bdr.append(el)
    insert_ordered(ppr, bdr, PPR_SEQ)


def build_styles(c: Compiler) -> dict[str, Any]:
    """在文档中定义公文具名段落样式，返回 名称 → 样式。字体字号、缩进均取配置档（模板）的生效参数。"""
    p = c.p
    pitch = p.char_pitch_pt
    body_font = p.el("body").get("font", "fangsong")
    body_size = p.el("body")["size"]
    indent = float(p.el("body").get("first_indent_chars", 2)) * pitch
    levels = p.el("levels")
    title = p.el("title")
    imp = p.el("imprint")
    imp_char = p.size(imp["size"])
    seal = p.el("signature_seal")
    out: dict[str, Any] = {}
    out["公文标题"] = _style(c, "GwTitle", "公文标题", font=title.get("font", "xiaobiaosong"), size=title["size"], align=WD_ALIGN_PARAGRAPH.CENTER, keep_next=True, outline=None)
    out["主送机关"] = _style(c, "GwRecipients", "主送机关", font=body_font, size=body_size)
    out["正文"] = _style(c, "GwBody", "正文", font=body_font, size=body_size, first=indent)
    names = {1: f"一级标题（{ROLE_LABELS.get(levels.get(1), '黑体')}）", 2: f"二级标题（{ROLE_LABELS.get(levels.get(2), '楷体')}）", 3: "三级标题", 4: "四级标题"}
    for lv in (1, 2, 3, 4):
        out[names[lv]] = _style(c, f"GwHeading{lv}", names[lv], font=levels.get(lv, body_font), size=body_size, first=indent, outline=lv - 1, keep_next=lv <= 2)
        out[names[lv]].next_paragraph_style = out["正文"]
    an = p.el("attachment_note")
    base = float(an.get("left_indent_chars", 2))
    out["附件说明"] = _style(c, "GwAttachNote", "附件说明", font=body_font, size=body_size, left=(base + 3) * pitch, first=-3 * pitch)
    out["署名"] = _style(c, "GwSignature", "署名", font=body_font, size=body_size, align=WD_ALIGN_PARAGRAPH.RIGHT, right=float(seal["date_right_indent_chars"]) * pitch, keep_next=True)
    out["成文日期"] = _style(c, "GwDate", "成文日期", font=body_font, size=body_size, align=WD_ALIGN_PARAGRAPH.RIGHT, right=float(seal["date_right_indent_chars"]) * pitch)
    out["附注"] = _style(c, "GwNote", "附注", font=body_font, size=body_size, left=float(p.el("note")["left_indent_chars"]) * pitch)
    ind = float(imp["indent_chars"])
    out["版记"] = _style(c, "GwImprint", "版记", font=imp.get("font", "fangsong"), size=imp["size"], left=imp_char * (ind + 3), first=-imp_char * 3, right=imp_char * ind, line_pt=p.line_pt * 0.95, spacing=False)
    _borders(out["版记"], [("top", imp["outer_rule_mm"]), ("bottom", imp["outer_rule_mm"]), ("between", imp["inner_rule_mm"])])
    om = p.el("organ_mark")
    mark_pt = p.size(om["max_size"])
    out["发文机关标志"] = _style(c, "GwOrganMark", "发文机关标志", font=om.get("font", "xiaobiaosong"), size=mark_pt, align=WD_ALIGN_PARAGRAPH.CENTER, line_pt=mark_pt * 1.02, color=str(om.get("color", "FF0000")), spacing=False)
    dn = p.el("doc_number")
    out["发文字号"] = _style(c, "GwDocNumber", "发文字号", font=dn.get("font", body_font), size=dn.get("size", body_size), align=WD_ALIGN_PARAGRAPH.CENTER)
    for name in ("公文标题", "主送机关", "附件说明", "署名", "附注"):
        out[name].next_paragraph_style = out["正文"]
    out["署名"].next_paragraph_style = out["成文日期"]
    return out


def export_dotx(profile: LayoutProfile, path: str | Path, *, name: str = "") -> Path:
    """按配置档（模板）导出 Word 模板 .dotx。"""
    unit = profile.unit or {}
    organ = unit.get("organ_mark") or "【发文机关标志】"
    prefix = unit.get("doc_number_prefix")
    ir = DocumentIR(doc_id="DOTX", matter_id="-", format_type="general", header=Header(organ_mark=organ, doc_number=f"{prefix}〔　　〕　号" if prefix else "【发文字号】"))
    c = Compiler(ir, profile, draft_label=False)
    c.setup_page()
    c.page_numbers()
    st = build_styles(c)
    c.header_block()  # 版头：发文机关标志、发文字号与红色分隔线（7.2）
    c.blank(int(profile.el("title")["blank_lines_before"]))
    doc = c.doc

    def add(text: str, style: str):
        return doc.add_paragraph(text, style=st[style])

    add("【公文标题】", "公文标题")
    c.blank(int(profile.el("recipients")["blank_lines_before"]))
    add("【主送机关】：", "主送机关")
    add("【正文：每个自然段左空二字，回行顶格。数字、年份不回行。】", "正文")
    lv = [k for k in st if k.startswith(("一级", "二级", "三级", "四级"))]
    for text, style in zip(("一、【一级标题】", "（一）【二级标题】", "1.【三级标题】", "（1）【四级标题】"), lv):
        add(text, style)
        add("【正文】", "正文")
    c.blank(int(profile.el("attachment_note")["blank_lines_before"]))
    add("附件：1.【附件名称】", "附件说明")
    c.blank(2)
    add(unit.get("organ_mark", "").removesuffix("文件") or "【发文机关署名】", "署名")
    add("【成文日期】", "成文日期")
    add("（【附注】）", "附注")
    c.blank(1)
    cc = "，".join(unit.get("cc") or []) or "【抄送机关】"
    add(f"抄送：{cc}。", "版记")
    par = add(f"{unit.get('printer') or '【印发机关】'}\t【印发日期】印发", "版记")
    imp = profile.el("imprint")
    char = profile.size(imp["size"])
    par.paragraph_format.first_line_indent = Pt(0)
    par.paragraph_format.left_indent = Pt(char * float(imp["indent_chars"]))
    par.paragraph_format.tab_stops.add_tab_stop(Pt(profile.type_width_pt - char * float(imp["indent_chars"]) * 2), WD_TAB_ALIGNMENT.RIGHT)
    cp = doc.core_properties
    label = name or profile.template or profile.id
    cp.title = f"公文模板：{label}"
    cp.subject = "GB/T 9704—2012 党政机关公文格式" + (f"（模板“{profile.template}”，偏离国标 {sum(1 for x in profile.changes if x.deviates)} 项）" if profile.template else "")
    cp.comments = "由公文智能体（gongwen template export-dotx）导出；方正小标宋简体、仿宋_GB2312 等授权字库须自行安装。"
    cp.author = "GONGWEN 公文智能体"
    buf = io.BytesIO()
    doc.save(buf)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _as_template(buf.getvalue(), path)
    return path


def _as_template(data: bytes, path: Path) -> None:
    """把主文档部件的内容类型改为 Word 模板（.dotx）。"""
    src = zipfile.ZipFile(io.BytesIO(data))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            content = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                text = content.decode("utf-8")
                if DOCUMENT_CT not in text:
                    raise RuntimeError("未找到主文档内容类型，无法转为模板")
                content = text.replace(DOCUMENT_CT, TEMPLATE_CT).encode("utf-8")
            out.writestr(item, content)


__all__ = ["DOCUMENT_CT", "TEMPLATE_CT", "build_styles", "export_dotx"]
