"""技能11 版式编译与检查：使用适用模板排版并核验输出。

* 生成参数核验（回读 DOCX）与实际渲染核验（LibreOffice → PDF 测量）分开报告；
* 版式规则保留标准中的条件性（“一般”“推荐”“可以”只提示）；
* 字体不可用或发生替代时明确报告，不声称已完全满足指定版式；
* 成文日期、发文字号和签发信息不用生成时间或模型猜测填充。
"""

from __future__ import annotations

from pathlib import Path

from ..layout.pipeline import layout_document
from ..layout.templates import resolve_profile
from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutReport
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
        # 公文模板：任务创建时指定的（--template）优先，其次配置 layout.template；都没有时按基础配置档默认参数
        template = str(sc.state.options.get("layout_template") or cfg.template or "")
        profile = resolve_profile(template, data_dir=sc.runtime.data_dir, workspace=sc.runtime.workspace, profile_id=cfg.profile, margin_mode=cfg.margin_mode)
        report = layout_document(ir, out_dir, profile=profile, render_check=cfg.render_check, font_substitution=cfg.font_substitution)
        sc.note(
            "skill.layout",
            {
                "doc": f"{ir.doc_id}.v{ir.version}",
                "template": report.template,
                "deviations": len(report.deviations),
                "rendered": report.render.rendered,
                "pages": report.render.pages,
                "failures": [c.item for c in report.failures()],
                "substitutions": len(report.render.substitutions),
                "outputs": [o.kind for o in report.outputs],
            },
        )
        return report
