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
from .references import find_incoming, incoming_core, is_reply

MEASURE_VERBS = ("建立", "开展", "推进", "实施", "落实", "组织", "完善", "建设", "采购", "购置", "设立", "制定", "制订", "培训", "改造", "新建", "扩建", "配备", "整治", "检查", "评估", "试点", "推广", "召开", "报送", "上报", "申请")
DEADLINE_RE = re.compile(r"(\d{4}年\d{1,2}月(?:\d{1,2}日)?(?:前|底前|以前|之前)?|\d{1,2}月(?:\d{1,2}日)?(?:前|底前)|年底前|年内|月底前|季度末前)")
CONDITION_RE = re.compile(r"((?:如|若|确有|必要时|对于|凡)[^，。；]{1,30})")
EXCEPTION_RE = re.compile(r"(除[^，。；]{1,30}外)")
SUBJECT_RE = re.compile(r"^([一-鿿]{2,20}?(?:委员会|局|厅|办公室|处|科|中心|医院|学院|大学|部门|单位|各[一-鿿]{1,6}))(?:负责|牵头|要|应|将|拟|按照|组织)")
# 要求与授权性措施（“可以结合实际……”“鼓励……”也是措施，不能因没有“要”“须”而丢失）
REQUIREMENT_RE = re.compile(r"(负责|牵头|(?<![主重需])要(?![素点])|应当|须|必须|务必|请各|责任单位|可以|鼓励|支持|提倡|引导|原则上)")
MEASURE_ROLES = ("proposal", "plans", "items", "tasks", "next", "goal", "suggestions", "division", "schedule", "opinions", "articles")
RESULT_CUES = re.compile(r"(提升|提高|增长|增加|下降|减少|缩短|达到|实现|覆盖|成效|改善|好转|获评|获得)")
DECISION_CUES = re.compile(r"(研究决定|决定|议定|同意|予以|给予|命名|授予|通报表彰|通报批评)")
DIVISION_RE = re.compile(r"(负责|牵头|配合|协助|分工|责任单位)")
GOAL_RE = re.compile(r"(目标|拟新建|新建|建成|达到|覆盖率|提升至|提高到|实现)")
PROBLEM_CUES = ("问题", "不足", "困难", "短板", "制约", "瓶颈", "滞后", "缺口", "不够", "不强", "不高", "隐患", "偏慢", "偏低", "偏少", "较慢", "不到位", "不平衡", "不充分", "薄弱", "欠缺", "老化")

GENRE_SECTIONS: dict[str, list[tuple[str, str, str]]] = {
    # (role, heading, function)
    "请示": [("facts", "基本情况", "事实"), ("reasons", "必要性和依据", "依据"), ("proposal", "拟议方案", "措施"), ("resources", "经费测算及来源", "事实")],
    "报告": [("work", "工作开展情况", "事实"), ("problems", "存在的问题", "分析"), ("plans", "下一步工作安排", "措施")],
    "通知": [("items", "主要任务", "措施"), ("requirements", "工作要求", "要求")],
    "函": [("matter", "", "请求")],
    "纪要": [("situation", "", "事实"), ("decisions", "会议议定事项", "措施"), ("pending", "待研究事项", "事实")],
    "批复": [("answer", "", "措施"), ("requirements", "", "要求")],
    # 通报、决定、通告、公告篇幅较短，按段落组织，不设层次标题
    "通报": [("facts", "", "事实"), ("verdict", "", "分析"), ("requirements", "", "要求")],
    "决定": [("basis", "", "依据"), ("decisions", "", "措施"), ("requirements", "", "要求")],
    "通告": [("basis", "", "依据"), ("items", "", "措施"), ("requirements", "", "要求")],
    "公告": [("matter", "", "措施")],
    "意见": [("background", "", "背景"), ("opinions", "主要措施", "措施"), ("requirements", "组织实施", "要求")],
    "决议": [("situation", "", "事实"), ("decisions", "", "措施"), ("requirements", "", "要求")],
    "命令（令）": [("matter", "", "措施")],
    "公报": [("content", "", "事实")],
    "议案": [("reason", "", "事实")],
    "讲话稿": [("work", "关于前一阶段工作", "事实"), ("problems", "关于存在的问题", "分析"), ("next", "关于下一步重点工作", "措施"), ("requirements", "关于工作要求", "要求")],
    "简报": [("content", "", "事实")],
    "工作要点": [("tasks", "重点任务", "措施"), ("requirements", "工作要求", "要求")],
    "管理办法": [("articles", "", "措施")],
    "工作方案": [("goal", "工作目标", "措施"), ("scope", "工作范围", "事实"), ("tasks", "主要任务", "措施"), ("division", "职责分工", "措施"), ("schedule", "进度安排", "措施"), ("resources", "保障措施", "措施"), ("evaluation", "评价方式", "要求")],
    "汇报材料": [("overview", "基本情况", "事实"), ("work", "主要工作和成效", "事实"), ("problems", "存在问题", "分析"), ("next", "下一步打算", "措施")],
    "工作总结": [("work", "主要工作", "事实"), ("results", "取得的成效", "事实"), ("problems", "问题与不足", "分析"), ("next", "下一步打算", "措施")],
    "调研报告": [("method", "调研基本情况", "事实"), ("findings", "主要发现", "事实"), ("analysis", "原因分析", "分析"), ("suggestions", "对策建议", "措施")],
}
# 文种的常见变体：内容结构与一般写法不同
VARIANT_SECTIONS: dict[str, list[tuple[str, str, str]]] = {
    "会议": [("m_time", "会议时间", "事实"), ("m_place", "会议地点", "事实"), ("m_people", "参会人员", "事实"), ("m_agenda", "会议内容", "事实"), ("requirements", "有关要求", "要求")],
    "任免": [("appoint", "", "措施")],
    "转发": [("requirements", "", "要求")],
}
KV_KEYS = {
    "m_time": ("会议时间", "时间"),
    "m_place": ("会议地点", "地点"),
    "m_people": ("参会人员", "参加人员", "与会人员", "参会范围", "出席人员"),
    "m_agenda": ("会议议程", "会议内容", "主要议程", "议程"),
}
APPOINT_RE = re.compile(r"任命|免去|聘任|聘为|任职|免职|兼任|试用期")


def notice_variant(request: str, doc_kind: str) -> str:
    """文种的常见变体：通知的转发（批转）、会议、任免；意见的指导、实施、若干意见。其他返回空。"""
    if doc_kind == "意见":
        m = re.search(r"(指导|实施|若干)意见", request)
        return m.group(1) if m else ""
    if doc_kind != "通知":
        return ""
    if re.search(r"转发|批转", request):
        return "转发"
    if re.search(r"任职|免职|任免|任命|聘任", request):
        return "任免"
    if re.search(r"(召开|举办|举行|参加)[^，。]{0,30}(会议|会|培训班|论坛)", request):
        return "会议"
    return ""


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
        variant = notice_variant(spec.request_text, doc_kind)
        incoming = find_incoming(bundle, forwarding=variant == "转发")
        reply = is_reply(spec.request_text, doc_kind) and incoming is not None
        forwarding = variant == "转发" and incoming is not None
        plan = OutlinePlan(genre=genre.suggested_genre, material_type=genre.material_type, variant=variant, title=self._title(issuer, subject, genre, incoming if (reply or forwarding) else None, variant))
        plan.total_budget_chars = BUDGETS.get(doc_kind, 1500)
        usable = [f for f in ledger.facts if "example" not in f.tags and f.status not in (FactStatus.CONFLICT, FactStatus.UNKNOWN)]
        if reply or forwarding:
            # 答复、转发类文稿：来文自身的陈述（对方的请求、背景、要求）不是本机关的事实，不写入本文正文
            usable = [f for f in usable if not any(s.material_id == incoming.material_id for s in f.sources)]
        planned = [f for f in usable if f.status == FactStatus.PROPOSED]
        current = [f for f in usable if f.status != FactStatus.PROPOSED]
        problems = [f for f in current if any(c in f.statement for c in PROBLEM_CUES)]
        done_or_ongoing = [f for f in current if f not in problems and "table" not in f.tags and "computed" not in f.tags and f.progress in (Progress.COMPLETED, Progress.ONGOING, Progress.NONE)]
        money = [f for f in usable if f.kind == "money"]
        computed = [f for f in usable if f.status == FactStatus.COMPUTED]
        substantive = [e for e in policies.items if is_substantive(e, sc.runtime.policies, subject)]
        # ---- 措施表（来源：材料中的拟议/安排类陈述）
        seen_measure_text: set[str] = set()
        requirement_like = [f for f in current if REQUIREMENT_RE.search(f.statement) and f.progress not in (Progress.COMPLETED, Progress.ONGOING) and "table" not in f.tags and "computed" not in f.tags and "meeting_record" not in f.tags]
        for f in [x for x in planned + requirement_like if not any(t.startswith("kv:") for t in x.tags)]:
            if f.statement in seen_measure_text:
                continue
            seen_measure_text.add(f.statement)
            m = extract_measure(sc.ids, f.statement, f)
            m.resources = [x.fact_id for x in money if x.statement == f.statement]
            plan.measures.append(m)
        # 作为措施来源的句子不再作为“情况”重复陈述
        measure_sources = {r.id for m in plan.measures for r in m.basis if r.kind == "fact"}
        done_or_ongoing = [f for f in done_or_ongoing if f.fact_id not in measure_sources]
        sections = VARIANT_SECTIONS.get(variant) or GENRE_SECTIONS.get(doc_kind) or [(c["key"], c["name"], (c.get("functions") or ["事实"])[0]) for c in (gi.contract if gi else [])]
        assigned = self._distribute(plan.measures, [r for r, _, _ in sections])
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
        meeting_decided = [f for f in usable if "meeting:decided" in f.tags]
        meeting_pending = [f for f in usable if "meeting:pending" in f.tags or ("meeting:discussion" in f.tags and "meeting:situation" not in f.tags)]
        meeting_situation = [f for f in usable if "meeting:situation" in f.tags]
        # 已作为措施来源或已被前面章节使用的事实，不在“要求”“保障”等章节重复陈述
        used: set[str] = set(measure_sources)
        used_text: set[str] = {f.statement for f in usable if f.fact_id in measure_sources}

        def fresh(fs: list[Fact]) -> list[Fact]:
            return [f for f in fs if f.fact_id not in used and f.statement not in used_text]
        for role, heading, function in sections:
            sec = SectionPlan(section_id=sc.ids.next("S"), heading=heading, role=role)
            pick: list[Fact] = []
            measure_ids: list[str] = []
            if role in KV_KEYS:
                # 会议通知的时间、地点、人员、内容：取材料中“会议时间：……”等事项
                pick = [f for f in usable if any(f"kv:{k}" in f.tags for k in KV_KEYS[role])][:3]
            elif role == "appoint":
                # 任免事项只取材料中的任免决定原文，不补写职务、时间
                pick = [f for f in fresh(current) if APPOINT_RE.search(f.statement)][:6]
                if not pick:
                    plan.contract_missing.append("任免事项")
                    plan.open_questions.append("材料中没有任免决定。任免事项须依据真实任免决定，请补充材料或人工填写。")
            elif role == "content":
                # 公报、简报：照材料陈述主要情况与数据
                pick = [f for f in fresh(current) if "computed" not in f.tags][:12]
            elif role == "reason":
                # 议案：提请审议的事由（起草情况、政府审议情况）
                pick = [f for f in fresh(usable) if "computed" not in f.tags][:8]
            elif role == "method" and doc_kind == "调研报告":
                pick = [f for f in fresh(done_or_ongoing) if re.search(r"调研|走访|座谈|问卷|实地|抽查|样本", f.statement) and not re.search(r"发现|显示|表明|反映", f.statement)][:4]
            elif role == "findings" and doc_kind == "调研报告":
                pick = [f for f in fresh(done_or_ongoing) if not re.search(r"建议|对策", f.statement)][:6]
            elif role == "suggestions" and doc_kind == "调研报告":
                pick = [f for f in fresh(usable) if re.search(r"建议|对策|应当|需要", f.statement)][:6]
                measure_ids = assigned.get(role, [])[:8]
            elif role == "decisions" and doc_kind == "决议":
                pick = [f for f in fresh(usable) if "meeting:decided" in f.tags or (DECISION_CUES.search(f.statement) and f.status != FactStatus.PROPOSED)]
                if not pick:
                    plan.contract_missing.append("决议事项")
                    plan.open_questions.append("材料中没有会议讨论通过的决议事项。决议只能写入会议讨论通过的内容，请补充会议记录或表决结果。")
            elif role == "situation" and doc_kind == "决议":
                pick = [f for f in fresh(done_or_ongoing) if not DECISION_CUES.search(f.statement)][:6]
            elif role == "scope":
                pick = [f for f in done_or_ongoing if re.search(r"范围|覆盖|对象|适用于|涉及", f.statement)][:4]
            elif role == "results":
                # 成效：有成效表述的现状事实；与“主要工作”不重复
                pick = [f for f in fresh(done_or_ongoing) if RESULT_CUES.search(f.statement) and f.kind != "plain"][:6]
            elif role == "facts" and doc_kind == "通报":
                # 通报的事实经过：现状与问题都是事实，不因含“滞后”等词而归入评价
                pick = [f for f in fresh(current) if "computed" not in f.tags and not DECISION_CUES.search(f.statement)][:8]
            elif role in ("facts", "work", "overview", "findings", "method", "situation") and doc_kind != "纪要":
                pick = [f for f in fresh(done_or_ongoing) if f.kind != "plain" and "computed" not in f.tags][:8]
            elif role in ("background", "basis"):
                # 背景与依据：现状与问题（政策依据在开头段引用）
                pick = [f for f in fresh(done_or_ongoing + problems) if "computed" not in f.tags][:6]
            elif role == "verdict":
                # 通报的表彰、批评决定：只取材料中的真实决定；需求要求表彰或批评而材料没有决定时留待补
                pick = [f for f in fresh(current) if DECISION_CUES.search(f.statement)][:3]
                if not pick and re.search(r"表彰|表扬|批评", spec.request_text):
                    plan.contract_missing.append("表彰或批评决定")
                    plan.open_questions.append("材料中没有表彰或批评的决定。通报中的表彰、批评须依据真实研究决定，请补充材料或人工填写。")
            elif role in ("problems", "analysis"):
                pick = problems[:5]
            elif role in MEASURE_ROLES:
                measure_ids = assigned.get(role, [])[:8]
            elif role == "resources":
                pick = fresh(computed + [f for f in money if f.status == FactStatus.PROPOSED])[:6]
                if not pick and spec.resource_mentions:
                    plan.contract_missing.append("资源测算与来源")
                    plan.open_questions.append("请示涉及资源，但未提供测算明细和资金来源。请补充（系统不会自行补写金额）。")
            elif role == "reasons":
                pick = problems[:3]
            elif role == "answer":
                # 答复意见：只用会议议定事项与审批类材料，不用来文自身的陈述
                pick = [f for f in usable if "meeting:decided" in f.tags or "approval_candidate" in f.tags]
                if not pick:
                    plan.open_questions.append("材料中没有可作为答复依据的决定（会议议定或审批意见）。批复的答复意见须来自真实决定，请补充材料或人工填写。")
            elif role == "decisions" and doc_kind == "决定":
                pick = [f for f in fresh(usable) if "meeting:decided" in f.tags or "approval_candidate" in f.tags or (DECISION_CUES.search(f.statement) and f.status != FactStatus.PROPOSED)]
                if not pick:
                    plan.contract_missing.append("决定事项")
                    plan.open_questions.append("材料中没有可作为决定事项的研究结论。决定事项须来自真实研究决定，请补充材料或人工填写。")
            elif role == "decisions":
                pick = meeting_decided
                if not pick:
                    plan.open_questions.append("会议记录中未识别到明确的议定事项。纪要只能写入确已议定的事项，请确认。")
            elif role == "pending":
                pick = meeting_pending
            elif role == "situation" and doc_kind == "纪要":
                pick = meeting_situation
            elif role in ("requirements", "evaluation"):
                # 执行要求：带时限、报送、联系人的要求性陈述；已完成或推进中的现状陈述不是要求
                pick = [f for f in fresh(usable) if (DATE_RE.search(f.statement) or "报送" in f.statement or "联系人" in f.statement) and f.progress not in (Progress.COMPLETED, Progress.ONGOING) and "computed" not in f.tags and "table" not in f.tags and not any(t.startswith("kv:") for t in f.tags)]
                pick = pick[:4]
                if variant in ("转发", "会议"):
                    # 转发、会议通知没有“任务”章节：本机关提出的要求（材料中的要求性陈述）都列在这里
                    measure_ids = [m.measure_id for m in plan.measures][:8]
                    pick = [f for f in pick if f.fact_id not in {r.id for m in plan.measures for r in m.basis}]
                    pick += [f for f in usable if "kv:联系人" in f.tags or "kv:联系电话" in f.tags or "kv:报名方式" in f.tags]
            if role == "matter":
                # 函、公告：必要背景 + 商洽或公告事项；涉及经费时写明测算合计（明细见附件）
                pick = (done_or_ongoing + planned)[:6] + [f for f in computed if f.kind == "money"][:1]
            used.update(f.fact_id for f in pick)
            used_text.update(f.statement for f in pick)
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
        closing_candidates = gi.closing_candidates(genre.direction, spec.purposes[0] if spec.purposes else None, include_acceptable=False) if gi else []
        if reply and doc_kind == "函":
            closing_candidates = ["特此函复。", "专此函复。"]  # 复函用“函复”，不用“请函复”
        elif doc_kind == "函" and gi:
            # 函的结束语按用途：询问用“请函复”，告知用“特此函告”，商洽用“请予支持为盼”，请求批准用“请予批准为盼”
            purpose = next((p for p, rx in (("询问答复", r"询问|咨询|了解|函询"), ("告知", r"告知|函告|通报"), ("请求批准", r"申请|请求批准|审批|核准"), ("商洽工作", r"商请|协助|支持|配合|商洽")) if re.search(rx, spec.request_text)), None)
            if purpose == "告知":
                closing_candidates = ["特此函告。"]
            elif purpose:
                closing_candidates = gi.closing_candidates(genre.direction, purpose, include_acceptable=False)
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
            core = re.sub(r"^(拟申请|申请|商请|请求|恳请|请)(支持|协助|安排|解决|给予)?", "", subject) or subject
            plan.attachments.append(f"{core}经费测算表" if "经费" not in core else f"{core}测算表")
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
    def _title(issuer: str, subject: str, genre: GenreDecision, incoming=None, variant: str = "") -> str:
        subject = subject.strip()
        kind = genre.material_type or genre.suggested_genre or ""
        if variant == "转发" and incoming is not None:
            # 转发类通知（实务）：标题写作“××转发××关于××的通知”，避免“关于转发……的通知的通知”
            verb = "批转" if "批转" in subject else "转发"
            return f"{issuer}{verb}{incoming.title}" if incoming.title.endswith("通知") else f"{issuer}关于{verb}{incoming.title}的通知"
        if kind == "命令（令）":
            return ""  # 命令（令）格式不设标题：发文机关标志、令号之后即为正文（GB/T 9704—2012 10.2）
        if kind in ("公报", "简报", "工作要点"):
            core = re.sub(r"^(一期|一份|本期)", "", subject)
            core = re.sub(r"(的)?(工作)?(简报)?$", "", core) if kind == "简报" else core
            core = re.sub(r"^关于|的$|情况的$", "", core)
            if kind == "公报":
                return core if core.endswith("公报") else core + "公报"
            if kind == "工作要点":
                return core if core.endswith("要点") else core + "工作要点"
            return core
        if kind == "讲话稿":
            m = re.match(r"^(.*?)在(.+?)上(的)?(讲话|发言)?(稿)?$", subject)
            return f"在{m.group(2)}上的讲话" if m else subject
        if kind == "汇报材料":
            core = re.sub(r"^(向[^，。]{1,20}?)?(汇报|报告)", "", subject)
            core = re.sub(r"(的)?(汇报材料|汇报|情况汇报)$", "", core)
            return f"关于{re.sub(r'^关于', '', core)}的汇报"
        if kind == "调研报告":
            core = re.sub(r"(的)?调研(报告)?$", "", subject)
            return f"关于{re.sub(r'^关于', '', core)}的调研报告"
        if genre.suggested_genre in ("批复", "函") and incoming is not None:
            core = incoming_core(incoming.title) if genre.suggested_genre == "函" else re.sub(r"的(请示|报告|函|意见)$", "", re.sub(r"^.*?关于", "", incoming.title))
            kind = "批复" if genre.suggested_genre == "批复" else "复函"
            return f"{issuer}关于{core}的{kind}" if issuer else f"关于{core}的{kind}"
        if genre.suggested_genre == "纪要":
            meeting = re.sub(r"(的)?(会议)?(纪要)?$", "", subject).strip("的") or "【待补：会议名称】"
            return f"{meeting}{'会议' if not meeting.endswith(('会', '会议')) else ''}纪要"
        if genre.material_type and genre.suggested_genre == "通知":
            name = re.sub(r"^印发", "", subject)
            name = name if re.search(r"(方案|计划|要点|办法|细则|规定|制度|规则|守则)$", name) else name + genre.material_type
            core = f"关于印发《{name}》的通知"
        elif genre.material_type:
            return subject if re.search(r"(方案|计划|要点|办法|细则|总结|材料|报告)$", subject) else f"{subject}{genre.material_type}"
        elif genre.suggested_genre == "意见" and variant:
            # 指导意见、实施意见、若干意见：变体名称属于文种名称，不写进事由
            core = f"关于{re.sub(r'(的)?' + variant + '$', '', subject)}的{variant}意见"
        else:
            core = f"关于{subject}的{genre.suggested_genre or ''}"
        return f"{issuer}{core}" if issuer else core
