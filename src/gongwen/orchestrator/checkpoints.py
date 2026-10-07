"""人工审核节点：只能由人通过人工通道处理；模型通道不可代为确认。"""

from __future__ import annotations

from ..schemas.state import Checkpoint, CheckpointKind, CheckpointOption, Stage

OPTIONS: dict[CheckpointKind, list[CheckpointOption]] = {
    CheckpointKind.MATERIAL_CONFIRM: [
        CheckpointOption(key="confirm", label="按申报属性准入", effect="data.materials={材料ID: 属性}，未申报的按“公开”处理须明确选择"),
        CheckpointOption(key="reject", label="不准入并删除", effect="待确认材料一律不进入系统"),
    ],
    CheckpointKind.TASK_CONFIRM: [
        CheckpointOption(key="accept", label="确认任务契约", effect="进入材料解析"),
        CheckpointOption(key="edit", label="修改后确认", effect="data 可含 issuer/issuer_type/recipients/relation/genre/subject/as_of/region/cc"),
        CheckpointOption(key="need_material", label="先补充材料", effect="进入“待补材料”状态"),
        CheckpointOption(key="abort", label="终止任务", effect="任务结束"),
    ],
    CheckpointKind.AUTHORITY: [
        CheckpointOption(key="edit", label="调整发文主体或行文关系", effect="data 同任务契约；重新判断"),
        CheckpointOption(key="proceed", label="已有授权，继续", effect="须在说明中写明授权依据，问题保留在审校报告中"),
        CheckpointOption(key="abort", label="终止任务", effect="任务结束"),
    ],
    CheckpointKind.CONFLICT: [
        CheckpointOption(key="resolve", label="确认采信的数据", effect="data.confirm_facts=[事实ID]；未采信的一方标为未知"),
        CheckpointOption(key="mark_unknown", label="暂不采信冲突数据", effect="冲突事实均标为未知，文稿中以待补占位"),
        CheckpointOption(key="proceed", label="已人工裁定依据冲突，继续", effect="须在说明中写明裁定理由"),
    ],
    CheckpointKind.NEED_MATERIAL: [
        CheckpointOption(key="continue", label="材料已补充，继续", effect="重新准入与解析"),
        CheckpointOption(key="abort", label="终止任务", effect="任务结束"),
    ],
    CheckpointKind.OUTLINE_CONFIRM: [
        CheckpointOption(key="accept", label="确认提纲与措施", effect="进入起草"),
        CheckpointOption(key="edit", label="修改后确认", effect="data 可含 confirm_facts/add_facts/drop_measures/confirm_measures/alternative"),
        CheckpointOption(key="abort", label="终止任务", effect="任务结束"),
    ],
    CheckpointKind.REVIEW_ESCALATION: [
        CheckpointOption(key="apply", label="采纳建议修改", effect="应用需人工确认的修订建议，并重新审校"),
        CheckpointOption(key="keep", label="保留原文，作为讨论稿继续", effect="问题保留在送审包中"),
        CheckpointOption(key="revise", label="按意见修改", effect="data 可含 instruction/edits/fact_changes"),
    ],
    CheckpointKind.HUMAN_REVIEW: [
        CheckpointOption(key="submit", label="形成送审材料", effect="由人确认送审；系统不形成审批结论"),
        CheckpointOption(key="revise", label="退回修改", effect="data 可含 instruction/edits/fact_changes"),
        CheckpointOption(key="abort", label="终止任务", effect="任务结束"),
    ],
}

# 无头模式可按显式参数自动接受的节点及其默认选项；“人工送审”与“材料准入确认”“权限确认”永不自动
AUTO_ACCEPT_DEFAULTS: dict[CheckpointKind, str] = {
    CheckpointKind.TASK_CONFIRM: "accept",
    CheckpointKind.OUTLINE_CONFIRM: "accept",
    CheckpointKind.CONFLICT: "mark_unknown",
    CheckpointKind.REVIEW_ESCALATION: "keep",
}
AUTO_ACCEPT_KEYS = {
    "task_confirm": CheckpointKind.TASK_CONFIRM,
    "outline_confirm": CheckpointKind.OUTLINE_CONFIRM,
    "conflict": CheckpointKind.CONFLICT,
    "review_escalation": CheckpointKind.REVIEW_ESCALATION,
}


def make(ids, kind: CheckpointKind, stage: Stage, question: str, details: list[str] | None = None, payload: dict | None = None) -> Checkpoint:
    return Checkpoint(
        cp_id=ids.next("CP"),
        kind=kind,
        stage=stage,
        question=question,
        details=details or [],
        options=OPTIONS[kind],
        payload=payload or {},
        auto_acceptable=kind in AUTO_ACCEPT_DEFAULTS,
    )
