"""技能1 任务建模：把自然语言需求转换为办文任务契约 TaskSpec。

关键字段缺失时先检索已有材料；仍无法解决、且会影响文种、权限或重要事实的，才向用户提问。
不为了获得一份看起来完整的任务单，反复询问不影响当前阶段的细节。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..harness.injection import UNTRUSTED_NOTICE, wrap_untrusted
from ..knowledge import kb
from ..llm.base import ChatMessage, ModelRefused, ModelUnavailable
from ..schemas.common import slot
from ..schemas.sources import SourceBundle
from ..schemas.state import Stage
from ..schemas.task import Direction, Gap, GapImpact, Organ, Purpose, TaskLayer, TaskSpec
from .base import Skill, SkillContext

PURPOSE_CUES: list[tuple[Purpose, tuple[str, ...]]] = [
    (Purpose.APPROVE, ("申请", "请求批准", "审批", "批准", "核拨", "拨付", "立项", "请予", "恳请", "增加编制", "申请经费")),
    (Purpose.INSTRUCT, ("请求指示", "如何处理", "请示意见", "请上级明确")),
    (Purpose.REPLY_LOWER, ("批复", "答复下级", "回复下级请示", "对请示的答复")),
    (Purpose.REPORT, ("汇报", "报告情况", "反映情况", "进展情况", "工作情况", "上报情况", "报告…情况")),
    (Purpose.REPLY_UPPER, ("回复上级询问", "答复上级")),
    (Purpose.RECORD, ("会议纪要", "纪要", "会议议定", "办公会")),
    (Purpose.COMMEND, ("表彰", "通报批评", "批评", "通报表扬")),
    (Purpose.ISSUE_PLAN, ("印发方案", "印发", "下发方案", "印发办法", "印发制度", "印发细则")),
    (Purpose.ANNOUNCE, ("公告", "通告", "向社会公布", "面向社会")),
    (Purpose.DECIDE, ("决定",)),
    (Purpose.OPINION, ("意见",)),
    (Purpose.NEGOTIATE, ("商洽", "联系", "协助", "协调", "征求意见", "请予支持", "合作")),
    (Purpose.INQUIRE, ("询问", "咨询", "答复函", "复函")),
    (Purpose.EXECUTE, ("部署", "开展", "组织", "落实", "安排", "做好", "要求各", "检查", "整治", "推进")),
    (Purpose.INFORM, ("告知", "周知", "知悉", "放假", "召开会议", "参加会议")),
    (Purpose.PLAN, ("工作方案", "实施方案", "工作计划")),
    (Purpose.SPEECH, ("讲话稿", "发言稿", "讲话")),
    (Purpose.SUMMARY, ("工作总结", "年度总结", "总结")),
    (Purpose.RESEARCH, ("调研报告", "调研")),
    (Purpose.BRIEFING, ("汇报材料", "情况汇报")),
]

UP_CUES = ("向上级", "向主管部门", "主管部门", "上级机关", "上报", "呈报", "报送上级", "向市政府", "向省政府", "向区政府", "向县政府", "向市委", "向省委", "向集团", "向总部", "向主管单位", "报请")
# 注意：线索词不能是常见的事由用词（如“基层医疗”中的“基层”），否则会把事由误当成行文关系
DOWN_CUES = ("各科室", "各单位", "各部门", "下属单位", "所属单位", "全院", "全校", "全所", "各区", "各县", "各乡镇", "各街道", "下发", "各处室", "各学院", "各分院")
PARALLEL_CUES = ("兄弟单位", "不相隶属", "友邻", "商洽", "函告", "复函", "协助", "合作单位", "发函", "致函", "去函", "商请")
REGIONS = (
    "北京市", "天津市", "上海市", "重庆市", "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省", "江苏省", "浙江省", "安徽省",
    "福建省", "江西省", "山东省", "河南省", "湖北省", "湖南省", "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省",
    "甘肃省", "青海省", "台湾省", "内蒙古自治区", "广西壮族自治区", "西藏自治区", "宁夏回族自治区", "新疆维吾尔自治区",
)
RESOURCE_WORDS = ("经费", "资金", "预算", "编制", "用地", "设备", "人员", "场地", "补助")
_ORGAN_RE = re.compile(
    r"([一-鿿]{2,24}?(?:人民政府办公厅|人民政府办公室|人民政府|委员会|办公厅|办公室|管理局|监督管理局|卫生健康委|医院|大学|学院|研究院|研究所|中心|集团|公司|局|厅|委|署|院|部))"
)
_AS_OF_RE = re.compile(r"(?:按|依据|根据|截至|截止)?\s*(\d{4})年(?:(\d{1,2})月(?:(\d{1,2})日)?)?(?:的|时)?(?:政策|规定|口径|背景)")
_GENERIC_ORGANS = ("主管部门", "上级机关", "上级部门", "有关部门", "相关部门", "有关单位")


def detect_requested_genre(text: str) -> str | None:
    names = list(kb.STATUTORY_GENRES) + [g for g in kb.genres() if not kb.genres()[g].statutory]
    names += list(kb.seed("genres.yaml").get("aliases", {}).keys())
    best: tuple[int, int, str] | None = None
    for n in names:
        for m in re.finditer(re.escape(n), text):
            # 越靠后、越长的匹配越可能是文种（“……的报告”）
            key = (m.end(), len(n), n)
            if best is None or key > best:
                best = key
    if best is None:
        return None
    return kb.canonical_genre(best[2])


def detect_purposes(text: str, requested: str | None) -> list[str]:
    found: list[str] = []
    for p, cues in PURPOSE_CUES:
        if any(c in text for c in cues):
            found.append(p.value)
    # 文种词本身也是意图线索（但不覆盖实质意图）
    genre_purpose = {
        "请示": Purpose.APPROVE, "报告": Purpose.REPORT, "通知": Purpose.EXECUTE, "函": Purpose.NEGOTIATE,
        "纪要": Purpose.RECORD, "批复": Purpose.REPLY_LOWER, "通报": Purpose.COMMEND, "公告": Purpose.ANNOUNCE,
        "通告": Purpose.ANNOUNCE, "意见": Purpose.OPINION, "决定": Purpose.DECIDE, "工作方案": Purpose.PLAN,
        "讲话稿": Purpose.SPEECH, "工作总结": Purpose.SUMMARY, "调研报告": Purpose.RESEARCH, "汇报材料": Purpose.BRIEFING,
    }
    if requested in genre_purpose and genre_purpose[requested].value not in found:
        found.append(genre_purpose[requested].value)
    # “报告”字面 + “申请”实质：申请批准优先
    if Purpose.APPROVE.value in found and Purpose.REPORT.value in found and requested == "报告":
        found.remove(Purpose.REPORT.value)
        found.remove(Purpose.APPROVE.value)
        found.insert(0, Purpose.APPROVE.value)
    return list(dict.fromkeys(found))


def detect_direction(text: str, purposes: list[str]) -> str:
    if any(c in text for c in PARALLEL_CUES) and not any(c in text for c in ("向上级", "上报", "呈报", "报请")):
        return Direction.PARALLEL.value
    if any(c in text for c in UP_CUES):
        return Direction.UP.value
    if any(c in text for c in DOWN_CUES):
        return Direction.DOWN.value
    if any(c in text for c in PARALLEL_CUES):
        return Direction.PARALLEL.value
    if Purpose.RECORD.value in purposes:
        return Direction.MEETING.value
    if Purpose.ANNOUNCE.value in purposes:
        return Direction.PUBLIC.value
    if Purpose.REPLY_LOWER.value in purposes:
        return Direction.DOWN.value
    return Direction.UNKNOWN.value


def layer_of(genre: str | None, text: str) -> str:
    if any(k in text for k in ("规范性文件", "管理办法", "实施细则", "暂行规定")) and any(k in text for k in ("公民", "企业", "群众", "经营者", "申请人")):
        return TaskLayer.PROCEDURAL.value
    g = kb.genre(genre)
    if g is not None and not g.statutory:
        return TaskLayer.AFFAIRS.value
    return TaskLayer.FORMAL.value


def preliminary_genre(purposes: list[str], direction: str, requested: str | None) -> tuple[str | None, str]:
    P = Purpose
    if P.REPLY_LOWER.value in purposes:
        return "批复", "答复下级请示"
    if P.RECORD.value in purposes:
        return "纪要", "记载会议主要情况和议定事项"
    if P.APPROVE.value in purposes or P.INSTRUCT.value in purposes:
        if direction == Direction.PARALLEL.value:
            return "函", "不相隶属机关之间请求批准事项用函"
        if direction in (Direction.UP.value, Direction.UNKNOWN.value):
            return "请示", "实质意图是请求上级批准或指示"
    if P.ISSUE_PLAN.value in purposes:
        return "通知", "方案、办法等需由通知印发"
    if P.COMMEND.value in purposes:
        return "通报", "表彰先进、批评错误或告知重要情况"
    if P.ANNOUNCE.value in purposes:
        return (requested if requested in ("公告", "通告") else "通告"), "公开宣布事项"
    if P.REPORT.value in purposes or P.REPLY_UPPER.value in purposes:
        if direction in (Direction.UP.value, Direction.UNKNOWN.value):
            return "报告", "向上级汇报工作、反映情况"
    if P.NEGOTIATE.value in purposes or P.INQUIRE.value in purposes:
        return "函", "商洽工作、询问和答复问题"
    if P.DECIDE.value in purposes and requested == "决定":
        return "决定", "对重要事项作出决策和部署"
    if P.OPINION.value in purposes and requested == "意见":
        return "意见", "对重要问题提出见解和处理办法"
    if requested and kb.genre(requested) and not kb.genre(requested).statutory:
        return requested, "公务事务材料"
    if P.EXECUTE.value in purposes or P.INFORM.value in purposes:
        return "通知", "发布、传达要求执行或周知的事项"
    return requested, "按用户要求"


_ADDRESSING = re.compile(r"(请|帮我|帮忙)?(给|向|致|对)[^，。]{1,30}?(发函|去函|致函|行文|发文|写信|去信|发个函|发一个函)")
_SUBJECT_VERBS = re.compile(r"(申请|请求|报告|汇报|部署|开展|商请|印发|整理|做好|加强|推进|关于)")


def extract_subject(text: str, genre: str | None) -> str:
    # 多个小句时，选含文种词或主要办文动词的小句；其余小句（补充事实、对文号日期的要求等）不属于事由
    clauses = [c.strip() for c in re.split(r"[，,；;。！!？?：:\n]", text) if c.strip()]
    # “给××发函”“向××行文”只说明收文对象，不是事由
    clauses = [c for c in clauses if not _ADDRESSING.fullmatch(c)] or clauses
    if len(clauses) > 1:
        t = next((c for c in clauses if genre and (c.endswith(genre) or f"的{genre}" in c)), None) or next((c for c in clauses if _SUBJECT_VERBS.search(c)), clauses[0])
    else:
        t = text.strip()
    t = re.sub(r"^(请|麻烦|帮我|帮忙|需要|拟|我想|我要)?(帮我|给我)?(起草|写|拟写|拟|撰写|草拟|准备|整理)?(一份|一个|个|篇|一篇)?", "", t)
    t = re.sub(r"(。|！|\?|？)$", "", t)
    t = re.sub(r"^(根据|依据|按照)[^，。]{0,16}?(整理|形成|起草|撰写|写)(出)?(一份|一个|一篇)?", "", t)
    if genre:
        for name in {genre, genre.replace("（令）", ""), "会议纪要"}:
            if t.endswith(name):
                t = t[: -len(name)]
    t = t.rstrip("的").strip()
    # “给××发函，商请……”“向××行文……”：收发文机关和行文动作不属于事由
    t = re.sub(r"^(给|向|致|对)[^，。]{1,30}?(发函|去函|致函|行文|发文|写信|去信|发个函|发一个函)[，,、]?", "", t)
    t = re.sub(r"^(向[^，。]{1,20}?)(申请|请求|报告|汇报|提出|请示)", r"\2", t)
    if genre == "报告":
        t = re.sub(r"^(报告|汇报)", "", t)
    elif genre == "请示":
        t = re.sub(r"^请示", "", t)
    t = re.sub(r"^关于", "", t)
    return t[:40] or "【待确认：事由】"


def _organs_in(text: str) -> list[str]:
    out = []
    for m in _ORGAN_RE.finditer(text):
        name = m.group(1)
        name = re.sub(r"^(向|致|给|对|由|以|请|报|代|为|与|和|及|送|帮|写|起草|关于)+", "", name)
        if len(name) >= 3 and name not in out:
            out.append(name)
    return out


def _find_issuer_in_materials(bundle: SourceBundle | None) -> str | None:
    """先检索已有材料：落款、版头中的机关名称。"""
    if not bundle:
        return None
    for u in reversed(bundle.units):
        if u.kind in ("paragraph", "heading", "page") and len(u.text) <= 30:
            organs = _organs_in(u.text)
            if organs and u.text.strip() in organs:
                return organs[0]
    return None


class TaskModelingSkill(Skill):
    name = "gongwen-task-modeling"
    number = 1
    title = "任务建模"
    stage = Stage.TASK_CONFIRM
    channel_name = "planner"
    allowed_tools = ("gongwen_material_peek", "gongwen_policy_search")
    output_artifact = "task_spec"

    def run(self, sc: SkillContext, request_text: str, hints: dict[str, Any] | None = None, bundle: SourceBundle | None = None) -> TaskSpec:
        hints = hints or {}
        rt = sc.runtime
        profile = rt.profile
        text = request_text.strip()
        requested = kb.canonical_genre(hints.get("genre")) or detect_requested_genre(text)
        purposes = detect_purposes(text, requested)
        recips_hint = hints.get("recipients") or ""
        recips_hint = "、".join(recips_hint) if isinstance(recips_hint, list) else str(recips_hint)
        direction = hints.get("direction") or detect_direction(text + "\n" + recips_hint, purposes)
        genre, why = preliminary_genre(purposes, direction, requested)
        if genre and kb.genre(genre) and not kb.genre(genre).statutory and requested and requested != genre:
            pass
        spec = TaskSpec(task_id=sc.state.task_id, request_text=text, unit_profile=profile["name"])
        spec.requested_genre = requested
        spec.purposes = purposes
        spec.layer = slot(layer_of(genre, text), "推断", "需求文本")
        spec.relation = slot(direction, "用户提供" if hints.get("direction") else ("推断" if direction != Direction.UNKNOWN.value else "缺失"), "需求文本")
        spec.suggested_genre = slot(genre, "推断", why)
        spec.subject = slot(hints.get("subject") or extract_subject(text, requested or genre), "用户提供" if hints.get("subject") else "推断", "需求文本")
        # ---- 发文主体
        issuer_name = hints.get("issuer") or rt.config.environment.unit_name or None
        status = "用户提供" if issuer_name else "缺失"
        if not issuer_name:
            found = _find_issuer_in_materials(bundle)
            if found:
                issuer_name, status = found, "材料记载"
        if issuer_name:
            spec.issuer = slot(Organ(name=issuer_name, type=hints.get("issuer_type", "未知")).model_dump(), status, "配置/提示/材料")
        # ---- 受文主体
        recips = hints.get("recipients")
        if isinstance(recips, str):
            recips = [r for r in re.split(r"[、，,；;]", recips) if r.strip()]
        if recips:
            spec.recipients = slot([Organ(name=r).model_dump() for r in recips], "用户提供", "提示")
        else:
            organs = [o for o in _organs_in(text) if o != issuer_name]
            generic = next((g for g in _GENERIC_ORGANS if g in text), None)
            if organs:
                spec.recipients = slot([Organ(name=o).model_dump() for o in organs[:3]], "推断", "需求文本")
            elif generic:
                spec.recipients = slot([Organ(name=f"{generic}（名称待确认）").model_dump()], "待确认", "需求文本")
        if hints.get("cc"):
            cc = hints["cc"] if isinstance(hints["cc"], list) else re.split(r"[、，,]", hints["cc"])
            spec.cc = slot([Organ(name=c.strip()).model_dump() for c in cc if c.strip()], "用户提供", "提示")
        # ---- 政策时点与地域
        as_of = hints.get("as_of")
        if as_of:
            spec.policy_as_of = date.fromisoformat(str(as_of))
            spec.policy_mode = "historical" if spec.policy_as_of < date.today() else "current"
        else:
            m = _AS_OF_RE.search(text)
            if m:
                y, mo, d = int(m.group(1)), int(m.group(2) or 12), int(m.group(3) or 28)
                spec.policy_as_of = date(y, mo, min(d, 28))
                spec.policy_mode = "historical"
        region = hints.get("region") or rt.config.environment.region or next((r for r in REGIONS if r in text), None)
        if region:
            spec.region = slot(region, "用户提供" if (hints.get("region") or rt.config.environment.region) else "推断", "提示/配置/需求文本")
        spec.resource_mentions = [w for w in RESOURCE_WORDS if w in text]
        spec.material_ids = [m for m in hints.get("material_ids", [])]
        spec.constraints = {k: str(v) for k, v in (hints.get("constraints") or {}).items()}
        # ---- 模型辅助（可选）：只补充，不覆盖确定性判断；所有抽取值须在原文中可见
        if sc.model_available("light"):
            self._model_refine(sc, spec, text, bundle)
        self._gaps(spec, bundle, requested, genre)
        process = sorted({m.group(1) for m in re.finditer(r"(发文字号|文号|成文日期|签发人|印发日期|份号)", text)})
        if process:
            spec.notes.append(f"需求中涉及{'、'.join(process)}：这些字段只能来自真实办理流程，系统不代为填写，文稿中保留占位。")
        if requested and genre and requested != genre:
            spec.notes.append(f"用户字面要求“{requested}”，但办文意图（{'、'.join(purposes) or '未识别'}）更适合“{genre}”：{why}。请确认。")
        sc.note("skill.task_modeling", {"requested": requested, "suggested": genre, "purposes": purposes, "direction": direction, "gaps": [g.field for g in spec.gaps]})
        return spec

    # ------------------------------------------------------------------
    def _gaps(self, spec: TaskSpec, bundle: SourceBundle | None, requested: str | None, genre: str | None) -> None:
        searched = bundle is not None
        formal = spec.layer.value != TaskLayer.AFFAIRS.value
        if not spec.issuer.known:
            spec.gaps.append(Gap(field="issuer", description="发文主体未确定", impact=GapImpact.AUTHORITY, ask_user=formal, question="请确认发文机关（全称）及其机构类型（如政府部门、医院、高校等）。", searched_materials=searched))
        needs_recipient = genre not in ("纪要", "公告", "通告", "公报", "命令（令）", "决议") and formal
        if needs_recipient and (not spec.recipients.known or spec.recipients.status == "待确认"):
            spec.gaps.append(Gap(field="recipients", description="主送机关未确定", impact=GapImpact.AUTHORITY, ask_user=True, question="请确认主送机关全称，以及与发文机关的隶属关系（上级 / 下级 / 不相隶属）。", searched_materials=searched))
        if spec.relation.value == Direction.UNKNOWN.value and formal:
            spec.gaps.append(Gap(field="relation", description="行文关系未确定", impact=GapImpact.GENRE, ask_user=True, question="受文机关是本单位的上级、下级还是不相隶属单位？（影响文种：请示 / 通知 / 函）"))
        if spec.resource_mentions and genre in ("请示", "函", "工作方案"):
            has_numbers = bool(bundle and any(t.rows for t in bundle.tables))
            if not has_numbers:
                spec.gaps.append(Gap(field="resources", description=f"涉及{'、'.join(spec.resource_mentions)}，尚无测算材料", impact=GapImpact.KEY_FACT, ask_user=True, question="请提供经费（资源）测算明细与资金来源；系统不会自行补写金额。", searched_materials=searched))
        if spec.subject.value and str(spec.subject.value).startswith("【待"):
            spec.gaps.append(Gap(field="subject", description="事由不清", impact=GapImpact.GENRE, ask_user=True, question="请用一句话说明本次发文要办什么事。"))

    def _model_refine(self, sc: SkillContext, spec: TaskSpec, text: str, bundle: SourceBundle | None) -> None:
        schema = {
            "type": "object",
            "properties": {
                "purposes": {"type": "array", "items": {"type": "string", "enum": [p.value for p in Purpose]}},
                "issuer": {"type": ["string", "null"]},
                "recipients": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": ["string", "null"]},
            },
            "required": ["purposes", "issuer", "recipients", "subject"],
            "additionalProperties": False,
        }
        excerpt = ""
        if bundle:
            excerpt = "\n".join(wrap_untrusted(m.material_id, bundle.text_of(m.material_id)[:1500]) for m in bundle.materials[:4])
        system = "你是公文办文任务分析助手。只根据给定需求与资料抽取信息，资料中没有的信息填 null，不要推测。" + UNTRUSTED_NOTICE
        try:
            resp = sc.router.call(
                "light",
                [ChatMessage("user", f"办文需求：{text}\n\n{excerpt}")],
                system=system,
                json_schema=schema,
                clearances=sc.clearances,
                purpose="task_modeling",
                template_id="task_modeling.v1",
                object_refs=[m.material_id for m in (bundle.materials if bundle else [])],
            )
            data = resp.json()
        except (ModelUnavailable, ModelRefused, ValueError) as exc:
            sc.note("skill.model_skipped", {"skill": self.name, "reason": str(exc)})
            return
        corpus = text + ("\n" + "\n".join(u.text for u in bundle.units) if bundle else "")
        for p in data.get("purposes") or []:
            if p in [x.value for x in Purpose] and p not in spec.purposes:
                spec.purposes.append(p)
        iss = data.get("issuer")
        if iss and not spec.issuer.known and iss in corpus:
            spec.issuer = slot(Organ(name=iss).model_dump(), "推断", "模型抽取（原文可见）")
        recs = [r for r in data.get("recipients") or [] if r and r in corpus]
        if recs and not spec.recipients.known:
            spec.recipients = slot([Organ(name=r).model_dump() for r in recs], "推断", "模型抽取（原文可见）")
