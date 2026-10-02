"""技能8 跨材料一致性检查：比对正文、附件、表格、相关文稿。

同一事项的请示正文、可行性说明、经费表、实施方案和汇报摘要共享同一个事项数据模型，
一处关键事实修改后，可以定位所有受影响的文件；同时避免把“送审方案中的拟议目标”
复制成“总结中的已实现成绩”。
"""

from __future__ import annotations

from ..rules import CheckContext
from ..rules import consistency as cons
from ..schemas.ir import DocumentIR
from ..schemas.review import ConsistencyReport, ReviewIssue
from ..schemas.state import Stage
from .base import Skill, SkillContext


class ConsistencyCheckSkill(Skill):
    name = "gongwen-consistency-check"
    number = 8
    title = "跨材料一致性检查"
    stage = Stage.REVIEW
    channel_name = "reviewer"
    allowed_tools = ()
    output_artifact = "consistency_report"

    def run(self, sc: SkillContext, check_ctx: CheckContext) -> tuple[ConsistencyReport, list[ReviewIssue]]:
        issues: list[ReviewIssue] = []
        if sc.features.consistency_check:
            for fn in cons.CHECKERS:
                issues.extend(fn(check_ctx))
        report = cons.build_report(check_ctx, issues)
        sc.note("skill.consistency", {"findings": len(report.findings), "checked": report.checked})
        return report, issues


def matter_siblings(sc: SkillContext, doc_id: str) -> list[DocumentIR]:
    """同一事项下其他任务的最新文稿（事项级一致性）。"""
    out: list[DocumentIR] = []
    store = sc.runtime.tasks
    for tid in store.list_tasks():
        if tid == sc.state.task_id:
            continue
        try:
            from ..schemas.state import TaskState

            st = TaskState.model_validate_json((store.task_dir(tid) / "state.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if st.matter_id != sc.state.matter_id:
            continue
        for d in st.doc_ids:
            vs = store.versions(tid, d)
            if vs:
                ir = store.load_version(tid, d, vs[-1], DocumentIR)
                if ir and ir.doc_id != doc_id:
                    out.append(ir)
    return out
