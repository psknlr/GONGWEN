"""实际渲染核验：DOCX → PDF（LibreOffice）后测量版面，而不是只检查生成参数。

* 测量项：页数与幅面、首页是否显示正文（7.3.3）、发文机关标志位置（7.2.4）、
  订口（5.2.1）、页码一字线位置（7.5）、每面行数与每行字数（5.2.3，一般）；
* 字体：对照系统已安装字体与 PDF 实际嵌入字体，字体缺失或被替代时明确报告，
  不能在字体被替代的情况下声称“已完全满足指定版式”；
* 环境缺少 LibreOffice 时，结论为“未进行实际渲染核验”，而不是“通过”。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutCheck, RenderInfo
from .profile import MM_PER_PT, LayoutProfile

_WORD_RE = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>')
_LINE_RE = re.compile(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>', re.S)
_PAGE_RE = re.compile(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S)


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def x0_mm(self) -> float:
        return self.x0 * MM_PER_PT

    @property
    def y0_mm(self) -> float:
        return self.y0 * MM_PER_PT

    @property
    def yc_mm(self) -> float:
        return (self.y0 + self.y1) / 2 * MM_PER_PT


def tools_available() -> dict[str, str | None]:
    return {k: shutil.which(k) for k in ("soffice", "pdfinfo", "pdffonts", "pdftotext", "fc-list")}


def libreoffice_version() -> str:
    try:
        out = subprocess.run(["soffice", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        return out.split("\n")[0][:60] or "LibreOffice"
    except Exception:
        return "LibreOffice"


def installed_font_families() -> set[str]:
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return set()
    fams: set[str] = set()
    for line in out.splitlines():
        for f in line.split(","):
            fams.add(f.strip())
    return fams


def to_pdf(docx: Path, out_dir: Path, timeout: int = 180) -> Path | None:
    profile_dir = Path(tempfile.mkdtemp(prefix="gw-lo-"))
    try:
        subprocess.run(
            ["soffice", f"-env:UserInstallation=file://{profile_dir}", "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "HOME": str(profile_dir)},
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    pdf = out_dir / (docx.stem + ".pdf")
    return pdf if pdf.is_file() else None


def pdf_pages(pdf: Path) -> list[tuple[float, float, list[Line]]]:
    try:
        xml = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return []
    pages = []
    for pm in _PAGE_RE.finditer(xml):
        w, h, body = float(pm.group(1)), float(pm.group(2)), pm.group(3)
        lines = []
        for lm in _LINE_RE.finditer(body):
            words = _WORD_RE.findall(lm.group(5))
            text = "".join(wd[4] for wd in words)
            lines.append(Line(float(lm.group(1)), float(lm.group(2)), float(lm.group(3)), float(lm.group(4)), text))
        lines.sort(key=lambda l: (round(l.y0, 1), l.x0))
        pages.append((w, h, lines))
    return pages


def pdf_fonts(pdf: Path) -> list[str]:
    try:
        out = subprocess.run(["pdffonts", str(pdf)], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    names = []
    for line in out.splitlines()[2:]:
        if line.strip():
            name = line.split()[0]
            names.append(name.split("+", 1)[-1])
    return sorted(set(names))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def check_rendering(docx: Path, ir: DocumentIR, profile: LayoutProfile, fonts_requested: set[str], out_dir: Path) -> tuple[RenderInfo, list[LayoutCheck]]:
    info = RenderInfo(fonts_requested=sorted(fonts_requested))
    checks: list[LayoutCheck] = []
    tools = tools_available()
    if not tools["soffice"] or not tools["pdftotext"]:
        info.notes.append("环境缺少 LibreOffice 或 poppler-utils：未进行实际渲染核验，版式结论仅基于生成参数")
        return info, checks
    pdf = to_pdf(docx, out_dir)
    if pdf is None:
        info.notes.append("LibreOffice 转换失败：未进行实际渲染核验")
        return info, checks
    info.renderer = libreoffice_version()
    info.rendered = True
    info.pdf_path = str(pdf)
    pages = pdf_pages(pdf)
    info.pages = len(pages)
    # ---- 字体：缺失与替代
    installed = installed_font_families()
    info.fonts_embedded = pdf_fonts(pdf)
    for f in sorted(fonts_requested):
        if f not in installed:
            info.substitutions.append(f"{f}：本机未安装，渲染时被替代（实际嵌入：{'、'.join(info.fonts_embedded) or '未知'}）")
    if not pages:
        info.notes.append("PDF 文本层为空，无法测量版面")
        return info, checks
    w, h, first = pages[0]
    info.page_size_mm = (round(w * MM_PER_PT, 1), round(h * MM_PER_PT, 1))
    pg = profile.data["page"]
    checks.append(
        LayoutCheck(
            rule_id="LAY-PAGE",
            item="幅面尺寸",
            expected=f"{pg['width_mm']}mm×{pg['height_mm']}mm",
            actual=f"{info.page_size_mm[0]}mm×{info.page_size_mm[1]}mm",
            status="pass" if abs(info.page_size_mm[0] - pg["width_mm"]) < 1 and abs(info.page_size_mm[1] - pg["height_mm"]) < 1 else "fail",
            clause="GB/T 9704—2012 5.1",
        )
    )
    m = profile.data["margins"]
    top_area = m["top_mm"]
    bottom_area = top_area + profile.data["type_area"]["height_mm"]
    in_area = [l for l in first if top_area - 3 <= l.y0_mm <= bottom_area + 1]
    # 首页必须显示正文
    body_sents = [s.text for _, s in ir.iter_sentences(include_attachments=False)]
    probe = _norm(body_sents[0])[:6] if body_sents else ""
    page1 = _norm("".join(l.text for l in first))
    info.first_page_has_body = bool(probe) and probe in page1
    checks.append(
        LayoutCheck(
            rule_id="LAY-FIRSTPAGE",
            item="公文首页必须显示正文",
            expected="首页可见正文",
            actual="可见" if info.first_page_has_body else "首页未见正文",
            status="pass" if info.first_page_has_body else "fail",
            clause="GB/T 9704—2012 7.3.3",
            note="" if info.first_page_has_body else "主送机关过多导致首页不能显示正文时，应将主送机关移至版记（7.3.2）",
        )
    )
    # 订口（左白边）：取首页无缩进行（主送机关行、标题外）的最小横坐标
    if in_area:
        left = min(l.x0_mm for l in in_area if len(l.text) > 3) if any(len(l.text) > 3 for l in in_area) else None
        if left is not None:
            ok = abs(left - m["left_mm"]) <= m["left_tol_mm"] + 0.5
            checks.append(LayoutCheck(rule_id="LAY-LEFT", item="订口（左白边）", expected=f"{m['left_mm']}mm±{m['left_tol_mm']}mm", actual=f"{left:.1f}mm（文字左缘实测）", status="pass" if ok else "warn", clause="GB/T 9704—2012 5.2.1", note="以首页最左文字行测量，字形左侧留白会造成少量偏差"))
    # 发文机关标志：上边缘至版心上边缘 35mm（通用格式）
    mark = _norm(ir.header.organ_mark)
    if mark and ir.format_type in ("general", "jiyao"):
        ml = next((l for l in first if mark[:4] and mark[:4] in _norm(l.text)), None)
        if ml is not None:
            dist = ml.y0_mm - top_area
            exp = profile.el("organ_mark")["top_from_type_area_mm"]
            checks.append(LayoutCheck(rule_id="LAY-MARK", item="发文机关标志上边缘至版心上边缘", expected=f"{exp}mm", actual=f"{dist:.1f}mm（字框上缘实测）", status="pass" if abs(dist - exp) <= 3 else "warn", clause="GB/T 9704—2012 7.2.4", note="字形上缘与字框上缘存在差异，误差 3mm 内视为符合"))
    # 页码一字线：上距版心下边缘 7mm；单页码居右
    pn = [l for l in first if re.fullmatch(r"[—\-－]\s*\d+\s*[—\-－]", l.text.replace(" ", ""))]
    if pn:
        d = pn[0].yc_mm - bottom_area
        exp = profile.el("page_number")["dash_offset_mm"]
        checks.append(LayoutCheck(rule_id="LAY-PAGENO", item="页码一字线上距版心下边缘", expected=f"{exp}mm", actual=f"{d:.1f}mm", status="pass" if abs(d - exp) <= 2 else "warn", clause="GB/T 9704—2012 7.5", conditional=True))
        right_ok = pn[0].x1 * MM_PER_PT > (m["left_mm"] + profile.data["type_area"]["width_mm"]) - 12
        checks.append(LayoutCheck(rule_id="LAY-PAGENO-ODD", item="单页码居右空一字", expected="居右", actual="居右" if right_ok else "非居右", status="pass" if right_ok else "fail", clause="GB/T 9704—2012 7.5"))
    else:
        checks.append(LayoutCheck(rule_id="LAY-PAGENO", item="页码", expected="“— 1 —”式页码", actual="首页未检测到页码", status="warn" if ir.format_type != "letter" else "pass", clause="GB/T 9704—2012 7.5、10.1"))
    # 每面行数与每行字数（一般）：选正文最满的一页测量
    best = None
    for _, _, lines in pages:
        body_lines = [l for l in lines if top_area - 1 <= l.y0_mm <= bottom_area and len(l.text) >= 10 and not l.text.startswith("【讨论稿") and not l.text.startswith("【送审稿")]
        if best is None or len(body_lines) > len(best):
            best = body_lines
    if best:
        g = profile.data["grid"]
        rows = len({round(l.y0, 0) for l in best})
        checks.append(LayoutCheck(rule_id="LAY-LINES", item="每面行数", expected=f"一般{g['lines_per_page']}行", actual=f"{rows}行（正文最满一页，含空行时少于{g['lines_per_page']}属正常）", status="pass" if rows <= g["lines_per_page"] else "warn", clause="GB/T 9704—2012 5.2.3", conditional=True))
        widths = []
        for l in best:
            t = l.text
            widths.append(sum(1.0 if ord(c) > 0x2E7F else 0.5 for c in t))
        maxw = max(widths)
        checks.append(LayoutCheck(rule_id="LAY-CHARS", item="每行字数", expected=f"一般{g['chars_per_line']}字", actual=f"最长行约{maxw:.0f}字", status="pass" if maxw <= g["chars_per_line"] + 0.6 else "warn", clause="GB/T 9704—2012 5.2.3", conditional=True))
    # 版记：末条分隔线与最后一面版心下边缘重合（以印发行位置近似）
    if ir.imprint.printer and ir.format_type != "letter":
        _, _, last = pages[-1]
        pl = next((l for l in reversed(last) if ir.imprint.printer[:4] in l.text), None)
        if pl is not None:
            gap = bottom_area - pl.y1 * MM_PER_PT
            checks.append(LayoutCheck(rule_id="LAY-IMPRINT", item="版记位于最后一面版心底部", expected="末条分隔线与版心下边缘重合", actual=f"印发行下缘距版心下边缘 {gap:.1f}mm", status="pass" if gap <= 6 else "warn", clause="GB/T 9704—2012 7.4.1", conditional=True))
    if info.substitutions:
        info.notes.append("存在字体替代：渲染结果仅能核验版面结构，不能据此声称已满足指定字体版式；请在安装规定字库的环境中复核")
    return info, checks
