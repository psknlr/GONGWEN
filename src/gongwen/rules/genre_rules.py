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


def _last_sentences(ctx: CheckContext, n: int = 2):
    sents = list(ctx.ir.iter_sentences(include_attachments=False))
    return sents[-n:]


def check_title(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    ir = ctx.ir
    title = ir.title.strip()
    bare = title.rstrip("。，；：！？.,;:!? ")  # 末尾标点另报 GW-FMT-009，不影响文种判断
    if not title:
        return [ctx.issue("GW-GENRE-008", IssueType.REQUIRED_MISSING, "缺少标题", field_name="title")]
    if title[-1] in "。，；：！？.,;:":
        out.append(ctx.issue("GW-FMT-009", IssueType.FORMAT, "标题末尾不用标点符号", field_name="title", original=title, auto_fixable=True, fix_hint={"strip_end": title[-1]}))
    if any(w in title for w in ("紧急", "特急", "加急")):
        out.append(ctx.issue("GW-GENRE-009", IssueType.FORMAT, "紧急程度应作为版头要素标注，不写入标题", field_name="title", original=title))
    g = kb.genre(ir.genre)
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
            if name and name not in title and not (short and short in title) and ir.format_type != "jiyao":
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


def check_routing(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    ir = ctx.ir
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


CHECKERS = [check_title, check_closing_and_direction, check_address_terms, check_single_matter, check_routing]
