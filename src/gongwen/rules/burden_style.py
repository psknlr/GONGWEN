"""发文必要性与基层负担检查、段落功能检查、空泛表述检查（设计 §2.3、§6.4）。

一个成熟的公文智能体，应当能够提出“这件事可能不需要再发一份文件”，
而不只是不断生成新文件。对“进一步加强”“持续提升”等表述，检查后面是否有实际内容，
而不是机械删除这些词。
"""

from __future__ import annotations

import re

from ..knowledge import kb
from ..knowledge.retrieval import coverage
from ..schemas.common import EvidenceRef, Severity
from ..schemas.review import IssueType, ReviewIssue
from .base import CheckContext
from .textutil import extract_numbers

FUNCTION_CUES = {
    "依据": ("根据", "依据", "按照", "依照", "为贯彻", "为落实", "遵照"),
    "事实": ("截至", "共有", "现有", "累计", "已", "完成", "建成", "开展了", "统计", "数据显示"),
    "分析": ("原因", "主要是", "由于", "问题", "不足", "短板", "制约", "表现在"),
    "措施": ("建立", "开展", "推进", "实施", "落实", "组织", "完善", "加强", "拟", "安排", "设立", "制定", "制订"),
    "条件": ("如", "若", "确有", "必要时", "原则上", "符合条件", "除", "经批准"),
    "要求": ("请于", "请各", "务必", "要求", "报送", "联系人", "联系电话", "须", "应当", "必须"),
    "请求": ("妥否", "请批示", "请予", "申请", "恳请", "拟请"),
    "结语": ("特此", "此复", "以上"),
}
_CONTENT_RE = re.compile(r"\d|《|“|[A-Za-z]")


def classify_function(text: str) -> list[str]:
    return [fn for fn, cues in FUNCTION_CUES.items() if any(c in text for c in cues)]


def _strip_empty(text: str) -> str:
    for p in kb.lexicon()["empty_phrases"]:
        text = text.replace(p, "")
    return re.sub(r"[，。；、：\s]", "", text)


def check_empty_phrases(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    phrases = kb.lexicon()["empty_phrases"]
    for b, s in ctx.ir.iter_sentences():
        hits = [p for p in phrases if p in s.text]
        if not hits:
            continue
        rest = _strip_empty(s.text)
        # 去掉空泛表述后若只剩很少的实际内容（无数字、无具体对象），或同一句堆砌三处以上表态，提示精简
        if (len(rest) < 12 and not _CONTENT_RE.search(rest)) or len(hits) >= 3:
            out.append(
                ctx.issue(
                    "GW-STYLE-001",
                    IssueType.EMPTY_PHRASE,
                    f"“{'”“'.join(hits)}”之后缺少实际内容（谁做、做什么、何时完成），删去后不影响理解和执行的，建议精简或补充具体措施",
                    block=b,
                    sentence=s,
                )
            )
    return out


def check_paragraph_function(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    for _, b in ctx.ir.iter_blocks():
        if b.kind != "paragraph" or not b.sentences:
            continue
        text = b.text()
        fns = classify_function(text) or [s.function for s in b.sentences if s.function]
        if not fns:
            out.append(ctx.issue("GW-STYLE-002", IssueType.PARAGRAPH_FUNCTION, "该段未承担明确功能（说明依据、陈述事实、分析问题、提出措施、说明条件或明确办理要求），请确认是否需要保留", block=b, sentence=b.sentences[0]))
    return out


def _allowed_sources(ctx: CheckContext) -> str:
    parts = []
    if ctx.task:
        parts.append(ctx.task.request_text)
        parts += list(ctx.task.constraints.values())
    if ctx.policies:
        parts += [e.quote for e in ctx.policies.items]
    if ctx.outline:
        parts += [m.text for m in ctx.outline.measures if m.confirmed or m.origin != "系统建议"]
    if ctx.sources:
        parts += [u.text for u in ctx.sources.units if u.kind != "comment"]
    return "\n".join(parts)


def check_burden(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    if not ctx.features.burden_check:
        return out
    if ctx.direction == "上行文":
        return out  # 减负规定针对上级向基层提出的报送、考核、留痕要求；上行文不适用
    lex = kb.lexicon()
    terms = [t for group in lex["burden_terms"].values() for t in group]
    allowed = _allowed_sources(ctx)
    for b, s in ctx.ir.iter_sentences():
        text = re.sub(r"以上报告|以上报", "", s.text)  # “以上报告”中的“上报”不是报送要求
        hits = [t for t in terms if t in text]
        if not hits:
            continue
        freq = [f for f in lex["frequency"] if f in s.text]
        unsupported = [t for t in hits if t not in allowed]
        if unsupported:
            out.append(
                ctx.issue(
                    "GW-BURDEN-001",
                    IssueType.BURDEN,
                    f"新增{'、'.join(unsupported)}要求{('（频次：' + '、'.join(freq) + '）') if freq else ''}在现有依据和任务说明中未出现，请确认是否确需新增；能共享获得的数据不应再要求报送",
                    block=b,
                    sentence=s,
                    severity=Severity.MAJOR,
                    needs_human=True,
                )
            )
        elif freq and any(t in ("报送", "上报", "填报", "月报", "周报", "日报") for t in hits):
            out.append(ctx.issue("GW-BURDEN-001", IssueType.BURDEN, f"定期报送要求（{'、'.join(freq)}）会增加基层负担，请说明必要性", block=b, sentence=s, severity=Severity.INFO))
    # 文件篇幅：减负规定“一般不超过5000字/4000字”属条件性要求，只做提示
    body_len = len(ctx.ir.body_text(include_attachments=False))
    if body_len > 4000 and ctx.ir.genre in ("通知", "意见", "决定"):
        out.append(
            ctx.issue(
                "GW-BURDEN-001",
                IssueType.BURDEN,
                f"正文约 {body_len} 字。地方和部门文件一般不超过5000字，部署专项工作或者具体任务的一般不超过4000字（《整治形式主义为基层减负若干规定》），请评估是否精简",
                field_name="body",
                severity=Severity.INFO,
                evidence=[EvidenceRef(kind="policy", id="jianfu-guiding-2024")],
            )
        )
    return out


def check_necessity(ctx: CheckContext) -> list[ReviewIssue]:
    """已有文件可能已能解决问题：与材料中的既有文件高度重合时提示。"""
    out: list[ReviewIssue] = []
    if not ctx.sources:
        return out
    body = ctx.ir.body_text()
    if len(body) < 80:
        return out
    by_material: dict[str, str] = {}
    for u in ctx.sources.units:
        by_material.setdefault(u.material_id, "")
        by_material[u.material_id] += u.text
    for mid, text in by_material.items():
        mat = next((m for m in ctx.sources.materials if m.material_id == mid), None)
        if mat is None or mat.role not in ("existing_doc", "material") or len(text) < 200:
            continue
        cov = coverage(body, text)
        if cov > 0.75 and mat.role == "existing_doc":
            out.append(
                ctx.issue(
                    "GW-BURDEN-002",
                    IssueType.NECESSITY,
                    f"本稿与已有文件“{mat.filename}”内容高度重合（{cov:.0%}）。如已有文件能够解决问题，可能不需要再发一份文件",
                    field_name="body",
                    evidence=[EvidenceRef(kind="material", id=mid)],
                    needs_human=True,
                )
            )
    return out


def check_sensitive_and_injection(ctx: CheckContext) -> list[ReviewIssue]:
    from ..harness.injection import detect

    out: list[ReviewIssue] = []
    id_re = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
    phone_re = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
    for b, s in ctx.ir.iter_sentences():
        if id_re.search(s.text):
            out.append(ctx.issue("GW-SEC-001", IssueType.SENSITIVE, "文稿中出现身份证号码，非必要不得写入", block=b, sentence=s, needs_human=True))
        if phone_re.search(s.text) and "联系电话" not in s.text and "联系人" not in s.text:
            out.append(ctx.issue("GW-SEC-001", IssueType.SENSITIVE, "文稿中出现手机号码，请确认是否必要", block=b, sentence=s, severity=Severity.MINOR))
        hits = detect(s.text)
        if hits:
            out.append(ctx.issue("GW-SEC-002", IssueType.INJECTION, f"文稿中出现疑似来自资料的指令性语句（{hits[0].reason}），不应写入正文", block=b, sentence=s, needs_human=True))
    return out


def check_placeholders(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    process_fields = ("发文字号", "成文日期", "签发", "印发", "份号")
    for b, s in ctx.ir.iter_sentences():
        if s.has_placeholder:
            m = re.search(r"【待[^】]*】", s.text)
            label = m.group(0) if m else "【待补】"
            if any(f in label for f in process_fields):
                out.append(ctx.issue("GW-PH-002", IssueType.PLACEHOLDER, f"{label}由真实办理流程填写", block=b, sentence=s))
            else:
                out.append(ctx.issue("GW-PH-001", IssueType.PLACEHOLDER, f"存在待补内容{label}，送审前须补齐或删除", block=b, sentence=s, needs_human=True))
    return out


def check_length_vs_numbers(ctx: CheckContext) -> list[ReviewIssue]:
    """数字密集但无引用的句子：提示补充证据关联（便于审阅侧栏追溯）。"""
    out: list[ReviewIssue] = []
    for b, s in ctx.ir.iter_sentences():
        if len(extract_numbers(s.text)) >= 3 and not s.refs:
            out.append(ctx.issue("GW-FACT-002", IssueType.UNSOURCED_NUMBER, "该句包含多个数据但未关联任何证据", block=b, sentence=s, severity=Severity.MINOR))
    return out


CHECKERS = [check_empty_phrases, check_paragraph_function, check_burden, check_necessity, check_sensitive_and_injection, check_placeholders, check_length_vs_numbers]
