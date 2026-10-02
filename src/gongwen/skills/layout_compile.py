"""技能11 版式编译与检查：使用适用模板排版并核验输出。

* 生成参数核验（回读 DOCX）与实际渲染核验（LibreOffice → PDF 测量）分开报告；
* 版式规则保留标准中的条件性（“一般”“推荐”“可以”只提示）；
* 字体不可用或发生替代时明确报告，不声称已完全满足指定版式；
* 成文日期、发文字号和签发信息不用生成时间或模型猜测填充。
"""

from __future__ import annotations

from pathlib import Path

from ..layout import LayoutProfile, check_docx, check_rendering, compile_docx, standalone_html
from ..schemas.common import sha256_bytes
from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutCheck, LayoutReport, OutputFile, RenderInfo
from ..schemas.state import Stage
from .base import Skill, SkillContext


class LayoutCompileSkill(Skill):
    name = "gongwen-layout-compile"
    number = 11
    title = "版式编译与检查"
    stage = Stage.LAYOUT
    channel_name = "layout"
    allowed_tools = ("gongwen_export",)
    output_artifact = "layout_report"

    def run(self, sc: SkillContext, ir: DocumentIR, out_dir: Path) -> LayoutReport:
        cfg = sc.runtime.config.layout
        profile = LayoutProfile.load(cfg.profile, cfg.margin_mode)
        stem = f"{ir.doc_id}.v{ir.version}"
        docx_path, fonts = compile_docx(ir, out_dir / f"{stem}.docx", profile)
        html_path = out_dir / f"{stem}.html"
        html_path.write_text(standalone_html(ir), encoding="utf-8")
        md_path = out_dir / f"{stem}.md"
        md_path.write_text(ir.to_markdown(), encoding="utf-8")
        first_body = next((s.text for _, s in ir.iter_sentences(include_attachments=False)), "")
        checks: list[LayoutCheck] = check_docx(docx_path, profile, ir.title, first_body)
        if cfg.render_check:
            render, rchecks = check_rendering(docx_path, ir, profile, fonts, out_dir)
            checks += rchecks
        else:
            render = RenderInfo(fonts_requested=sorted(fonts), notes=["配置关闭了实际渲染核验"])
        for f in sorted(fonts):
            checks.append(
                LayoutCheck(
                    rule_id="FONT",
                    item=f"字体：{f}",
                    expected="定稿环境已安装该字库（字库名属实务，国标规定的是字体类别）",
                    actual="已替代" if any(s.startswith(f) for s in render.substitutions) else ("已安装" if render.rendered else "未核验"),
                    status="warn" if any(s.startswith(f) for s in render.substitutions) else ("pass" if render.rendered else "unverified"),
                    clause="GB/T 9704—2012 5.2.2",
                    level="实务",
                    conditional=True,
                )
            )
        outputs = []
        for kind, p in (("docx", docx_path), ("html", html_path), ("md", md_path)):
            outputs.append(OutputFile(kind=kind, path=str(p), sha256=sha256_bytes(p.read_bytes())))
        if render.pdf_path:
            pp = Path(render.pdf_path)
            outputs.append(OutputFile(kind="pdf", path=str(pp), sha256=sha256_bytes(pp.read_bytes())))
        report = LayoutReport(profile=profile.id, profile_version=profile.version, checks=checks, render=render, outputs=outputs)
        sc.note(
            "skill.layout",
            {
                "doc": stem,
                "rendered": render.rendered,
                "pages": render.pages,
                "failures": [c.item for c in report.failures()],
                "substitutions": len(render.substitutions),
                "outputs": [o.kind for o in outputs],
            },
        )
        return report
