"""实际渲染核验：DOCX → PDF（LibreOffice）后测量版面，而不是只检查生成参数。

* 测量项：页数与幅面、首页是否显示正文（7.3.3）、发文机关标志位置（7.2.4；命令（令）格式 10.2、
  纪要格式 10.3，简报报头为实务）、订口（5.2.1）、页码一字线位置（7.5）、每面行数与每行字数（5.2.3，一般）；
* 信函格式（10.1）：发文机关标志距上页边、两条红色双线的位置、粗细次序与线长、发文字号位置、首页不显示页码；
  红色线条不在文本层中，将页面栅格化后按红色像素测量；
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


@dataclass
class Band:
    """栅格化页面上连续含红色像素的一段像素行（mm）。"""

    y0: float
    y1: float
    x0: float
    x1: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def tools_available() -> dict[str, str | None]:
    return {k: shutil.which(k) for k in ("soffice", "pdfinfo", "pdffonts", "pdftotext", "pdftoppm", "fc-list")}


_HI = bytes(1 if i >= 0xC8 else 0 for i in range(256))
_LO = bytes(1 if i <= 0x50 else 0 for i in range(256))


def red_bands(pdf: Path, page: int = 1, dpi: int = 200) -> list[Band] | None:
    """把一面栅格化（不抗锯齿），找出含红色像素的连续像素行，返回各段的上下缘与左右端（mm）。

    pdftotext 不输出线条，红色分隔线、双线以及红色标志的字形上下缘都以此测量。缺少 pdftoppm 或转换失败时返回 None。"""
    if not shutil.which("pdftoppm"):
        return None
    try:
        raw = subprocess.run(["pdftoppm", "-r", str(dpi), "-f", str(page), "-l", str(page), "-aa", "no", "-aaVector", "no", str(pdf)], capture_output=True, timeout=60).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\s", raw)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    stride, px = w * 3, 25.4 / dpi
    bands: list[Band] = []
    last_y = -2
    for y in range(h):
        row = raw[m.end() + y * stride : m.end() + (y + 1) * stride]
        r = row[0::3].translate(_HI)
        if 1 not in r:
            continue
        # 红色像素：R ≥ 200 且 G、B ≤ 80（逐像素按位与，避免逐像素的 Python 循环）
        hit = (int.from_bytes(r, "big") & int.from_bytes(row[1::3].translate(_LO), "big") & int.from_bytes(row[2::3].translate(_LO), "big")).to_bytes(w, "big")
        x0, x1 = hit.find(1), hit.rfind(1)
        if x0 < 0:
            continue
        if bands and y == last_y + 1:
            b = bands[-1]
            b.y1, b.x0, b.x1 = (y + 1) * px, min(b.x0, x0 * px), max(b.x1, (x1 + 1) * px)
        else:
            bands.append(Band(y * px, (y + 1) * px, x0 * px, (x1 + 1) * px))
        last_y = y
    return bands


def double_lines(bands: list[Band], min_len_mm: float = 100, max_gap_mm: float = 2) -> list[tuple[Band, Band]]:
    """把细长的红色像素段两两配成双线（两线间隔不超过 max_gap_mm）。"""
    lines = [b for b in bands if b.height <= 2 and b.x1 - b.x0 >= min_len_mm]
    out = []
    i = 0
    while i + 1 < len(lines):
        if lines[i + 1].y0 - lines[i].y1 <= max_gap_mm:
            out.append((lines[i], lines[i + 1]))
            i += 2
        else:
            i += 1
    return out


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


def _full_page_rows(pages: list[tuple[float, float, list[Line]]], top_mm: float, bottom_mm: float, pitch_mm: float) -> list[tuple[int, int]]:
    """已排满的续页各排了几行：首行在版心第一行格、各行都落在同一行格上且空行不多（含表格、大字号标题的页面不计），
    且按实测行距版心内已排不下下一行。末页（版记所在页）不计。返回 (页码, 行数)。"""
    out = []
    for n, (_, _, lines) in enumerate(pages[:-1], 1):
        ys = sorted({round(l.y0_mm, 2) for l in lines if top_mm - 1 <= l.y0_mm <= bottom_mm and l.text.strip()})
        if len(ys) < 3 or ys[0] - top_mm > pitch_mm:  # 首行不在版心第一行格：不是续页
            continue
        steps = [(y - ys[0]) / pitch_mm for y in ys]
        if any(abs(s - round(s)) > 0.15 for s in steps):
            continue
        k = round(steps[-1]) + 1
        if k < 3 or len(ys) < k - 4:
            continue
        pitch = (ys[-1] - ys[0]) / (k - 1)
        if bottom_mm - (top_mm + k * pitch) < pitch:
            out.append((n, k))
    return out


def _letter_checks(pdf: Path, first: list[Line], ir: DocumentIR, profile: LayoutProfile) -> list[LayoutCheck]:
    """信函格式首页（GB/T 9704—2012 10.1）：标志距上页边 30mm；标志下 4mm 处上粗下细的红色双线、距下页边 20mm 处
    上细下粗的红色双线，线长均为 170mm 并居中；发文字号顶格居版心右边缘，距第一条双线 7/8 个 3 号字高。"""
    lt, out = profile.data["letter"], []
    clause = "GB/T 9704—2012 10.1"
    mark = _norm(ir.header.organ_mark)
    ml = next((l for l in first if mark[:4] and mark[:4] in _norm(l.text)), None)
    if ml is not None:
        exp = lt["organ_mark_top_from_page_mm"]
        out.append(LayoutCheck(rule_id="LAY-LETTER-MARK", item="信函格式发文机关标志上边缘至上页边", expected=f"{exp}mm", actual=f"{ml.y0_mm:.1f}mm（字框上缘实测）", status="pass" if abs(ml.y0_mm - exp) <= 3 else "warn", clause=clause, note="字形上缘与字框上缘存在差异，误差 3mm 内视为符合"))
    bands = red_bands(pdf)
    if bands is None:
        out.append(LayoutCheck(rule_id="LAY-LETTER-RULES", item="信函格式红色双线", expected="标志下 4mm、距下页边 20mm 各一条，长 170mm", actual="缺少 pdftoppm，未测量", status="unverified", clause=clause))
        return out
    page_h = profile.data["page"]["height_mm"]
    pairs = double_lines(bands)
    top = next(iter(pairs), None)
    bottom = next((p for p in reversed(pairs) if p[1].y1 > page_h - 40), None)
    if top is None or bottom is None or top is bottom:
        out.append(LayoutCheck(rule_id="LAY-LETTER-RULES", item="信函格式红色双线", expected="标志下 4mm、距下页边 20mm 各一条，长 170mm", actual=f"首页检测到 {len(pairs)} 条红色双线", status="fail", clause=clause))
        return out
    centre = profile.data["margins"]["left_mm"] + profile.data["type_area"]["width_mm"] / 2
    problems, facts = [], []
    # 标志的红色字形在第一条双线之上：取其下缘计算距离（字形实测，不受字库字框大小影响）
    ink = [b for b in bands if b.y1 <= top[0].y0 and b.height > 2]
    if ink:
        gap = top[0].y0 - ink[-1].y1
        facts.append(f"第一条距标志 {gap:.1f}mm")
        if abs(gap - lt["rule_below_mark_mm"]) > 1.5:
            problems.append(f"第一条应在标志下 {lt['rule_below_mark_mm']}mm")
    dist = page_h - bottom[1].y1
    facts.append(f"第二条距下页边 {dist:.1f}mm")
    if abs(dist - lt["bottom_rule_from_page_mm"]) > 1:
        problems.append(f"第二条应距下页边 {lt['bottom_rule_from_page_mm']}mm")
    for name, (a, b), want in (("第一条", top, "上粗下细"), ("第二条", bottom, "上细下粗")):
        order = "上粗下细" if a.height > b.height * 1.5 else "上细下粗" if b.height > a.height * 1.5 else "粗细相同"
        length = max(a.x1, b.x1) - min(a.x0, b.x0)
        mid = (max(a.x1, b.x1) + min(a.x0, b.x0)) / 2
        facts.append(f"{name}{order}、长 {length:.0f}mm")
        if order != want:
            problems.append(f"{name}应{want}")
        if abs(length - lt["rule_length_mm"]) > 2 or abs(mid - centre) > 2:
            problems.append(f"{name}应长 {lt['rule_length_mm']}mm 并居中")
    out.append(LayoutCheck(rule_id="LAY-LETTER-RULES", item="信函格式红色双线", expected=f"标志下 {lt['rule_below_mark_mm']}mm 上粗下细、距下页边 {lt['bottom_rule_from_page_mm']}mm 上细下粗，长 {lt['rule_length_mm']}mm 居中", actual="；".join(facts), status="fail" if problems else "pass", clause=clause, note="；".join(problems)))
    # 发文字号：顶格居版心右边缘，字框上缘距第一条双线 7/8 个 3 号字高
    dn = ir.header.doc_number
    shown = _norm("【待编号】" if dn.startswith("【待") else dn)
    dl = next((l for l in first if _norm(l.text).endswith(shown) and l.y0_mm > top[1].y1), None) if shown else None
    if dl is not None:
        exp = lt["element_gap_ratio"] * profile.body_pt * MM_PER_PT
        gap = dl.y0_mm - top[1].y1
        right = profile.data["margins"]["left_mm"] + profile.data["type_area"]["width_mm"]
        ok = abs(gap - exp) <= 1.5 and abs(dl.x1 * MM_PER_PT - right) <= 1.5
        out.append(LayoutCheck(rule_id="LAY-LETTER-DOCNO", item="信函格式发文字号位置", expected=f"顶格居版心右边缘，距第一条双线 {exp:.1f}mm（3 号字高的 7/8）", actual=f"距双线 {gap:.1f}mm（字框上缘实测），右端距版心右边缘 {round(right - dl.x1 * MM_PER_PT, 1) + 0.0}mm", status="pass" if ok else "warn", clause=clause))
    return out


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
    # 发文机关标志：上边缘至版心上边缘 35mm（通用格式 7.2.4）、20mm（命令（令）格式 10.2）；纪要标志按纪要格式，
    # 简报名称按实务。信函格式的标志在版心之上，另行测量（见 _letter_checks）；事务文书不设版头，不测
    mark = _norm(ir.header.organ_mark)
    mark_exp = {
        "general": (profile.el("organ_mark")["top_from_type_area_mm"], "发文机关标志", "GB/T 9704—2012 7.2.4", "国标"),
        "command": (profile.data["command"]["organ_mark_top_from_type_area_mm"], "发文机关标志", "GB/T 9704—2012 10.2", "国标"),
        "jiyao": (profile.data["jiyao"]["mark_top_from_type_area_mm"], "纪要标志", "GB/T 9704—2012 10.3", "国标"),
        "brief": (profile.data["brief"]["mark_top_from_type_area_mm"], "简报名称", "简报报头（GB/T 9704—2012 未规定）", "实务"),
    }.get(ir.format_type)
    if mark and mark_exp:
        exp, what, clause, level = mark_exp
        ml = next((l for l in first if mark[:4] and mark[:4] in _norm(l.text)), None)
        if ml is not None:
            dist = ml.y0_mm - top_area
            checks.append(LayoutCheck(rule_id="LAY-MARK", item=f"{what}上边缘至版心上边缘", expected=f"{exp}mm", actual=f"{dist:.1f}mm（字框上缘实测）", status="pass" if abs(dist - exp) <= 3 else "warn", clause=clause, level=level, conditional=ir.format_type in ("jiyao", "brief"), note="字形上缘与字框上缘存在差异，误差 3mm 内视为符合"))
    if ir.format_type == "letter":
        checks += _letter_checks(pdf, first, ir, profile)
    # 页码一字线：上距版心下边缘 7mm；单页码居右
    pn = [l for l in first if re.fullmatch(r"[—\-－]\s*\d+\s*[—\-－]", l.text.replace(" ", ""))]
    if pn and ir.format_type == "letter" and not profile.data["letter"].get("first_page_number", True):
        checks.append(LayoutCheck(rule_id="LAY-PAGENO", item="信函格式首页不显示页码", expected="首页无页码", actual=f"首页显示页码“{pn[0].text}”", status="fail", clause="GB/T 9704—2012 10.1"))
    elif pn:
        d = pn[0].yc_mm - bottom_area
        exp = profile.el("page_number")["dash_offset_mm"]
        checks.append(LayoutCheck(rule_id="LAY-PAGENO", item="页码一字线上距版心下边缘", expected=f"{exp}mm", actual=f"{d:.1f}mm", status="pass" if abs(d - exp) <= 2 else "warn", clause="GB/T 9704—2012 7.5", conditional=True))
        right_ok = pn[0].x1 * MM_PER_PT > (m["left_mm"] + profile.data["type_area"]["width_mm"]) - 12
        checks.append(LayoutCheck(rule_id="LAY-PAGENO-ODD", item="单页码居右空一字", expected="居右", actual="居右" if right_ok else "非居右", status="pass" if right_ok else "fail", clause="GB/T 9704—2012 7.5"))
    elif ir.format_type == "letter":
        checks.append(LayoutCheck(rule_id="LAY-PAGENO", item="信函格式首页不显示页码", expected="首页无页码", actual="首页无页码", status="pass", clause="GB/T 9704—2012 10.1"))
    else:
        checks.append(LayoutCheck(rule_id="LAY-PAGENO", item="页码", expected="“— 1 —”式页码", actual="首页未检测到页码", status="warn", clause="GB/T 9704—2012 7.5"))
    # 每面行数与每行字数（一般）：选正文最满的一页测量
    best = None
    for _, _, lines in pages:
        body_lines = [l for l in lines if top_area - 1 <= l.y0_mm <= bottom_area and len(l.text) >= 10 and not l.text.startswith("【讨论稿") and not l.text.startswith("【送审稿")]
        if best is None or len(body_lines) > len(best):
            best = body_lines
    if best:
        g = profile.data["grid"]
        rows = len({round(l.y0, 0) for l in best})
        # 已排满的续页却不足 22 行：行距偏大（如 580 缇×22 行超出 225mm 版心，每面只能排 21 行）
        short = [(n, k) for n, k in _full_page_rows(pages, top_area, bottom_area, profile.line_pt * MM_PER_PT) if k < g["lines_per_page"]]
        actual = f"{rows}行（正文最满一页，含空行时少于{g['lines_per_page']}属正常）"
        if short:
            actual += "；排满的页面只有" + "、".join(f"第{n}面{k}行" for n, k in short[:5])
        checks.append(LayoutCheck(rule_id="LAY-LINES", item="每面行数", expected=f"一般{g['lines_per_page']}行", actual=actual, status="pass" if rows <= g["lines_per_page"] and not short else "warn", clause="GB/T 9704—2012 5.2.3", conditional=True, note="排满的页面不足规定行数，多为行距偏大" if short else ""))
        widths = []
        for l in best:
            t = l.text
            widths.append(sum(1.0 if ord(c) > 0x2E7F else 0.5 for c in t))
        maxw = max(widths)
        checks.append(LayoutCheck(rule_id="LAY-CHARS", item="每行字数", expected=f"一般{g['chars_per_line']}字", actual=f"最长行约{maxw:.0f}字", status="pass" if maxw <= g["chars_per_line"] + 0.6 else "warn", clause="GB/T 9704—2012 5.2.3", conditional=True))
    # 署名页须有正文：署名、成文日期不能单独成页（7.3.5.5：容不下时调整行距、字距解决）
    date_text = _norm(ir.signature.date or "")
    organ_text = _norm(ir.signature.organs[0]) if ir.signature.organs else ""
    if ir.format_type == "jiyao" and ir.attendees:
        # 纪要以出席名单收尾：名单所在页同样须有正文
        key = next(k for k in ("出席", "请假", "列席") if ir.attendees.get(k))
        date_text, organ_text = "", _norm(f"{key}：{'、'.join(ir.attendees[key])}")[:8]
        att_page = next((i for i, (_, _, ls) in enumerate(pages) if any(_norm(l.text).startswith(organ_text) for l in ls)), None)
        if att_page is not None:
            _, _, ls = pages[att_page]
            first = next(l for l in ls if _norm(l.text).startswith(organ_text))
            body_all = _norm("".join(b.text() for b in ir.blocks))
            has_body = att_page == 0 or any(len(_norm(l.text)) >= 4 and _norm(l.text) in body_all for l in ls if l.y0 < first.y0)
            checks.append(LayoutCheck(rule_id="LAY-SIGNATURE", item="出席名单与正文同页", expected="名单所在页有正文，不单独成页", actual=f"第{att_page + 1}面" + ("有正文" if has_body else "只有出席名单"), status="pass" if has_body else "fail", clause="GB/T 9704—2012 10.3、7.3.5.5（参照）", note="" if has_body else "出席名单被挤到下一面：应调整行距，或使正文末段与名单同页"))
    if date_text and ir.format_type != "jiyao":
        body_all = _norm("".join(b.text() for b in ir.blocks))
        sig_page = next((i for i, (_, _, ls) in enumerate(pages) if any(_norm(l.text) == date_text for l in ls)), None)
        if sig_page is not None:
            _, _, ls = pages[sig_page]
            if ir.signature.seal_mode == "signature_stamp":
                # 加盖签名章（7.3.5.3）：署名行为“签发人职务　【签名章】”，不是发文机关名称
                title = _norm(ir.signature.signer_title)
                organ_line = next((l for l in ls if "签名章" in l.text or (title and _norm(l.text) == title)), None)
            else:
                organ_line = next((l for l in ls if organ_text and _norm(l.text) == organ_text), None)
            above = [l for l in ls if organ_line is None or l.y0 < organ_line.y0]
            has_body = sig_page == 0 or any(len(_norm(l.text)) >= 4 and _norm(l.text) in body_all for l in above)
            checks.append(
                LayoutCheck(
                    rule_id="LAY-SIGNATURE",
                    item="署名、成文日期与正文同页",
                    expected="署名页有正文，不单独成页",
                    actual=f"第{sig_page + 1}面" + ("有正文" if has_body else "只有附件说明或署名"),
                    status="pass" if has_body else "fail",
                    clause="GB/T 9704—2012 7.3.5.5",
                    note="" if has_body else "署名与成文日期被挤到下一面：应调整行距、字距，或使正文末段与署名同页",
                )
            )
    # 版记：末条分隔线与最后一面版心下边缘重合（以印发行位置近似）
    if ir.imprint.printer and ir.format_type not in ("letter", "plain", "brief"):  # 信函版记不加印发机关；事务文书、简报不设版记
        _, _, last = pages[-1]
        pl = next((l for l in reversed(last) if ir.imprint.printer[:4] in l.text), None)
        if pl is not None:
            gap = bottom_area - pl.y1 * MM_PER_PT
            # 末条分隔线应与版心下边缘重合：负值表示版记越出版心（过长的浮动版记会越出页面），过大表示未排到底部
            ok = -1 <= gap <= 6
            note = "" if ok else ("版记越出版心下边缘：应精简主送、抄送" if gap < -1 else "版记未排到最后一面版心底部（版记高于一面版心时只能紧接正文排列）")
            checks.append(LayoutCheck(rule_id="LAY-IMPRINT", item="版记位于最后一面版心底部", expected="末条分隔线与版心下边缘重合（印发行下缘距版心下边缘 −1～6mm）", actual=f"印发行下缘距版心下边缘 {gap:.1f}mm", status="pass" if ok else "fail", clause="GB/T 9704—2012 7.4.1", note=note))
        else:
            checks.append(LayoutCheck(rule_id="LAY-IMPRINT", item="版记位于最后一面版心底部", expected="末条分隔线与版心下边缘重合", actual="最后一面未检测到印发行", status="warn", clause="GB/T 9704—2012 7.4.1", note="版记可能越出页面或被拆到其他页面，请人工查看"))
    if info.substitutions:
        info.notes.append("存在字体替代：渲染结果仅能核验版面结构，不能据此声称已满足指定字体版式；请在安装规定字库的环境中复核")
    return info, checks
