"""版式流水线：编译 DOCX/HTML/Markdown，回读参数核验，并按配置做实际渲染核验。

供技能11（版式编译与检查）与命令行 gongwen format（对已有文稿排版）共用。
"""

from __future__ import annotations

from pathlib import Path

from ..schemas.common import sha256_bytes
from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutCheck, LayoutReport, OutputFile, RenderInfo
from .docx_checks import check_docx
from .docx_compiler import compile_docx
from .html_preview import standalone_html
from .profile import LayoutProfile
from .render_check import check_rendering


def layout_document(ir: DocumentIR, out_dir: Path, *, profile_id: str = "gbt9704-2012", margin_mode: str = "standard", render_check: bool = True, stem: str | None = None) -> LayoutReport:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = LayoutProfile.load(profile_id, margin_mode)
    stem = stem or f"{ir.doc_id}.v{ir.version}"
    docx_path, fonts = compile_docx(ir, out_dir / f"{stem}.docx", profile)
    html_path = out_dir / f"{stem}.html"
    html_path.write_text(standalone_html(ir), encoding="utf-8")
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(ir.to_markdown(), encoding="utf-8")
    first_body = next((s.text for _, s in ir.iter_sentences(include_attachments=False)), "")
    checks: list[LayoutCheck] = check_docx(docx_path, profile, ir.title, first_body)
    if render_check:
        render, rchecks = check_rendering(docx_path, ir, profile, fonts, out_dir)
        checks += rchecks
    else:
        render = RenderInfo(fonts_requested=sorted(fonts), notes=["配置关闭了实际渲染核验"])
    for f in sorted(fonts):
        substituted = any(s.startswith(f) for s in render.substitutions)
        checks.append(
            LayoutCheck(
                rule_id="FONT",
                item=f"字体：{f}",
                expected="定稿环境已安装该字库（字库名属实务，国标规定的是字体类别）",
                actual="已替代" if substituted else ("已安装" if render.rendered else "未核验"),
                status="warn" if substituted else ("pass" if render.rendered else "unverified"),
                clause="GB/T 9704—2012 5.2.2",
                level="实务",
                conditional=True,
            )
        )
    outputs = [OutputFile(kind=k, path=str(p), sha256=sha256_bytes(p.read_bytes())) for k, p in (("docx", docx_path), ("html", html_path), ("md", md_path))]
    if render.pdf_path:
        pp = Path(render.pdf_path)
        outputs.append(OutputFile(kind="pdf", path=str(pp), sha256=sha256_bytes(pp.read_bytes())))
    return LayoutReport(profile=profile.id, profile_version=profile.version, checks=checks, render=render, outputs=outputs)


__all__ = ["layout_document"]
