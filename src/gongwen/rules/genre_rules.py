"""文种、行文关系、结束语与称谓检查。"""

from __future__ import annotations

import re

from ..knowledge import kb
from ..schemas.common import EvidenceRef, Severity
from ..schemas.review import IssueType, ReviewIssue
from .base import CheckContext

REQUEST_CLOSINGS = ("妥否", "当否", "请批示", "请批复", "请予批复", "请予批准", "请审批", "请予审批", "请审示", "请予审议", "请予支持", "盼复", "请函复", "请予同意")
DOWNWARD_DIRECTIVES = ("请遵照执行", "请认真贯彻执行", "请贯彻落实", "请认真贯彻落实", "请结合实际认真贯彻执行", "认真组织实施")
REQUEST_VERB = re.compile(r"(申请|请求|恳请|拟请|请予|请批准|请审批|请核拨|请给予|请支持)")

# 请示“一文一事”：按事项类别识别多个请求事项
MATTER_CATEGORIES = {
    "经费": ("经费", "资金", "拨款", "预算", "补助", "核拨"),
    "编制人员": ("编制", "人员", "招聘", "职数", "岗位"),
    "用地": ("用地", "土地", "场地"),
    "立项建设": ("立项", "建设项目", "新建", "改扩建"),
    "设备采购": ("采购", "购置设备", "设备购置"),
    "机构设置": ("机构设置", "设立", "更名", "撤销机构"),
    "政策调整": ("政策", "标准调整", "收费"),
    "表彰": ("表彰", "奖励", "命名"),
}


# 各文种专用的结束语：出现在其他文种中即为错用
CLOSING_OWNER = {
    "特此通知": "通知", "特此报告": "报告", "特此通报": "通报", "此复": "批复", "特此批复": "批复",
    "特此函告": "函", "特此函复": "函", "特此函达": "函", "专此函复": "函", "特此公告": "公告", "特此通告": "通告",
}
LEADER_TITLE = re.compile(r"(书记|省长|市长|县长|区长|乡长|镇长|主任|局长|厅长|部长|院长|校长|处长|所长|同志)$")


def _last_sentences(ctx: CheckContext, n: int = 2):
    sents = list(ctx.ir.iter_sentences(include_attachments=False))
    return sents[-n:]


def check_title(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    ir = ctx.ir
    title = ir.title.strip()
    bare = title.rstrip("。，；：！？.,;:!? ")  # 末尾标点另报 GW-FMT-009，不影响文种判断
    if not title:
        if ir.format_type == "command":
            return []  # 命令（令）格式不设标题（GB/T 9704—2012 10.2）
        return [ctx.issue("GW-GENRE-008", IssueType.REQUIRED_MISSING, "缺少标题", field_name="title")]
    if title[-1] in "。，；：！？.,;:":
        out.append(ctx.issue("GW-FMT-009", IssueType.FORMAT, "标题末尾不用标点符号", field_name="title", original=title, auto_fixable=True, fix_hint={"strip_end": title[-1]}))
    if any(w in title for w in ("紧急", "特急", "加急")):
        out.append(ctx.issue("GW-GENRE-009", IssueType.FORMAT, "紧急程度应作为版头要素标注，不写入标题", field_name="title", original=title))
    g = kb.genre(ir.genre)
    if re.search(r"(请示报告|报告请示)$", bare):
        out.append(ctx.issue("GW-GENRE-001", IssueType.GENRE_MISMATCH, "“请示报告”不是法定文种：请求批准或指示用请示，汇报工作、反映情况用报告，二者不得混用", field_name="title", original=title, needs_human=True))
    if g and g.statutory:
        genre_tail = ir.genre.replace("（令）", "")
        if not (bare.endswith(ir.genre) or bare.endswith(genre_tail) or (ir.genre == "命令（令）" and bare.endswith("令"))):
            out.append(ctx.issue("GW-GENRE-008", IssueType.GENRE_MISMATCH, f"标题应以文种“{ir.genre}”结尾（标题由发文机关名称、事由和文种组成）", field_name="title", original=title))
        for other in kb.STATUTORY_GENRES:
            if other != ir.genre and bare.endswith(other) and other not in ir.genre:
                out.append(ctx.issue("GW-GENRE-001", IssueType.GENRE_MISMATCH, f"标题文种“{other}”与判定文种“{ir.genre}”不一致", field_name="title", original=title, needs_human=True))
                break
        if ctx.task and ctx.task.issuer.known:
            organ = ctx.task.issuer.value
            name = organ.get("name") if isinstance(organ, dict) else str(organ)
            short = organ.get("short_name") if isinstance(organ, dict) else None
            if name and name not in title and not (short and short in title) and ir.format_type != "jiyao" and name not in ir.title_note and ir.genre not in ("公报", "决议"):
                out.append(
                    ctx.issue(
                        "GW-GENRE-008",
                        IssueType.FORMAT,
                        f"标题未包含发文机关名称“{name}”；如本单位制度允许省略，请保持全文一致",
                        field_name="title",
                        original=title,
                        severity=Severity.INFO,
                    )
                )
    return out


def check_closing_and_direction(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    ir = ctx.ir
    genre = ir.genre
    direction = ctx.direction
    sentences = list(ir.iter_sentences(include_attachments=False))
    if not sentences:
        return out
    # 报告等非请示性公文夹带请示事项
    if genre in ("报告", "通知", "意见", "通报", "函", "纪要") and genre != "请示":
        g = kb.genre(genre)
        forbidden = list(kb.lexicon()["request_phrases"]) if genre == "报告" else ["妥否", "当否", "请批示", "请予批准", "请审批", "请予审批"]
        if genre == "函":
            forbidden = ["妥否", "当否", "请批示"]
        for b, s in sentences:
            hit = next((p for p in forbidden if p in s.text), None)
            if hit:
                if genre == "报告" or (g and hit in (g.forbidden_phrases or [])):
                    out.append(
                        ctx.issue(
                            "GW-GENRE-003",
                            IssueType.MIXED_REQUEST,
                            f"{genre}中出现请求批准表述“{hit}”。需要上级批准的事项应另行请示，不得在{genre}中夹带",
                            block=b,
                            sentence=s,
                            needs_human=True,
                        )
                    )
                elif genre != "函":
                    out.append(
                        ctx.issue(
                            "GW-GENRE-003",
                            IssueType.MIXED_REQUEST,
                            f"非请示性公文中出现“{hit}”，请确认是否夹带请示事项",
                            block=b,
                            sentence=s,
                            needs_human=True,
                        )
                    )
    # “申请……的报告”：以报告请求批准
    if genre == "报告" and re.search(r"申请|请求|核拨|拨付|追加", ir.title) and not any(i.type == IssueType.MIXED_REQUEST for i in out):
        b, s = sentences[0]
        out.append(ctx.issue("GW-GENRE-003", IssueType.MIXED_REQUEST, "标题为申请、请求事项，却使用“报告”：请求批准的事项应当用请示，报告中不得夹带请示事项", field_name="title", original=ir.title, needs_human=True))
    # 其他文种的专用结束语（请示另行检查）
    if genre and genre != "请示":
        for b, s in sentences:
            t = s.text.strip()
            hit = next((p for p, owner in CLOSING_OWNER.items() if owner != genre and (t == p + "。" or t.endswith("，" + p + "。") or t == p)), None)
            if hit:
                good = (kb.genre(genre).closing_candidates(direction, include_acceptable=False) or [""])[0] if kb.genre(genre) else ""
                out.append(ctx.issue("GW-GENRE-004", IssueType.CLOSING_MISMATCH, f"“{hit}”是{CLOSING_OWNER[hit]}的结束语，{genre}不应使用" + (f"（可用“{good}”）" if good else ""), block=b, sentence=s))
    # 请示：结尾须有明确请求
    if genre == "请示":
        tail = "".join(s.text for _, s in _last_sentences(ctx, 3))
        if not any(p in tail for p in REQUEST_CLOSINGS):
            b, s = sentences[-1]
            out.append(
                ctx.issue(
                    "GW-GENRE-004",
                    IssueType.CLOSING_MISMATCH,
                    "请示结尾应有明确的请求性结束语（如“妥否，请批示。”“以上请示，请予批复。”）",
                    block=b,
                    sentence=s,
                    severity=Severity.MAJOR,
                    auto_fixable=True,
                    fix_hint={"append_sentence": "妥否，请批示。"},
                )
            )
        for b, s in sentences:
            if any(p in s.text for p in ("特此报告", "特此通知")):
                out.append(ctx.issue("GW-GENRE-004", IssueType.CLOSING_MISMATCH, "请示不使用“特此报告”“特此通知”等结束语", block=b, sentence=s, auto_fixable=True, fix_hint={"replace": "特此报告。", "with": "妥否，请批示。"}))
    # 行文方向与结束语
    if direction in ("上行文",):
        for b, s in sentences:
            hit = next((p for p in DOWNWARD_DIRECTIVES if p in s.text), None)
            if hit:
                out.append(ctx.issue("GW-GENRE-005", IssueType.CLOSING_MISMATCH, f"上行文不应使用要求性用语“{hit}”", block=b, sentence=s))
    if direction == "下行文" and genre not in ("请示", "报告"):
        for b, s in sentences:
            hit = next((p for p in ("妥否", "当否", "请批示") if p in s.text), None)
            if hit:
                out.append(ctx.issue("GW-GENRE-005", IssueType.CLOSING_MISMATCH, f"下行文不应使用请求性用语“{hit}”", block=b, sentence=s))
    # 函：慎用指令口吻
    if genre == "函":
        for b, s in sentences:
            hit = next((p for p in kb.lexicon()["command_tone"] if p in s.text), None)
            if hit:
                out.append(ctx.issue("GW-GENRE-007", IssueType.ADDRESS_MISMATCH, f"函用于不相隶属机关之间，“{hit}”属指令口吻，建议改为商洽、请求语气", block=b, sentence=s))
    # 批复、复函：引来文
    if genre == "批复" or (genre == "函" and ctx.ir.title.endswith("复函")):
        head = "".join(s.text for _, s in sentences[:2])
        kind = "请示" if genre == "批复" else "函"
        if sentences and not (re.search(rf"《[^》]+{kind}》", head) and "收悉" in head):
            b, s = sentences[0]
            out.append(ctx.issue("GW-GENRE-010", IssueType.REQUIRED_MISSING, f"{'批复' if genre == '批复' else '复函'}开头应引用来文标题和发文字号，并写明“收悉”", block=b, sentence=s))
    return out


def check_address_terms(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    lex = kb.lexicon()["address"]
    direction = ctx.direction
    for b, s in ctx.ir.iter_sentences(include_attachments=False):
        if direction in ("上行文", "平行文"):
            hit = next((w for w in lex["down_only"] if w in s.text and w != "你们"), None)
            if hit:
                out.append(ctx.issue("GW-GENRE-006", IssueType.ADDRESS_MISMATCH, f"“{hit}”仅用于下行文；{direction}应使用全称、规范化简称或“贵X”", block=b, sentence=s))
        if direction == "下行文":
            hit = next((w for w in lex["respectful"] if w in s.text), None)
            if hit:
                out.append(ctx.issue("GW-GENRE-006", IssueType.ADDRESS_MISMATCH, f"下行文对下级不使用“{hit}”", block=b, sentence=s, severity=Severity.MINOR))
    return out


def _requested_objects(t: str) -> str:
    """请求动词所在小句中被请求的对象；“用于设备购置、场地改造和人员培训”是经费用途，不是另外的请求事项。"""
    parts = []
    for m in REQUEST_VERB.finditer(t):
        end = min([i for i in (t.find(d, m.end()) for d in "，；。,;") if i >= 0] or [len(t)])
        seg = t[m.start() : end]
        parts.append(re.split(r"用于|用作|主要用于", seg)[0])
    return "；".join(parts)


def detect_request_matters(texts: list[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for t in texts:
        if not REQUEST_VERB.search(t):
            continue
        obj = _requested_objects(t)
        for cat, kws in MATTER_CATEGORIES.items():
            if any(k in obj for k in kws):
                found.setdefault(cat, []).append(t)
    return found


def check_single_matter(ctx: CheckContext) -> list[ReviewIssue]:
    if ctx.ir.genre != "请示":
        return []
    sents = [(b, s) for b, s in ctx.ir.iter_sentences(include_attachments=False) if s.function in ("请求", "") or REQUEST_VERB.search(s.text)]
    found = detect_request_matters([s.text for _, s in sents])
    if len(found) >= 2:
        b, s = next(((b, s) for b, s in sents if REQUEST_VERB.search(s.text)), sents[0])
        cats = "、".join(found)
        return [
            ctx.issue(
                "GW-GENRE-002",
                IssueType.MULTI_MATTER,
                f"请示中检测到多个请求事项（{cats}）。请示应当一文一事，建议拆分为多份请示，或确认它们属于同一事项的组成部分",
                block=b,
                sentence=s,
                needs_human=True,
            )
        ]
    return []


_LEVELS = ("省", "市", "区", "县", "乡", "镇", "街道")


def _looks_lower(organ: str, issuer: str) -> bool:
    """“各区卫生健康局”“各科室”或行政层级低于发文机关的机关，视为下级（只作提示依据，须人工确认）。"""
    if organ.startswith("各"):
        return True
    level = lambda name: max((i for i, w in enumerate(_LEVELS) if re.search(rf"[一-鿿]{{1,8}}{w}(?!委|政府办)", name)), default=-1)  # noqa: E731
    li, lo = level(issuer), level(organ)
    return li >= 0 and lo > li


_LOCAL_ORGAN = re.compile(r"(省|自治区|市(?!场)|自治州|县|盟|旗|[^地]区)")
_NON_ADMIN = re.compile(r"(公司|集团|大学|学院|学校|医院|中心|协会|学会|商会|研究院|研究所)")


def may_issue_order(issuer: str) -> bool:
    """能否以本机关名义发布命令（令）。

    国务院、国家主席及各级人民政府可以发令；国务院部门以部门令公布部门规章（《规章制定程序条例》）。
    地方政府部门、企事业单位和社会组织不得发令。
    """
    issuer = re.sub(r"(文件|令)$", "", issuer.strip())
    if issuer.endswith("人民政府") or issuer.startswith(("国务院", "中华人民共和国", "中央军事委员会")):
        return True
    return not (_LOCAL_ORGAN.search(issuer) or _NON_ADMIN.search(issuer))


def check_issuer_genre(ctx: CheckContext) -> list[ReviewIssue]:
    """只能由特定机关使用的文种（外部文稿检查；起草流程中由权限判断给出同样结论）。"""
    out: list[ReviewIssue] = []
    ir = ctx.ir
    if ctx.genre is not None and ctx.genre.authority_findings:
        return out
    # 发文机关标志优先：命令（令）的落款是签发人职务和姓名，不是机关名称
    m = re.match(r"^(.{2,30}?)关于", ir.title or "")
    issuer = re.sub(r"(文件|令)$", "", ir.header.organ_mark or "") or (ir.signature.organs[0] if ir.signature.organs else "") or (m.group(1) if m else "")
    if not issuer:
        return out
    if ir.genre == "命令（令）" and not may_issue_order(issuer):
        out.append(ctx.issue("GW-GENRE-013", IssueType.AUTHORITY, f"“{issuer}”不是有权发布命令（令）的机关；地方规章以人民政府令公布，政府部门不得以本部门名义发令", field_name="genre", needs_human=True))
    if ir.genre == "议案":
        if not issuer.endswith("人民政府"):
            out.append(ctx.issue("GW-GENRE-014", IssueType.AUTHORITY, f"议案只能由人民政府向同级人民代表大会或其常务委员会提请审议，“{issuer}”不能以本机关名义提出", field_name="genre", needs_human=True))
        bad = [r for r in ir.recipients if "人民代表大会" not in r]
        if bad:
            out.append(ctx.issue("GW-GENRE-014", IssueType.ROUTING, f"议案的主送机关应为同级人民代表大会或其常务委员会（当前：{'、'.join(bad)}）", field_name="recipients", severity=Severity.MAJOR, needs_human=True))
    if ir.genre == "决议" and not (re.search(r"通过", ir.title_note) or any("通过" in s.text for _, s in ir.iter_sentences(include_attachments=False))):
        out.append(ctx.issue("GW-GENRE-015", IssueType.REQUIRED_MISSING, "决议须为会议讨论通过的事项：应在题注或正文中写明通过的会议和日期", field_name="title_note", needs_human=True))
    return out


def check_routing(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    ir = ctx.ir
    if ir.format_type == "plain":
        return out  # 汇报材料、讲话稿等事务文书不是正式行文，不适用主送、签发人等行文规则
    leaders = [r for r in ir.recipients if LEADER_TITLE.search(r.strip())]
    if leaders and ctx.direction in ("上行文", "", "待核实") and not (ctx.genre and any(f.code == "TO_LEADER" for f in ctx.genre.authority_findings)):
        out.append(ctx.issue("GW-ROUTE-004", IssueType.ROUTING, f"主送“{'、'.join(leaders)}”为机关负责人：除上级机关负责人直接交办事项外，不得以本机关名义向上级机关负责人报送公文，应主送上级机关", field_name="recipients", original="、".join(ir.recipients), needs_human=True))
    if ctx.direction == "上行文":
        if len(ir.recipients) > 1:
            out.append(
                ctx.issue(
                    "GW-ROUTE-001",
                    IssueType.ROUTING,
                    f"上行文原则上主送一个上级机关（当前主送 {len(ir.recipients)} 个：{'、'.join(ir.recipients)}），其余可视需要抄送",
                    field_name="recipients",
                    needs_human=True,
                )
            )
        if not ir.header.signers:
            out.append(ctx.issue("GW-ROUTE-012", IssueType.PLACEHOLDER, "上行文应当标注签发人姓名；签发人须由真实签发流程确定，系统不代填", field_name="header.signers"))
        lower = [c for c in ir.imprint.cc if _looks_lower(c, ir.signature.organs[0] if ir.signature.organs else "")]
        if lower:
            out.append(ctx.issue("GW-ROUTE-002", IssueType.ROUTING, f"上行文不抄送下级机关（抄送中有：{'、'.join(lower)}）", field_name="imprint.cc", original="、".join(ir.imprint.cc), needs_human=True))
    if ctx.genre:
        for f in ctx.genre.authority_findings:
            rid = f.basis[0].rule_id if f.basis and f.basis[0].rule_id in _rule_ids() else "GW-ROUTE-005"
            out.append(
                ctx.issue(
                    rid,
                    IssueType.AUTHORITY if f.out_of_authority else IssueType.ROUTING,
                    f.message,
                    field_name="routing",
                    needs_human=True,
                    severity=Severity(f.severity) if f.severity in {s.value for s in Severity} else None,
                )
            )
        for proc in ctx.genre.procedures:
            rid = proc.basis[0].rule_id if proc.basis and proc.basis[0].rule_id in _rule_ids() else "GW-PROC-006"
            out.append(
                ctx.issue(
                    rid,
                    IssueType.PROCEDURE,
                    f"{proc.name}：{proc.trigger}。系统只识别需要衔接的程序，不生成“已通过”结论",
                    field_name="procedures",
                    needs_human=True,
                    severity=Severity.INFO if proc.code in ("PRINCIPAL_SIGN", "UNIT_COLLECTIVE", "UNIT_ETHICS", "UNIT_RESEARCH_INTEGRITY") else None,
                    evidence=[EvidenceRef(kind="procedure", id=proc.code)],
                )
            )
    return out


def _rule_ids() -> set[str]:
    from .registry import RULES

    return set(RULES)


CHECKERS = [check_title, check_closing_and_direction, check_address_terms, check_single_matter, check_issuer_genre, check_routing]
