"""技能3 文种与权限判断：文件性质 → 发文主体 → 受文主体 → 行文关系 → 事项权限 → 文种与适用程序。

输出“需要哪些真实程序及材料”，而不是自行生成“已通过合法性审核”。
不把所有通知都认定为规范性文件，也不只按文件名称排除。
"""

from __future__ import annotations

import re

from ..knowledge import kb
from ..rules.genre_rules import detect_request_matters, may_issue_order
from ..rules.registry import citation
from ..schemas.genre import AuthorityFinding, GenreDecision, ProcedureRequirement
from ..schemas.sources import SourceBundle
from ..schemas.state import Stage
from ..schemas.task import Direction, Purpose, TaskLayer, TaskSpec
from .base import Skill, SkillContext
from .task_modeling import preliminary_genre

RIGHTS_CUES = ("公民", "法人", "其他组织", "企业", "经营者", "个人", "群众", "申请人", "许可", "处罚", "收费", "罚款", "强制", "义务", "权利", "准入", "资格", "登记", "备案要求")
COMPETITION_CUES = ("经营者", "企业", "市场准入", "招商引资", "补贴", "奖励", "税收优惠", "政府采购", "招标投标", "特许经营", "指定交易", "限定", "本地企业", "外地企业", "价格", "收费标准")
COORDINATION_CUES = ("会同", "联合", "配合", "协同", "牵头", "共同", "各相关部门", "有关部门")
LEADER_RE = re.compile(r"(市长|省长|县长|区长|书记|主任|局长|厅长|部长|院长|校长)(同志)?$")
INTERNAL_INTERNAL_DOCS = ("请示", "报告", "纪要", "工作方案", "汇报材料", "工作总结", "讲话稿", "调研报告", "批复")
GOV_TYPES = ("政府", "政府部门", "人民政府", "行政机关", "办公厅（室）")


def _organ_name(v) -> str:
    if isinstance(v, dict):
        return v.get("name", "")
    return str(v or "")


def _organ_type(v) -> str:
    if isinstance(v, dict):
        return v.get("type", "未知")
    return "未知"


def _infer_type(name: str, given: str) -> str:
    # 内设机构以名称为准：“示例大学教务处”即使被标为“高校”，也是内设机构（条例第十七条）
    if "内设" in (given or "") or (re.search(r"(处|科|室)$", name) and not name.endswith(("办公室", "办公厅"))):
        return "部门内设机构"
    if given and given != "未知":
        return given
    if name.endswith("人民政府"):
        return "政府"
    if name.endswith(("办公厅", "人民政府办公室")):
        return "办公厅（室）"
    if re.search(r"(处|科|室)$", name) and not name.endswith(("办公室",)):
        return "部门内设机构"
    if name.endswith(("局", "委员会", "委", "厅", "署", "办")):
        return "政府部门"
    if name.endswith("医院"):
        return "医院"
    if name.endswith(("大学", "学院")):
        return "高校"
    if name.endswith(("研究所", "研究院")):
        return "科研机构"
    return "未知"


class GenreAuthoritySkill(Skill):
    name = "gongwen-genre-authority"
    number = 3
    title = "文种与权限判断"
    stage = Stage.EVIDENCE
    channel_name = "planner"
    allowed_tools = ("gongwen_policy_search",)
    output_artifact = "genre_decision"

    def run(self, sc: SkillContext, spec: TaskSpec, bundle: SourceBundle | None = None) -> GenreDecision:
        profile = sc.runtime.profile
        text = spec.request_text + "\n" + ("\n".join(u.text for u in bundle.units[:400]) if bundle else "")
        direction = spec.relation.value or Direction.UNKNOWN.value
        requested = spec.requested_genre
        if spec.suggested_genre.status in ("已确认", "用户提供") and spec.suggested_genre.value:
            genre, why = spec.suggested_genre.value, "用户已确认文种"
        else:
            genre, why = preliminary_genre(spec.purposes, direction, requested)
        g = kb.genre(genre)
        if genre == "通知" and Purpose.ISSUE_PLAN.value in spec.purposes:
            # “印发……方案的通知”：通知是文种，方案是所印发的事务材料
            m = re.search(r"印发[^，。]{0,40}?(实施方案|工作方案|方案|工作要点|要点|管理办法|实施细则|办法|细则|管理制度)", spec.request_text)
            if m:
                genre = kb.canonical_genre(m.group(1)) or "工作方案"
                g = kb.genre(genre)
                why = "方案、要点、办法等需由通知印发：通知为文种，所印发的材料作为附件"
        decision = GenreDecision(
            layer=spec.layer.value or TaskLayer.FORMAL.value,
            direction=direction,
            suggested_genre=genre if (g is None or g.statutory) else (g.issue_vehicle.split("（")[0] if g and g.issue_vehicle and spec.purposes and Purpose.ISSUE_PLAN.value in spec.purposes else None),
            material_type=None if (g is None or g.statutory) else genre,
            requested_genre=requested,
        )
        if g is not None and not g.statutory:
            decision.issue_vehicle = g.issue_vehicle
            decision.layer = TaskLayer.AFFAIRS.value
            if g.issue_vehicle:
                decision.reasons.append(f"“{genre}”不是条例第八条所列文种；需要正式下发时，应以{g.issue_vehicle}印发，{genre}作为附件")
                decision.citations.append(citation("GW-GENRE-011"))
        if decision.suggested_genre:
            dirs = (kb.genre(decision.suggested_genre).directions or []) if kb.genre(decision.suggested_genre) else []
            explicit = decision.suggested_genre == requested
            if len(dirs) == 1 and (decision.direction == Direction.UNKNOWN.value or (explicit and decision.direction != dirs[0])):
                # 文种只有一种行文方向（报告上行、函平行、决定与通报下行）：据文种确定，仍在任务契约中供人工核对
                if decision.direction != Direction.UNKNOWN.value:
                    decision.reasons.append(f"需求中的行文关系推断为{decision.direction}，与所要求的文种“{decision.suggested_genre}”不一致，已按文种定为{dirs[0]}，请核对收发文机关关系")
                else:
                    decision.reasons.append(f"行文方向未在需求中说明，按文种“{decision.suggested_genre}”定为{dirs[0]}")
                decision.direction = dirs[0]
            elif decision.direction == Direction.UNKNOWN.value and Direction.PUBLIC.value in dirs:
                decision.direction = Direction.PUBLIC.value  # 决议、命令（令）、公报等公布性文种
        if decision.suggested_genre:
            gi = kb.genre(decision.suggested_genre)
            decision.format_type = {"函": "letter", "纪要": "jiyao", "命令（令）": "command"}.get(decision.suggested_genre, "general")
            if gi:
                decision.reasons.append(f"{decision.suggested_genre}：{gi.definition}（条例{gi.article}）")
        decision.reasons.append(why)
        decision.citations.append(citation("GW-GENRE-001"))
        # ---- 与用户字面要求的冲突
        if requested and genre and requested != genre and (kb.genre(requested) is None or kb.genre(requested).statutory):
            msg = f"用户要求“{requested}”，但办文意图更适合“{genre}”：{why}"
            if requested == "报告" and genre == "请示":
                msg += "。请示与报告的区别不仅在标题和结尾，更涉及用途与办理要求：请示需要上级答复，报告中不得夹带请示事项"
                decision.citations.append(citation("GW-GENRE-012"))
            decision.conflicts.append(msg)
            decision.alternatives.append(requested)
            decision.needs_human = True
        # ---- 单位可用文种
        allowed = profile.get("allowed_genres") or []
        if decision.suggested_genre and allowed and decision.suggested_genre not in allowed:
            decision.conflicts.append(f"本单位配置档（{profile['title']}）未启用文种“{decision.suggested_genre}”，请核对本单位公文处理制度")
            decision.needs_human = True
        if profile.get("tiaoli_mode") == "参照":
            decision.reasons.append(f"本单位为{profile['title']}，参照执行《党政机关公文处理工作条例》（第四十条），发文权限以本单位制度和主管部门要求为准，不能视为与行政机关相同")
        # ---- 请示一文一事
        if decision.suggested_genre == "请示":
            found = detect_request_matters([spec.request_text] + ([u.text for u in bundle.units] if bundle else []))
            decision.single_matter_check = sorted(found)
            if len(found) >= 2:
                decision.needs_human = True
                decision.conflicts.append(f"检测到多个请求事项（{'、'.join(found)}），请示应当一文一事，请确认是否拆分")
                decision.citations.append(citation("GW-GENRE-002"))
        # ---- 行文关系与权限
        self._authority(decision, spec, profile)
        # ---- 专门程序识别
        self._procedures(decision, spec, text, profile)
        if direction == Direction.UNKNOWN.value:
            decision.needs_human = True
        decision.confidence = round(0.9 - 0.2 * len(decision.conflicts) - (0.2 if direction == Direction.UNKNOWN.value else 0), 2)
        sc.note("skill.genre_authority", {"genre": decision.suggested_genre, "material_type": decision.material_type, "conflicts": len(decision.conflicts), "procedures": [p.code for p in decision.procedures], "authority": [a.code for a in decision.authority_findings]})
        return decision

    # ------------------------------------------------------------------
    def _authority(self, d: GenreDecision, spec: TaskSpec, profile: dict) -> None:
        issuer = spec.issuer.value if spec.issuer.known else None
        iname = _organ_name(issuer)
        itype = _infer_type(iname, _organ_type(issuer)) if iname else "未知"
        recips = spec.recipients.value if spec.recipients.known else []
        rnames = [_organ_name(r) for r in recips]
        genre = d.suggested_genre
        if itype == "部门内设机构" and d.direction != Direction.INTERNAL.value and rnames:
            d.authority_findings.append(
                AuthorityFinding(
                    code="INTERNAL_UNIT_EXTERNAL",
                    message=f"发文主体“{iname}”疑似部门内设机构；{profile.get('internal_unit_rule', '部门内设机构除办公厅（室）外不得对外正式行文')}",
                    severity="阻断送审",
                    basis=[citation("GW-ROUTE-006")],
                    out_of_authority=True,
                )
            )
        if d.direction == Direction.DOWN.value and itype == "政府部门":
            gov_recips = [r for r in rnames if r.endswith(("人民政府", "党委")) or r.startswith("各") and r.endswith(("人民政府", "政府"))]
            if gov_recips:
                d.authority_findings.append(
                    AuthorityFinding(
                        code="DEPT_TO_LOWER_GOV",
                        message=f"政府部门向下级党委、政府（{'、'.join(gov_recips)}）行文：不得发布指令性公文或提出指令性要求；需经政府审批的具体事项，经政府同意后可由职能部门行文，文中须注明已经政府同意",
                        severity="阻断送审",
                        basis=[citation("GW-ROUTE-007")],
                        out_of_authority=False,
                    )
                )
        if d.direction == Direction.UP.value:
            if any(LEADER_RE.search(r) for r in rnames):
                d.authority_findings.append(
                    AuthorityFinding(code="TO_LEADER", message="除上级机关负责人直接交办事项外，不得以本机关名义向上级机关负责人报送公文", severity="重要", basis=[citation("GW-ROUTE-004")])
                )
            if len(rnames) > 1:
                d.authority_findings.append(
                    AuthorityFinding(code="MULTI_MAIN", message="上行文原则上主送一个上级机关，根据需要同时抄送相关上级机关和同级机关", severity="一般", basis=[citation("GW-ROUTE-001")])
                )
        # 只能由特定机关使用的文种
        if genre == "命令（令）" and iname and not may_issue_order(iname):
            d.authority_findings.append(
                AuthorityFinding(
                    code="ORDER_ISSUER",
                    message=f"“{iname}”不是有权发布命令（令）的机关：命令（令）用于公布行政法规和规章、宣布施行重大强制性措施、批准授予和晋升衔级、嘉奖有关单位和人员；地方规章以人民政府令公布，政府部门不得以本部门名义发令。需部署或告知的事项可改用通知、通告",
                    severity="阻断送审",
                    basis=[citation("GW-GENRE-013")],
                    out_of_authority=True,
                )
            )
            for alt in ("通知", "通告"):
                if alt not in d.alternatives:
                    d.alternatives.append(alt)
        if genre == "议案":
            if iname and not iname.endswith("人民政府"):
                d.authority_findings.append(
                    AuthorityFinding(
                        code="MOTION_ISSUER",
                        message=f"议案只能由各级人民政府按照法律程序向同级人民代表大会或其常务委员会提请审议；“{iname}”不能以本机关名义提出议案，应报本级人民政府研究后由政府提请",
                        severity="阻断送审",
                        basis=[citation("GW-GENRE-014")],
                        out_of_authority=True,
                    )
                )
            bad = [r for r in rnames if r and "人民代表大会" not in r]
            if bad:
                d.authority_findings.append(AuthorityFinding(code="MOTION_RECIPIENT", message=f"议案的主送机关应为同级人民代表大会或其常务委员会（当前：{'、'.join(bad)}）", severity="重要", basis=[citation("GW-GENRE-014")]))
        # 同一地方的两个政府部门之间一般不相隶属：请求批准应使用函（条例第八条（十四））
        prefix = re.match(r"^(.{2,6}?[省市县区])", iname or "")
        if prefix and itype == "政府部门" and d.direction == Direction.UP.value:
            peers = [r for r in rnames if r.startswith(prefix.group(1)) and _infer_type(r, "未知") == "政府部门"]
            if peers:
                d.authority_findings.append(
                    AuthorityFinding(
                        code="PEER_DEPARTMENT",
                        message=f"“{iname}”与“{'、'.join(peers)}”可能是同级、不相隶属的政府部门；不相隶属机关之间请求批准和答复审批事项应使用函，而不是请示。请确认二者的隶属关系",
                        severity="重要",
                        basis=[citation("GW-ROUTE-011")],
                    )
                )
                if "函" not in d.alternatives:
                    d.alternatives.append("函")
                d.needs_human = True
        if d.direction == Direction.PARALLEL.value and genre in ("请示", "批复"):
            d.authority_findings.append(
                AuthorityFinding(code="PARALLEL_APPROVAL", message="不相隶属机关之间请求批准和答复审批事项应使用函", severity="重要", basis=[citation("GW-ROUTE-011")])
            )
        # 机关隶属图（单位配置 organs）：判断越级
        organs = {o["name"]: o for o in profile.get("organs") or []}
        if iname in organs and d.direction == Direction.UP.value:
            parent = organs[iname].get("parent")
            grand = organs.get(parent, {}).get("parent") if parent else None
            for r in rnames:
                if grand and r == grand:
                    d.authority_findings.append(
                        AuthorityFinding(code="SKIP_LEVEL", message=f"主送“{r}”为越级行文；一般不得越级行文，特殊情况需要越级行文的，应当同时抄送被越过的机关（{parent}）", severity="重要", basis=[citation("GW-ROUTE-005")])
                    )

    def _procedures(self, d: GenreDecision, spec: TaskSpec, text: str, profile: dict) -> None:
        genre = d.suggested_genre or d.material_type or ""
        issuer = spec.issuer.value if spec.issuer.known else None
        itype = _infer_type(_organ_name(issuer), _organ_type(issuer)) if issuer else "未知"
        is_admin = itype in GOV_TYPES or profile.get("name") == "party_gov"
        internal_doc = genre in INTERNAL_INTERNAL_DOCS
        rights_hits = [c for c in RIGHTS_CUES if c in text]
        if is_admin and not internal_doc and genre in ("通知", "决定", "意见", "通告", "公告", "") and len(rights_hits) >= 2:
            d.procedures.append(
                ProcedureRequirement(
                    code="LEGALITY_REVIEW",
                    name="行政规范性文件合法性审核",
                    trigger=f"内容可能涉及公民、法人和其他组织权利义务（命中：{'、'.join(rights_hits[:5])}）。是否属于规范性文件需按制定主体、公文种类、管理事项判断",
                    basis=[citation("GW-PROC-001")],
                    status="需人工判断是否适用",
                    materials_needed=["合法性审核意见（如适用）", "制定主体清单与管理事项类别依据"],
                )
            )
        comp_hits = [c for c in COMPETITION_CUES if c in text]
        if is_admin and len(comp_hits) >= 2 and not internal_doc:
            d.procedures.append(
                ProcedureRequirement(
                    code="FAIR_COMPETITION",
                    name="公平竞争审查",
                    trigger=f"可能属于涉及经营者经济活动的政策措施（命中：{'、'.join(comp_hits[:5])}）",
                    basis=[citation("GW-PROC-002")],
                    status="需人工判断是否适用",
                    materials_needed=["公平竞争审查意见（如适用）"],
                )
            )
        coord = [c for c in COORDINATION_CUES if c in text]
        if coord and d.suggested_genre != "纪要":  # 纪要记载会议议定事项，会议本身即协调机制
            d.procedures.append(
                ProcedureRequirement(
                    code="CONSULTATION",
                    name="征求相关部门意见或会签",
                    trigger=f"事项可能涉及其他地区或部门职权（命中：{'、'.join(coord[:4])}）；下行文涉及多个部门职权且未协商一致的，不得向下行文",
                    basis=[citation("GW-PROC-003"), citation("GW-ROUTE-008")],
                    status="需真实程序及材料",
                    materials_needed=["相关部门意见或会签记录"],
                )
            )
        if d.direction == Direction.UP.value and d.suggested_genre:  # 汇报材料等事务文书不是正式行文，不另走签发程序
            if itype == "政府部门" and any(k in text for k in ("重大", "重要")):
                d.procedures.append(
                    ProcedureRequirement(
                        code="LEVEL_APPROVAL",
                        name="经本级党委、政府同意或者授权",
                        trigger="部门向上级主管部门请示、报告重大事项",
                        basis=[citation("GW-ROUTE-003")],
                        status="需人工判断是否适用",
                    )
                )
            d.procedures.append(
                ProcedureRequirement(code="PRINCIPAL_SIGN", name="由机关主要负责人签发", trigger="上行文", basis=[citation("GW-PROC-004")], status="需真实程序及材料", materials_needed=["签发记录（意见、姓名和完整日期）"])
            )
        for up in profile.get("unit_procedures") or []:
            hits = [k for k in up.get("trigger_keywords", []) if k in text]
            if hits:
                d.procedures.append(
                    ProcedureRequirement(code=up["code"], name=up["name"], trigger=f"命中关键词：{'、'.join(hits)}", basis=[citation("GW-PROC-006")], status="需人工判断是否适用")
                )
