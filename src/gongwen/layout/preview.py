"""渲染预览：排版（版式流水线）→ LibreOffice 渲染 PDF → pdftoppm 栅格化为页面图像 → preview.html。

preview.html 自包含（页面图像以 data URI 内嵌），并排显示各页，附核验结果（通过／提示／不符合／未核验）、模板偏离与字体替代。
本机没有 LibreOffice 或 poppler 时退回 HTML 近似预览，并在页面上写明“未实际渲染，不代表实际版面”。
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutReport
from . import fonts as gwfonts
from .html_preview import CSS as DOC_CSS
from .html_preview import render_document
from .pipeline import layout_document
from .profile import LayoutProfile
from .render_check import to_pdf, tools_available

PAGE_RE = re.compile(r"^page-(\d{1,3})\.png$")
STATUS_LABELS = {"pass": "通过", "warn": "提示", "fail": "不符合", "unverified": "未核验", "na": "不适用"}


@dataclass
class PreviewResult:
    out_dir: Path
    html: Path
    pages: list[Path] = field(default_factory=list)
    report: LayoutReport | None = None
    pdf: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def rendered(self) -> bool:
        return bool(self.pages)

    def to_dict(self) -> dict:
        return {
            "html": str(self.html),
            "pages": [str(p) for p in self.pages],
            "pdf": str(self.pdf) if self.pdf else None,
            "rendered": self.rendered,
            "notes": self.notes,
            "template": self.report.template if self.report else "",
            "deviations": self.report.deviations if self.report else [],
            "checks": [c.model_dump() for c in self.report.checks] if self.report else [],
            "substitutions": self.report.render.substitutions if self.report else [],
        }


def can_render() -> bool:
    t = tools_available()
    return bool(t["soffice"] and t["pdftoppm"])


def rasterize(pdf: Path, pages_dir: Path, dpi: int = 80) -> list[Path]:
    """PDF 各页 → pages_dir/page-N.png（N 从 1 起，不补零）。"""
    pages_dir.mkdir(parents=True, exist_ok=True)
    for old in pages_dir.glob("page-*.png"):
        old.unlink()
    try:
        subprocess.run(["pdftoppm", "-r", str(dpi), "-png", str(pdf), str(pages_dir / "page")], capture_output=True, timeout=300, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return []
    out = []
    for p in pages_dir.glob("page-*.png"):
        m = re.match(r"^page-0*(\d+)\.png$", p.name)
        if not m:
            continue
        target = pages_dir / f"page-{int(m.group(1))}.png"
        if target != p:
            p.replace(target)
        out.append(target)
    return sorted(out, key=lambda p: int(PAGE_RE.match(p.name).group(1)))


def _data_uri(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")


PREVIEW_CSS = """
:root{--bg:#eceff1;--panel:#fff;--ink:#1f2328;--muted:#5f6b7a;--line:#d8dde3;--ok:#2e7d32;--warn:#b26a00;--bad:#c62828;--un:#5f6b7a}
@media (prefers-color-scheme: dark){:root{--bg:#15181c;--panel:#1d2126;--ink:#e6e8eb;--muted:#9aa4af;--line:#2d333b;--ok:#81c784;--warn:#ffb74d;--bad:#ef9a9a;--un:#9aa4af}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:12px 16px}
header h1{font-size:17px;margin:0 0 4px}
.meta{color:var(--muted);font-size:12px}
.banner{margin:12px 16px;padding:10px 12px;border-radius:6px;border:1px solid var(--warn);color:var(--warn);background:var(--panel)}
.pages{display:flex;flex-wrap:wrap;gap:16px;padding:16px;justify-content:center}
figure{margin:0;background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:6px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
figure img{display:block;width:420px;max-width:calc(100vw - 48px);height:auto;background:#fff}
figcaption{text-align:center;color:var(--muted);font-size:12px;margin-top:4px}
section{background:var(--panel);border:1px solid var(--line);border-radius:6px;margin:0 16px 16px;padding:10px 14px;overflow-x:auto}
section h2{font-size:15px;margin:4px 0 8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}
.st{display:inline-block;min-width:3.5em;text-align:center;border-radius:4px;padding:0 6px;font-size:12px;border:1px solid currentColor}
.st.pass{color:var(--ok)} .st.warn{color:var(--warn)} .st.fail{color:var(--bad)} .st.unverified,.st.na{color:var(--un)}
ul{margin:4px 0;padding-left:20px}
.counts span{margin-right:12px}
.doc{padding:16px}
"""


def build_html(title: str, report: LayoutReport | None, pages: list[Path], *, img_src: Callable[[int, Path], str] | None = None, notes: list[str] | None = None, fallback_doc: str = "") -> str:
    """预览页面。img_src(序号, 文件) 给出图像地址；缺省时内嵌为 data URI（自包含）。"""
    img_src = img_src or (lambda i, p: _data_uri(p))
    e = html.escape
    notes = list(notes or [])
    checks = report.checks if report else []
    counts = {k: sum(1 for c in checks if c.status == k) for k in ("pass", "warn", "fail", "unverified")}
    render = report.render if report else None
    meta = []
    if report:
        meta.append(f"版式：{e(report.profile)}" + (f"｜模板：{e(report.template)}（偏离国标 {len(report.deviations)} 项）" if report.template else "｜未用模板（国标默认参数）"))
    if render and render.rendered:
        meta.append(f"已实际渲染：{e(render.renderer)}，共 {render.pages} 面")
    parts = [
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}｜排版预览</title><style>{PREVIEW_CSS}{DOC_CSS}</style></head><body>',
        f'<header><h1>{e(title)}｜排版预览</h1><div class="meta">{"　".join(meta)}</div>',
        '<div class="meta counts">' + "".join(f'<span><span class="st {k}">{STATUS_LABELS[k]}</span> {v}</span>' for k, v in counts.items()) + "</div></header>",
    ]
    if not pages:
        parts.append('<div class="banner">未实际渲染（本机缺少 LibreOffice 或 poppler-utils，或渲染失败）：以下为 HTML 近似预览，字体、行距与分页均不代表实际版面；版式核验只基于生成参数。</div>')
    if render and render.substitutions:
        parts.append('<div class="banner">存在字体替代：页面图像中的字体是替代字体，仅供预览；定稿须在安装指定字库的环境中复核。</div>')
    if pages:
        parts.append('<div class="pages">' + "".join(f'<figure><img alt="第 {i} 面" src="{e(img_src(i, p))}"><figcaption>第 {i} 面</figcaption></figure>' for i, p in enumerate(pages, 1)) + "</div>")
    elif fallback_doc:
        parts.append(f'<div class="doc">{fallback_doc}</div>')
    if report and report.deviations:
        parts.append("<section><h2>模板偏离 GB/T 9704—2012</h2><ul>" + "".join(f"<li>{e(d)}</li>" for d in report.deviations) + "</ul></section>")
    if render and render.substitutions:
        parts.append("<section><h2>字体替代</h2><ul>" + "".join(f"<li>{e(s)}</li>" for s in render.substitutions) + "</ul></section>")
    if checks:
        rows = "".join(
            f'<tr><td><span class="st {e(c.status)}">{e(STATUS_LABELS.get(c.status, c.status))}</span></td><td>{e(c.item)}</td><td>{e(c.expected)}</td><td>{e(c.actual)}</td><td>{e(c.clause)}{"（条件性）" if c.conditional else ""}{("<br>" + e(c.note)) if c.note else ""}</td></tr>'
            for c in sorted(checks, key=lambda c: {"fail": 0, "warn": 1, "unverified": 2}.get(c.status, 3))
        )
        parts.append(f"<section><h2>核验结果</h2><table><thead><tr><th>状态</th><th>项目</th><th>要求</th><th>实际</th><th>依据</th></tr></thead><tbody>{rows}</tbody></table></section>")
    notes += list(render.notes) if render else []
    if notes:
        parts.append("<section><h2>说明</h2><ul>" + "".join(f"<li>{e(n)}</li>" for n in notes) + "</ul></section>")
    parts.append("</body></html>")
    return "".join(parts)


def preview_ir(ir: DocumentIR, out_dir: Path, profile: LayoutProfile, *, font_substitution: bool = True, stem: str = "preview", title: str = "", dpi: int = 80) -> PreviewResult:
    """排版并渲染预览（输出 DOCX、PDF、页面图像与 preview.html）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = layout_document(ir, out_dir, profile=profile, render_check=True, stem=stem, font_substitution=font_substitution)
    pdf = Path(report.render.pdf_path) if report.render.pdf_path else None
    pages = rasterize(pdf, out_dir / "pages", dpi) if pdf and shutil.which("pdftoppm") else []
    notes = []
    if not pages:
        notes.append("LibreOffice 渲染失败：未生成页面图像，显示 HTML 近似预览" if can_render() else "本机缺少 LibreOffice 或 pdftoppm（poppler-utils）：未生成页面图像，显示 HTML 近似预览")
    res = PreviewResult(out_dir=out_dir, html=out_dir / "preview.html", pages=pages, report=report, pdf=pdf, notes=notes)
    fallback = "" if pages else render_document(ir, label="HTML 近似预览（未实际渲染）")
    res.html.write_text(build_html(title or ir.title or stem, report, pages, notes=notes, fallback_doc=fallback), encoding="utf-8")
    return res


def preview_docx(docx: Path, out_dir: Path, report: LayoutReport | None, profile: LayoutProfile, *, ir: DocumentIR | None = None, font_substitution: bool = True, title: str = "", dpi: int = 80) -> PreviewResult:
    """渲染已有的 DOCX（如任务的当前排版稿，不重新排版），附上其排版报告。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    pdf = None
    notes: list[str] = []
    rendered_pdf = Path(report.render.pdf_path) if report and report.render.pdf_path else None
    fresh = bool(rendered_pdf and rendered_pdf.is_file() and rendered_pdf.stat().st_mtime >= Path(docx).stat().st_mtime)
    if fresh and report.render.font_env != gwfonts.env_fingerprint(profile.data.get("fonts"), enabled=font_substitution):
        # 渲染之后字体环境已变（安装了字库、开关了替代映射，或早于字体指纹的旧排版结果）：重新渲染，页面以当前环境为准
        fresh = False
        notes.append("排版时的字体环境与当前不同，已按当前字体环境重新渲染；排版核验结果仍为排版时的结论")
    if fresh and shutil.which("pdftoppm"):
        # 排版检查时已渲染过同一份 DOCX：直接栅格化该 PDF，页面与核验结果一致
        pdf = rendered_pdf
        pages = rasterize(pdf, out_dir / "pages", dpi)
    elif can_render():
        with gwfonts.render_env(profile.data.get("fonts"), enabled=font_substitution) as env:
            pdf = to_pdf(Path(docx), out_dir, env_extra=env)
        if pdf is not None:
            pages = rasterize(pdf, out_dir / "pages", dpi)
        else:
            notes.append("LibreOffice 转换失败：未生成页面图像")
    else:
        notes.append("本机缺少 LibreOffice 或 pdftoppm（poppler-utils）：未生成页面图像，显示 HTML 近似预览")
    res = PreviewResult(out_dir=out_dir, html=out_dir / "preview.html", pages=pages, report=report, pdf=pdf, notes=notes)
    fallback = render_document(ir, label="HTML 近似预览（未实际渲染）") if (ir is not None and not pages) else ""
    res.html.write_text(build_html(title or Path(docx).stem, report, pages, notes=notes, fallback_doc=fallback), encoding="utf-8")
    return res


def file_digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_manifest(res: PreviewResult, extra: dict | None = None) -> Path:
    p = res.out_dir / "manifest.json"
    p.write_text(json.dumps({**res.to_dict(), **(extra or {})}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


__all__ = ["PreviewResult", "build_html", "can_render", "preview_docx", "preview_ir", "rasterize", "write_manifest"]
