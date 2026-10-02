"""要素级格式检查（作用于 DocumentIR；版面几何与实际渲染检查见 layout.render_check）。"""

from __future__ import annotations

import re

from ..schemas.review import IssueType, ReviewIssue
from .base import CheckContext

DOC_NO_RE = re.compile(r"^[一-鿿]{1,12}〔\d{4}〕[1-9]\d*号$")
DATE_OK_RE = re.compile(r"^\d{4}年[1-9]\d?月[1-9]\d?日$")


def _is_placeholder(v: str) -> bool:
    return not v or v.startswith("【")


def check_header_fields(ctx: CheckContext) -> list[ReviewIssue]:
    out: list[ReviewIssue] = []
    h = ctx.ir.header
    if not _is_placeholder(h.doc_number):
        if not DOC_NO_RE.match(h.doc_number):
            hint = []
            if re.search(r"[\[\(（［]\d{4}[\]\)）］]", h.doc_number):
                hint.append("年份用六角括号〔〕")
            if "第" in h.doc_number:
                hint.append("顺序号不加“第”字")
            if re.search(r"〕0\d", h.doc_number):
                hint.append("顺序号不编虚位")
            out.append(ctx.issue("GW-FMT-001", IssueType.FORMAT, "发文字号格式不规范：" + ("、".join(hint) or "应为“机关代字〔年份〕顺序号号”"), field_name="header.doc_number", original=h.doc_number))
    if h.copy_no and not re.fullmatch(r"\d{6}", h.copy_no):
        out.append(ctx.issue("GW-FMT-007", IssueType.FORMAT, "份号应为6位阿拉伯数字（如000001）", field_name="header.copy_no", original=h.copy_no))
    if h.secrecy:
        out.append(ctx.issue("GW-SEC-003", IssueType.SENSITIVE, "文稿标注了密级：涉密公文不在本系统处理范围，应转入符合保密要求的环境办理", field_name="header.secrecy", original=h.secrecy, needs_human=True))
    d = ctx.ir.signature.date
    if not _is_placeholder(d) and not DATE_OK_RE.match(d):
        out.append(ctx.issue("GW-FMT-002", IssueType.FORMAT, "成文日期应用阿拉伯数字将年、月、日标全，月、日不编虚位（如“2026年8月1日”）", field_name="signature.date", original=d))
    pd = ctx.ir.imprint.print_date
    if not _is_placeholder(pd) and not DATE_OK_RE.match(pd.replace("印发", "")):
        out.append(ctx.issue("GW-FMT-002", IssueType.FORMAT, "印发日期应用阿拉伯数字标全年月日，月、日不编虚位", field_name="imprint.print_date", original=pd))
    for r in ctx.ir.recipients:
        if r.endswith(("：", ":", "，", "。")):
            out.append(ctx.issue("GW-FMT-005", IssueType.FORMAT, "主送机关名称内不含标点，最后一个名称后的全角冒号由排版自动添加", field_name="recipients", original=r, auto_fixable=True, fix_hint={"op": "strip_recipient_punct"}))
    if ctx.ir.genre in ("请示", "报告") and ctx.ir.format_type == "general" and not ctx.ir.recipients:
        out.append(ctx.issue("GW-ROUTE-001", IssueType.REQUIRED_MISSING, "上行文应有主送机关", field_name="recipients", needs_human=True))
    return out


CHECKERS = [check_header_fields]
