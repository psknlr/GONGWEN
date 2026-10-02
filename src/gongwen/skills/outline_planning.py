"""技能6 方案与提纲规划：先形成提纲与措施表，再写正文。

每个拟写段落记录：段落目的、核心内容、依赖的事实与依据、涉及的责任/时限/资源、
哪些已确认、哪些仍是建议、篇幅预算。系统可以提示缺失、提出候选方案，
但不能替代用户决定预算、实施规模、审批结论或政策承诺。
"""

from __future__ import annotations

import re

from ..knowledge import kb
from ..rules.semantics import features as sem_features
from ..rules.textutil import DATE_RE, split_sentences
from ..schemas.common import EvidenceRef
from ..schemas.facts import Fact, FactLedger, FactStatus, Progress
from ..schemas.genre import GenreDecision
from ..schemas.outline import AlternativePlan, Measure, OutlinePlan, ParagraphPlan, SectionPlan
from ..schemas.policy import PolicyPack
from ..schemas.sources import SourceBundle
from ..schemas.state import Stage
from ..schemas.task import TaskSpec
from .base import Skill, SkillContext
from .policy_retrieval import is_substantive

MEASURE_VERBS = ("建立", "开展", "推进", "实施", "落实", "组织", "完善", "建设", "采购", "购置", "设立", "制定", "制订", "培训", "改造", "新建", "扩建", "配备", "整治", "检查", "评估", "试点", "推广", "召开", "报送", "上报", "申请")
DEADLINE_RE = re.compile(r"(\d{4}年\d{1,2}月(?:\d{1,2}日)?(?:前|底前|以前|之前)?|\d{1,2}月(?:\d{1,2}日)?(?:前|底前)|年底前|年内|月底前|季度末前)")
CONDITION_RE = re.compile(r"((?:如|若|确有|必要时|对于|凡)[^，。；]{1,30})")
EXCEPTION_RE = re.compile(r"(除[^，。；]{1,30}外)")
SUBJECT_RE = re.compile(r"^([一-鿿]{2,20}?(?:委员会|局|厅|办公室|处|科|中心|医院|学院|大学|部门|单位|各[一-鿿]{1,6}))(?:负责|牵头|要|应|将|拟|按照|组织)")
REQUIREMENT_RE = re.compile(r"(负责|牵头|要|应当|须|必须|务必|请各|责任单位)")
MEASURE_ROLES = ("proposal", "plans", "items", "tasks", "next", "goal", "suggestions", "division", "schedule")
DIVISION_RE = re.compile(r"(负责|牵头|配合|协助|分工|责任单位)")
GOAL_RE = re.compile(r"(目标|拟新建|新建|建成|达到|覆盖率|提升至|提高到|实现)")
PROBLEM_CUES = ("问题", "不足", "困难", "短板", "制约", "瓶颈", "滞后", "缺口", "不够", "不强", "不高", "隐患")

GENRE_SECTIONS: dict[str, list[tuple[str, str, str]]] = {
    # (role, heading, function)
    "请示": [("facts", "基本情况", "事实"), ("reasons", "必要性和依据", "依据"), ("proposal", "拟议方案", "措施"), ("resources", "经费测算及来源", "事实")],
    "报告": [("work", "工作开展情况", "事实"), ("problems", "存在的问题", "分析"), ("plans", "下一步工作安排", "措施")],
    "通知": [("items", "主要任务", "措施"), ("requirements", "工作要求", "要求")],
    "函": [("matter", "", "请求")],
    "纪要": [("decisions", "会议议定事项", "措施"), ("pending", "待研究事项", "事实")],
    "工作方案": [("goal", "工作目标", "措施"), ("scope", "工作范围", "事实"), ("tasks", "主要任务", "措施"), ("division", "职责分工", "措施"), ("schedule", "进度安排", "措施"), ("resources", "保障措施", "措施"), ("evaluation", "评价方式", "要求")],
    "汇报材料": [("overview", "基本情况", "事实"), ("work", "主要工作和成效", "事实"), ("problems", "存在问题", "分析"), ("next", "下一步打算", "措施")],
    "工作总结": [("work", "主要工作", "事实"), ("results", "取得的成效", "事实"), ("problems", "问题与不足", "分析"), ("next", "下一步打算", "措施")],
    "调研报告": [("method", "调研基本情况", "事实"), ("findings", "主要发现", "事实"), ("analysis", "原因分析", "分析"), ("suggestions", "对策建议", "措施")],
}
BUDGETS = {"请示": 1200, "报告": 2000, "通知": 1500, "函": 600, "纪要": 1200, "工作方案": 2500, "批复": 500, "意见": 2500}


def extract_measure(ids, text: str, fact: Fact | None, origin: str = "材料记载") -> Measure:
    f = sem_features(text)
    subj = SUBJECT_RE.match(text)
    verb = next((v for v in MEASURE_VERBS if v in text), "")
    obj = ""
    if verb:
        after = text.split(verb, 1)[1]
        obj = re.split(r"[，。；]", after)[0][:24]
    dl = DEADLINE_RE.search(text)
    cond = CONDITION_RE.search(text)
    exc = EXCEPTION_RE.search(text)
    status = "已批准" if fact and fact.status == FactStatus.APPROVED else "拟议"
    return Measure(
        measure_id=ids.next("M"),
        subject=subj.group(1) if subj else "",
        action=verb,
        obj=obj,
        condition=cond.group(1) if cond else "",
        deadline=dl.group(1) if dl else "",
        obligation="、".join(f.obligation_words),
        exceptions=exc.group(1) if exc else "",
        basis=[EvidenceRef(kind="fact", id=fact.fact_id)] if fact else [],
        status=status,
        origin=origin,
        resources=[],
        text=text,
        confirmed=origin != "系统建议",
    )


class OutlinePlanningSkill(Skill):
    name = "gongwen-outline-planning"
    number = 6
    title = "方案与提纲规划"
    stage = Stage.OUTLINE_CONFIRM
    channel_name = "outliner"
    allowed_tools = ("gongwen_case_style",)
    output_artifact = "outline"

    def run(self, sc: SkillContext, spec: TaskSpec, genre: GenreDecision, ledger: FactLedger, policies: PolicyPack, bundle: SourceBundle | None = None) -> OutlinePlan:
        gname = genre.suggested_genre or genre.material_type or spec.requested_genre or "通知"
        doc_kind = genre.material_type or gname
        gi = kb.genre(doc_kind)
        subject = str(spec.subject.value or "有关事项")
        issuer = spec.issuer.value.get("name") if spec.issuer.known and isinstance(spec.issuer.value, dict) else ""
        plan = OutlinePlan(genre=genre.suggested_genre, material_type=genre.material_type, title=self._title(issuer, subject, genre))
        plan.total_budget_chars = BUDGETS.get(doc_kind, 1500)
        usable = [f for f in ledger.facts if "example" not in f.tags and f.status not in (FactStatus.CONFLICT, FactStatus.UNKNOWN)]
        planned = [f for f in usable if f.status == FactStatus.PROPOSED]
        current = [f for f in usable if f.status != FactStatus.PROPOSED]
        problems = [f for f in current if any(c in f.statement for c in PROBLEM_CUES)]
        done_or_ongoing = [f for f in current if f not in problems and "table" not in f.tags and "computed" not in f.tags and f.progress in (Progress.COMPLETED, Progress.ONGOING, Progress.NONE)]
        money = [f for f in usable if f.kind == "money"]
        computed = [f for f in usable if f.status == FactStatus.COMPUTED]
        substantive = [e for e in policies.items if is_substantive(e, sc.runtime.policies, subject)]
        # ---- 措施表（来源：材料中的拟议/安排类陈述）
        seen_measure_text: set[str] = set()
        requirement_like = [f for f in current if f.kind == "text" and REQUIREMENT_RE.search(f.statement) and not f.progress == Progress.COMPLETED]
        for f in planned + requirement_like:
            if f.statement in seen_measure_text:
                continue
            seen_measure_text.add(f.statement)
            m = extract_measure(sc.ids, f.statement, f)
            m.resources = [x.fact_id for x in money if x.statement == f.statement]
            plan.measures.append(m)
        # 作为措施来源的句子不再作为“情况”重复陈述
        measure_sources = {r.id for m in plan.measures for r in m.basis if r.kind == "fact"}
        done_or_ongoing = [f for f in done_or_ongoing if f.fact_id not in measure_sources]
        assigned = self._distribute(plan.measures, [r for r, _, _ in (GENRE_SECTIONS.get(doc_kind) or [])])
        # ---- 开头段
        plan.opening = ParagraphPlan(
            para_id=sc.ids.next("PP"),
            function="依据" if substantive else "背景",
            purpose="说明发文缘由与依据",
            core=subject,
            refs=[EvidenceRef(kind="policy", id=e.evidence_id) for e in substantive[:2]],
            budget_chars=120,
        )
        # ---- 按文种内容契约组织章节
        sections = GENRE_SECTIONS.get(doc_kind) or [(c["key"], c["name"], (c.get("functions") or ["事实"])[0]) for c in (gi.contract if gi else [])]
        meeting_decided = [f for f in usable if "meeting:decided" in f.tags]
        meeting_discussed = [f for f in usable if "meeting:discussion" in f.tags]
        for role, heading, function in sections:
            sec = SectionPlan(section_id=sc.ids.next("S"), heading=heading, role=role)
            pick: list[Fact] = []
            measure_ids: list[str] = []
            if role == "scope":
                pick = [f for f in done_or_ongoing if re.search(r"范围|覆盖|对象|适用于|涉及", f.statement)][:4]
            elif role in ("facts", "work", "overview", "results", "findings", "method", "situation"):
                pick = [f for f in done_or_ongoing if f.kind != "plain" and "computed" not in f.tags][:8]
            elif role in ("problems", "analysis"):
                pick = problems[:5]
            elif role in MEASURE_ROLES:
                measure_ids = assigned.get(role, [])[:8]
            elif role == "resources":
                pick = (computed + [f for f in money if f.status == FactStatus.PROPOSED])[:6]
                if not pick and spec.resource_mentions:
                    plan.contract_missing.append("资源测算与来源")
                    plan.open_questions.append("请示涉及资源，但未提供测算明细和资金来源。请补充（系统不会自行补写金额）。")
            elif role == "reasons":
                pick = problems[:3]
            elif role == "decisions":
                pick = meeting_decided
                if not pick:
                    plan.open_questions.append("会议记录中未识别到明确的议定事项。纪要只能写入确已议定的事项，请确认。")
            elif role == "pending":
                pick = meeting_discussed
            elif role in ("requirements", "evaluation"):
                pick = [f for f in usable if DATE_RE.search(f.statement) or "报送" in f.statement or "联系人" in f.statement][:4]
            if role == "matter":
                pick = (done_or_ongoing + planned)[:6]
            core_parts = list(dict.fromkeys(f.statement for f in pick))[:2] + [m.text for m in plan.measures if m.measure_id in measure_ids][:2]
            para = ParagraphPlan(
                para_id=sc.ids.next("PP"),
                function=function,
                purpose=heading or role,
                core="；".join(core_parts)[:120],
                refs=[EvidenceRef(kind="fact", id=f.fact_id) for f in pick]
                + ([EvidenceRef(kind="policy", id=e.evidence_id) for e in substantive[:3]] if role == "reasons" else []),
                measure_ids=measure_ids,
                confirmed_items=[f.fact_id for f in pick if f.status in (FactStatus.VERIFIED, FactStatus.APPROVED, FactStatus.COMPUTED)],
                suggested_items=[f.fact_id for f in pick if f.status in (FactStatus.RECORDED, FactStatus.PROPOSED)],
                budget_chars=max(120, plan.total_budget_chars // max(1, len(sections))),
            )
            sec.paragraphs.append(para)
            if not para.refs and not para.measure_ids:
                plan.contract_missing.append(heading or role)
            plan.sections.append(sec)
        # ---- 结尾
        closing_candidates = gi.closing_candidates(genre.direction, spec.purposes[0] if spec.purposes else None) if gi else []
        plan.closing = ParagraphPlan(para_id=sc.ids.next("PP"), function="结语" if doc_kind != "请示" else "请求", purpose="结束语", core=closing_candidates[0] if closing_candidates else "", budget_chars=30)
        if doc_kind == "请示" and not money and spec.resource_mentions:
            plan.open_questions.append("请明确请示事项：申请金额、资金来源与用途。")
        # ---- 候选方案：仅当材料本身包含规模选项时提出，不凭空构造
        corpus = " ".join(f.statement for f in usable)
        if any(w in corpus for w in ("试点", "先行")) and any(w in corpus for w in ("全面", "全覆盖", "全部")):
            plan.alternatives = [
                AlternativePlan(key="A", name="先行试点", applicable_when="条件不完全具备、需要积累经验时", resources="以材料中试点规模为准", risks="覆盖面有限", differences=["实施范围较小", "资源需求较低"]),
                AlternativePlan(key="B", name="全面实施", applicable_when="条件成熟、已有试点基础时", resources="以材料中全面实施规模为准", risks="资源与组织要求更高", differences=["实施范围扩大", "需确认资源来源"]),
            ]
            plan.open_questions.append("材料同时出现“试点”和“全面实施”两种规模，请选择方案（系统不会替您决定实施规模）。")
        for m in plan.measures:
            if not m.subject:
                plan.open_questions.append(f"措施“{m.text[:30]}”未明确责任主体，请补充（系统不会擅自确定责任）。")
        style = sc.runtime.cases.style_refs(doc_kind, subject, k=1)
        if style:
            plan.attachments = []
            sc.note("skill.style_reference", {"case_id": style[0]["case_id"], "usage": style[0]["usage"]})
        if any(f.kind == "money" for f in computed) and doc_kind in ("请示", "函", "工作方案"):
            plan.attachments.append(f"{subject}经费测算表" if "经费" not in subject else f"{subject.replace('申请', '')}测算表")
        sc.note("skill.outline", {"sections": len(plan.sections), "measures": len(plan.measures), "missing": plan.contract_missing, "questions": len(plan.open_questions)})
        return plan

    @staticmethod
    def _distribute(measures: list[Measure], roles: list[str]) -> dict[str, list[str]]:
        """把措施分到各章节，每项只出现一次：职责分工 > 工作目标 > 任务类章节；
        只有没有任务类章节时，带时限的措施才进入“进度安排”。缺少的章节以待补占位，不重复填充。"""
        present = [r for r in roles if r in MEASURE_ROLES]
        out: dict[str, list[str]] = {r: [] for r in present}
        task_role = next((r for r in present if r not in ("division", "goal", "schedule")), None)
        for m in measures:
            if "division" in out and DIVISION_RE.search(m.text):
                out["division"].append(m.measure_id)
            elif "goal" in out and GOAL_RE.search(m.text) and re.search(r"\d", m.text):
                out["goal"].append(m.measure_id)
            elif task_role:
                out[task_role].append(m.measure_id)
            elif "schedule" in out and m.deadline:
                out["schedule"].append(m.measure_id)
            elif present:
                out[present[0]].append(m.measure_id)
        return out

    @staticmethod
    def _title(issuer: str, subject: str, genre: GenreDecision) -> str:
        subject = subject.strip()
        if genre.suggested_genre == "纪要":
            meeting = re.sub(r"(的)?(会议)?(纪要)?$", "", subject).strip("的") or "【待补：会议名称】"
            return f"{meeting}{'会议' if not meeting.endswith(('会', '会议')) else ''}纪要"
        if genre.material_type and genre.suggested_genre == "通知":
            core = f"关于印发《{subject}{'' if subject.endswith(genre.material_type) else genre.material_type}》的通知"
        elif genre.material_type:
            return f"{subject}{'' if subject.endswith(genre.material_type) else genre.material_type}"
        else:
            core = f"关于{subject}的{genre.suggested_genre or ''}"
        return f"{issuer}{core}" if issuer else core
