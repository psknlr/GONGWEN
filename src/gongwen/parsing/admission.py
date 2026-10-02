"""阶段0：材料准入与安全分流（设计 §5 阶段0、§7.1）。

先判断材料能否进入当前系统，再进行任何模型处理：本扫描完全在本地以确定性规则运行，
不允许“先上传模型，再让模型判断是否敏感”。扫描范围覆盖正文、附件、文件名、批注、
修订痕迹、隐藏字段、页眉页脚与文档属性。

三种结论：允许处理 / 需人工确认 / 禁止进入当前环境。无法确认属性的材料先暂停。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..harness.injection import detect as detect_injection
from ..schemas.common import AdmissionDecision, Clearance, EnvironmentRoute
from ..schemas.sources import AdmissionFinding, AdmissionResult
from .base import ParseResult

# 国家秘密标志：密级标注格式（如“机密★1年”“秘密★”）或独立成行的密级、密级字段
_SECRET_MARKS = [
    re.compile(r"(绝密|机密|秘密)\s*★"),
    re.compile(r"(?m)^\s*(绝\s*密|机\s*密|秘\s*密)\s*$"),
    re.compile(r"密级\s*[：:]\s*(绝密|机密|秘密)"),
    re.compile(r"(属于|定为|标定为)(绝密|机密|秘密)级?(国家秘密)?"),
]
_SECRET_FILENAME = re.compile(r"(绝密|机密|秘密|涉密)")
_SOFT_SECRET = re.compile(r"涉密(文件|材料|信息|事项|项目|人员)")
_WORK_SECRET = [
    re.compile(r"工作秘密"),
]
_INTERNAL = [
    re.compile(r"内部(资料|文件|材料|刊物|传阅|使用)"),
    re.compile(r"仅(限|供)内部"),
    re.compile(r"不得(外传|公开|对外)"),
    re.compile(r"注意保存"),
    re.compile(r"未经(批准|同意|授权)不得(公开|转发|复制)"),
    re.compile(r"限(本单位|内部)(阅|传阅|使用)"),
]
_SENSITIVE = [
    re.compile(r"敏感(信息|数据|材料)"),
    re.compile(r"不予公开"),
    re.compile(r"依申请公开"),
]
_ID_CARD = re.compile(r"(?<!\d)(\d{6})(19|20)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(\d{3})([\dXx])(?!\d)")
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_BANK = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"


def _valid_id(s: str) -> bool:
    if len(s) != 18:
        return False
    total = sum(int(c) * w for c, w in zip(s[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == s[17].upper()


def _luhn(s: str) -> bool:
    digits = [int(c) for c in s][::-1]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class ScanInput:
    material_id: str
    filename: str
    parsed: ParseResult
    declared: Clearance | None = None


def _scan_text(text: str, where: str, findings: list[AdmissionFinding]) -> None:
    for rx in _SECRET_MARKS:
        m = rx.search(text)
        if m:
            findings.append(AdmissionFinding(code="SECRET_MARK", detail=f"发现国家秘密标志“{m.group(0).strip()}”", where=where, clearance=Clearance.CLASSIFIED))
            break
    m = _SOFT_SECRET.search(text)
    if m:
        findings.append(AdmissionFinding(code="SECRET_MENTION", detail=f"提及“{m.group(0)}”，需确认材料本身是否涉密", where=where, clearance=Clearance.UNKNOWN))
    for rx in _WORK_SECRET:
        m = rx.search(text)
        if m:
            findings.append(AdmissionFinding(code="WORK_SECRET_MARK", detail=f"发现“{m.group(0)}”标注或表述", where=where, clearance=Clearance.WORK_SECRET))
            break
    for rx in _INTERNAL:
        m = rx.search(text)
        if m:
            findings.append(AdmissionFinding(code="INTERNAL_MARK", detail=f"发现内部材料标注“{m.group(0)}”", where=where, clearance=Clearance.INTERNAL))
            break
    for rx in _SENSITIVE:
        m = rx.search(text)
        if m:
            findings.append(AdmissionFinding(code="SENSITIVE_MARK", detail=f"发现“{m.group(0)}”", where=where, clearance=Clearance.SENSITIVE))
            break
    ids = [m.group(0) for m in _ID_CARD.finditer(text) if _valid_id(m.group(0))]
    if ids:
        findings.append(AdmissionFinding(code="PII_ID_CARD", detail=f"发现 {len(ids)} 个居民身份证号码", where=where, clearance=Clearance.SENSITIVE))
    mobiles = _MOBILE.findall(text)
    if len(mobiles) >= 3:
        findings.append(AdmissionFinding(code="PII_PHONE", detail=f"发现 {len(mobiles)} 个手机号码", where=where, clearance=Clearance.SENSITIVE))
    banks = [b for b in _BANK.findall(text) if _luhn(b)]
    if banks:
        findings.append(AdmissionFinding(code="PII_BANK", detail=f"发现 {len(banks)} 个疑似银行卡号", where=where, clearance=Clearance.SENSITIVE))
    emails = _EMAIL.findall(text)
    if len(emails) >= 5:
        findings.append(AdmissionFinding(code="PII_EMAIL", detail=f"发现 {len(emails)} 个电子邮箱", where=where, clearance=Clearance.SENSITIVE))


def scan(inp: ScanInput, route: EnvironmentRoute, accept_internal: bool = False) -> AdmissionResult:
    findings: list[AdmissionFinding] = []
    reasons: list[str] = []
    if _SECRET_FILENAME.search(inp.filename):
        findings.append(AdmissionFinding(code="SECRET_FILENAME", detail=f"文件名含“{_SECRET_FILENAME.search(inp.filename).group(0)}”", where="文件名", clearance=Clearance.UNKNOWN))
    _scan_text(inp.parsed.visible_text(), "正文", findings)
    for h in inp.parsed.hidden:
        _scan_text(h.text, h.where, findings)
    hidden_kinds = sorted({h.where for h in inp.parsed.hidden if h.where in ("批注", "修订痕迹", "隐藏文字", "隐藏工作表", "隐藏行列", "单元格批注")})
    if hidden_kinds:
        findings.append(
            AdmissionFinding(code="HIDDEN_CONTENT", detail=f"包含{'、'.join(hidden_kinds)}，可能含未显示的敏感内容", where="、".join(hidden_kinds), clearance=Clearance.UNKNOWN)
        )
    inj = detect_injection(inp.parsed.visible_text())
    if inj:
        findings.append(
            AdmissionFinding(code="INJECTION", detail=f"疑似提示词注入语句 {len(inj)} 处（如：{inj[0].excerpt}）；仅作为资料，不作为指令", where="正文", clearance=Clearance.PUBLIC)
        )

    detected = Clearance.PUBLIC
    for f in findings:
        if f.clearance in (Clearance.UNKNOWN,):
            continue
        if f.clearance.rank > detected.rank:
            detected = f.clearance
    if any(f.code == "SECRET_MARK" for f in findings):
        detected = Clearance.CLASSIFIED

    declared = inp.declared
    effective = detected
    if declared is not None and declared.rank > effective.rank and declared != Clearance.UNKNOWN:
        effective = declared

    # ---- 判定
    if detected == Clearance.CLASSIFIED or declared == Clearance.CLASSIFIED:
        reasons.append("涉密材料不得进入本系统；涉密应用须按保密主管要求另行建设和管理")
        decision = AdmissionDecision.FORBID
        effective = Clearance.CLASSIFIED
    elif route == EnvironmentRoute.CLASSIFIED:
        reasons.append("涉密应用不在本系统能力承诺范围内")
        decision = AdmissionDecision.FORBID
    elif route == EnvironmentRoute.PUBLIC_DEV and effective.rank > Clearance.PUBLIC.rank:
        reasons.append(f"公开材料研发版不接入未公开工作材料（检测/申报属性：{effective.value}）")
        decision = AdmissionDecision.FORBID
    else:
        decision = AdmissionDecision.ALLOW
        if declared is None:
            reasons.append("材料属性未申报：无法确认属性的材料先暂停，请经办人确认来源与属性")
            decision = AdmissionDecision.NEED_CONFIRM
            effective = Clearance.UNKNOWN if detected == Clearance.PUBLIC else detected
        if declared is not None and detected.rank > declared.rank:
            reasons.append(f"检测到的属性（{detected.value}）高于申报属性（{declared.value}），需人工确认")
            decision = AdmissionDecision.NEED_CONFIRM
        if any(f.code in ("SECRET_MENTION", "SECRET_FILENAME") for f in findings):
            reasons.append("材料提及涉密或文件名含密级字样，需确认材料本身不涉密")
            decision = AdmissionDecision.NEED_CONFIRM
        if hidden_kinds:
            reasons.append("材料含批注、修订痕迹或隐藏内容，需确认可以一并处理")
            decision = AdmissionDecision.NEED_CONFIRM
        if any(f.code.startswith("PII_") for f in findings):
            reasons.append("材料含个人信息，需确认处理必要性并按最小化原则处理")
            decision = AdmissionDecision.NEED_CONFIRM
        if route == EnvironmentRoute.UNIT_APPROVED and effective in (Clearance.INTERNAL, Clearance.SENSITIVE, Clearance.WORK_SECRET):
            if not accept_internal:
                reasons.append("单位业务环境尚未在配置中开启内部材料处理（environment.accept_internal_materials）")
                decision = AdmissionDecision.FORBID
            elif decision == AdmissionDecision.ALLOW and declared is None:
                decision = AdmissionDecision.NEED_CONFIRM
    if decision == AdmissionDecision.ALLOW and not reasons:
        reasons.append("申报属性与扫描结果一致，未发现准入风险")
    return AdmissionResult(
        material_id=inp.material_id,
        filename=inp.filename,
        decision=decision,
        detected_clearance=effective,
        declared_clearance=declared,
        findings=findings,
        reasons=reasons,
    )


def aggregation_risk(results: list[AdmissionResult]) -> str | None:
    """公开材料也可能因汇聚、关联产生额外风险：准入判断不能完全按单份文件孤立进行。"""
    pii = sum(1 for r in results for f in r.findings if f.code.startswith("PII_"))
    internal = sum(1 for r in results if r.detected_clearance in (Clearance.INTERNAL, Clearance.SENSITIVE, Clearance.WORK_SECRET))
    if pii >= 3 or (pii >= 1 and internal >= 2) or len(results) >= 20:
        return "多份材料汇聚后可能形成更高敏感度的信息集合，请人工评估汇聚风险"
    return None
