"""版式流水线：编译 DOCX/HTML/Markdown，回读参数核验，并按配置做实际渲染核验。

供技能11（版式编译与检查）与命令行 gongwen format / preview（对已有文稿排版）共用。
按公文模板排版时传入模板的生效参数（templates.resolve_profile）：模板的单位信息补全空缺要素，
核验以模板参数为准，模板对国标的偏离列为 TEMPLATE、TPL-DEV 核验项。
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
from .templates import apply_unit, template_checks


def layout_document(
    ir: DocumentIR,
    out_dir: Path,
    *,
    profile_id: str = "gbt9704-2012",
    margin_mode: str = "standard",
    render_check: bool = True,
    stem: str | None = None,
    profile: LayoutProfile | None = None,
    font_substitution: bool = True,
) -> LayoutReport:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = profile or LayoutProfile.load(profile_id, margin_mode)
    stem = stem or f"{ir.doc_id}.v{ir.version}"
    ir, filled = apply_unit(ir, profile.unit)
    docx_path, fonts = compile_docx(ir, out_dir / f"{stem}.docx", profile)
    html_path = out_dir / f"{stem}.html"
    html_path.write_text(standalone_html(ir), encoding="utf-8")
    md_path = out_dir / f"{stem}.md"
    md_path.write_text(ir.to_markdown(), encoding="utf-8")
    first_body = next((s.text for _, s in ir.iter_sentences(include_attachments=False)), "")
    checks: list[LayoutCheck] = check_docx(docx_path, profile, ir.title, first_body)
    if render_check:
        render, rchecks = check_rendering(docx_path, ir, profile, fonts, out_dir, font_substitution=font_substitution)
        tight = 0
        while tight < 2 and any(c.rule_id == "LAY-SIGNATURE" and c.status == "fail" for c in rchecks):
            # 署名、成文日期被挤到没有正文的下一面：按 7.3.5.5 调整空行行距，仍不行则使正文末段与署名同页
            tight += 1
            docx_path, fonts = compile_docx(ir, out_dir / f"{stem}.docx", profile, tight=tight)
            render, rchecks = check_rendering(docx_path, ir, profile, fonts, out_dir, font_substitution=font_substitution)
            render.notes.append(("出席名单" if ir.format_type == "jiyao" else "署名") + "所在页无正文：已按 GB/T 9704—2012 7.3.5.5 " + ("缩小正文与署名之间的空行行距" if tight == 1 else "缩小空行行距并使正文末段与署名同页"))
        checks = check_docx(docx_path, profile, ir.title, first_body) + rchecks
    else:
        render = RenderInfo(fonts_requested=sorted(fonts), notes=["配置关闭了实际渲染核验"])
    if filled:
        render.notes.append(f"按模板“{profile.template}”的单位信息填入：{'、'.join(filled)}（文稿原无此内容，请核对）")
    for f in sorted(fonts):
        sub = next((s for s in render.substitutions if s.startswith(f + "：")), None)
        checks.append(
            LayoutCheck(
                rule_id="FONT",
                item=f"字体：{f}",
                expected="定稿环境已安装该字库（字库名属实务，国标规定的是字体类别）",
                actual=sub.split("：", 1)[1] if sub else ("已安装" if render.rendered else "未核验"),
                status="warn" if sub else ("pass" if render.rendered else "unverified"),
                clause="GB/T 9704—2012 5.2.2",
                level="实务",
                conditional=True,
            )
        )
    checks += template_checks(profile)
    outputs = [OutputFile(kind=k, path=str(p), sha256=sha256_bytes(p.read_bytes())) for k, p in (("docx", docx_path), ("html", html_path), ("md", md_path))]
    if render.pdf_path:
        pp = Path(render.pdf_path)
        outputs.append(OutputFile(kind="pdf", path=str(pp), sha256=sha256_bytes(pp.read_bytes())))
    return LayoutReport(
        profile=profile.id,
        profile_version=profile.version,
        checks=checks,
        render=render,
        outputs=outputs,
        template=profile.template,
        deviations=[c.describe() for c in profile.changes if c.deviates],
    )


__all__ = ["layout_document"]
