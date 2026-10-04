"""技能5 事实账本：整理数据、事实、计算值和待核事项，而不是直接拼接材料。

状态：已核实事实 / 材料记载 / 计算结果 / 拟议内容 / 已批准事项 / 未知或冲突。
数字由程序核验求和、比例、单位、口径与时点：算术正确不等于统计口径正确，
两张表覆盖不同项目范围时，不能直接合并。
"""

from __future__ import annotations

import re
from collections import defaultdict

from ..harness.injection import UNTRUSTED_NOTICE, wrap_untrusted
from ..harness.injection import detect as detect_injection
from ..llm.base import ChatMessage, ModelRefused, ModelUnavailable
from ..rules.semantics import progress_of
from ..rules.textutil import COUNT_UNITS, MONEY_UNITS, extract_numbers, split_sentences
from ..knowledge.retrieval import coverage
from ..schemas.common import Locator
from ..schemas.facts import CalcCheck, Fact, FactConflict, FactLedger, FactStatus, Formula, Progress, Verification
from ..schemas.sources import SourceBundle, SourceUnit
from ..schemas.state import Stage
from ..schemas.task import TaskSpec
from .base import Skill, SkillContext

_DELIMS = "，。；、：:（）()！？\n"
_LEAD_WORDS = (
    "共计", "合计", "总计", "累计", "共有", "现有", "共", "约", "达到", "达", "为", "有", "拟建设", "拟新建", "拟", "建设", "新增",
    "安排", "申请", "需要", "需", "计划", "完成", "投入", "其中", "全年", "已", "已经", "建成", "实现", "预计", "总额", "金额",
)
_TOTAL_RE = re.compile(r"^(合计|总计|共计|小计)$")
_UNIT_IN_HEADER = re.compile(r"[（(]\s*(万元|亿元|元|千元|个|家|人|项|%|％|台|套|次|平方米|公里)\s*[）)]")
_AS_OF_RE = re.compile(r"截至\s*(\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?(?:底|末)?)")
_CALIBER_RE = re.compile(r"[（(]((?:不含|含|仅统计|仅含|按)[^）)]{1,30})[）)]|(仅统计[^，。；]{1,30})|(按[^，。；]{1,12}口径)")
_TITLE_LIKE = re.compile(r"^(关于.{2,60}(说明|报告|请示|通知|方案|函|纪要|意见|汇报|总结|计划|测算表|明细表)|[^。！？；]{1,24})$")
_SUBSTANTIVE = re.compile(
    r"(问题|不足|困难|短板|制约|隐患|滞后|缺口|原因|主要是|建立|开展|推进|实施|落实|组织|完善|建设|采购|购置|培训|改造|负责|牵头|要求|决定|议定|同意|^为"
    r"|应当|必须|务必|须于|须在|要在|要于|确保|不得|严禁|完成|报送|提交)"
)
_MEETING_UNDECIDED = re.compile(r"(未作决定|未作出决定|未决定|未议定|未形成(决定|意见|结论)|不同意|暂不|暂缓|待研究|再研究|另行研究|进一步研究|需进一步|会后研究|未达成一致)")
_PROCESS_FIELDS = re.compile(r"(发文字号|文号|成文日期|签发人|印发日期|份号|落款日期)")


def request_clauses(text: str) -> list[str]:
    """把需求拆成小句；去掉“起草一份……”等办理指令和办理流程字段，只保留可能是事实陈述的小句。"""
    out = []
    for c in re.split(r"[，,；;。！!？?\n]", text):
        c = c.strip()
        if not c or _PROCESS_FIELDS.search(c):
            continue
        if re.match(r"^(请|麻烦|帮我|帮忙|需要)?(起草|写|拟写|撰写|草拟|准备|整理|形成)", c):
            continue
        out.append(c + "。")
    return out


_CELL_PATH = re.compile(r"^(?:(?P<sheet>[^!]+)!(?P<col>[A-Z]{1,3})(?P<row>\d+)|table(?P<t>\d+)\.r(?P<r>\d+)\.c(?P<c>\d+))$")


def _num(s: str) -> float | None:
    s = s.replace(",", "").replace("，", "").strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        return float(s)
    return None


def _col_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def infer_attribute(sentence: str, start: int, end: int, unit: str) -> str:
    left = sentence[:start]
    cut = max(left.rfind(d) for d in _DELIMS)
    seg = left[cut + 1 :].strip()
    changed = True
    while changed and seg:
        changed = False
        # 主语自称与时点前缀不属于“属性”（“我委已建成示范点”“2026年拟新建示范点”的属性都是“示范点”）
        stripped = re.sub(r"^(我委|我局|我院|我校|我所|我单位|本单位|全市|全省|全区|今年|去年|当年|截至|截止|\d{4}年(\d{1,2}月)?(\d{1,2}日)?(底|末)?)", "", seg)
        if stripped != seg and stripped:
            seg, changed = stripped, True
        for w in _LEAD_WORDS:
            if seg.endswith(w):
                seg = seg[: -len(w)]
                changed = True
            if seg.startswith(w) and len(seg) > len(w):
                seg = seg[len(w) :]
                changed = True
    seg = re.sub(r"^(我委|我局|我院|我校|我所|我单位|本单位|全市|全省|全区|今年|去年|当年)", "", seg).strip()
    if len(seg) >= 2:
        return seg[-12:]
    right = sentence[end:]
    m = re.match(r"([一-鿿]{2,8})", right)
    if m:
        return m.group(1)
    return {"money": "金额", "percent": "比例"}.get("money" if unit in MONEY_UNITS else ("percent" if unit in ("%", "％") else ""), "数量")


class FactLedgerSkill(Skill):
    name = "gongwen-fact-ledger"
    number = 5
    title = "事实账本构建"
    stage = Stage.EVIDENCE
    channel_name = "ledger"
    allowed_tools = ("gongwen_material_peek",)
    output_artifact = "fact_ledger"

    def run(self, sc: SkillContext, spec: TaskSpec, bundle: SourceBundle) -> FactLedger:
        ledger = FactLedger()
        mats = {m.material_id: m for m in bundle.materials}
        notes_by_table: dict[str, str] = {t.table_id: "；".join(t.notes) for t in bundle.tables if t.notes}
        table_units: dict[str, list[SourceUnit]] = defaultdict(list)
        for u in bundle.units:
            if u.kind in ("table_cell", "sheet_cell") and u.attrs.get("table"):
                table_units[u.attrs["table"]].append(u)
                continue
            if u.kind == "comment":
                continue
            self._text_facts(sc, ledger, u, mats.get(u.material_id))
        for tid, units in table_units.items():
            mat = mats.get(units[0].material_id)
            self._table_facts(sc, ledger, tid, units, mat, notes_by_table.get(tid, ""))
        # 需求文本中给出的数字（用户陈述）：只取数字所在的小句，办理流程字段（发文字号、成文日期等）不是事实
        req_loc = Locator(material_id="request", kind="request", path="request", excerpt=spec.request_text[:60])
        for s in request_clauses(spec.request_text):
            for n in extract_numbers(s):
                ledger.facts.append(
                    Fact(
                        fact_id=sc.ids.next("F"),
                        statement=s,
                        attribute=infer_attribute(s, n.start, n.end, n.unit),
                        value=n.value,
                        unit=n.unit,
                        kind=n.kind,
                        status=FactStatus.PROPOSED if progress_of(s) == Progress.PLANNED else FactStatus.RECORDED,
                        progress=progress_of(s),
                        sources=[req_loc],
                        tags=["request"],
                    )
                )
        if sc.model_available("light"):
            self._model_extract(sc, ledger, bundle)
        if sc.features.fact_ledger:
            self._conflicts(sc, ledger)
            self._unit_checks(sc, ledger)
        unknown = [f"{g.field}：{g.description}" for g in spec.gaps if g.impact.value == "影响重要事实"]
        ledger.unknowns.extend(unknown)
        sc.note(
            "skill.fact_ledger",
            {
                "facts": len(ledger.facts),
                "by_status": {s.value: len(ledger.by_status(s)) for s in FactStatus},
                "conflicts": len(ledger.conflicts),
                "calc_checks": len(ledger.calc_checks),
                "calc_failures": sum(1 for c in ledger.calc_checks if not c.ok),
            },
        )
        return ledger

    # ------------------------------------------------------------------
    def _status_for(self, mat, progress: Progress) -> tuple[FactStatus, list[str], Verification | None]:
        tags: list[str] = []
        verification = None
        if mat is not None and mat.role == "example":
            tags.append("example")
        if mat is not None and mat.role == "approval_candidate":
            tags.append("approval_candidate")
        if mat is not None and mat.role == "meeting_record":
            tags.append("meeting_record")
        if progress == Progress.PLANNED:
            return FactStatus.PROPOSED, tags, None
        if mat is not None and mat.authoritative and "example" not in tags:
            verification = Verification(method="来源核对：经人工确认的权威来源", by="system")
            return FactStatus.VERIFIED, tags, verification
        return FactStatus.RECORDED, tags, verification

    def _text_facts(self, sc: SkillContext, ledger: FactLedger, u: SourceUnit, mat) -> None:
        if u.kind == "heading" or _TITLE_LIKE.match(u.text.strip()):
            return  # 材料标题、层次标题不是事实陈述
        for s in split_sentences(u.text):
            if detect_injection(s):
                # 资料中的指令性语句只是数据：不作为事实，也不让其中的数字进入账本
                ledger.unknowns.append(f"{u.locator.label()}：疑似指令性语句，未作为事实（{s[:30]}）")
                continue
            nums = extract_numbers(s)
            prog = progress_of(s)
            status, tags, ver = self._status_for(mat, prog)
            caliber = ""
            m = _CALIBER_RE.search(s)
            if m:
                caliber = next(g for g in m.groups() if g)
            as_of = ""
            m = _AS_OF_RE.search(s)
            if m:
                as_of = m.group(1)
            loc = Locator(material_id=u.material_id, kind=u.kind, path=u.locator.path, excerpt=s[:80])
            if "meeting_record" in tags:
                # 先看否定与待定语境：“会议未作决定”“暂不同意”“需进一步研究”不是议定事项
                if _MEETING_UNDECIDED.search(s):
                    tags = tags + ["meeting:discussion"]
                elif any(w in s for w in ("决定", "议定", "同意", "明确", "确定")):
                    tags = tags + ["meeting:decided"]
                elif any(w in s for w in ("讨论", "建议", "提出", "认为", "发言")):
                    tags = tags + ["meeting:discussion"]
            if nums:
                for n in nums:
                    ledger.facts.append(
                        Fact(
                            fact_id=sc.ids.next("F"),
                            statement=s,
                            attribute=infer_attribute(s, n.start, n.end, n.unit),
                            value=n.value,
                            unit=n.unit,
                            kind=n.kind,
                            as_of=as_of,
                            caliber=caliber,
                            status=status,
                            progress=prog,
                            sources=[loc],
                            verification=ver,
                            tags=tags,
                        )
                    )
            elif len(s) >= 8 and (prog != Progress.NONE or "meeting_record" in tags or "approval_candidate" in tags or _SUBSTANTIVE.search(s)) and u.kind != "heading":
                ledger.facts.append(
                    Fact(
                        fact_id=sc.ids.next("F"),
                        statement=s,
                        attribute=s[:16],
                        kind="text",
                        status=status,
                        progress=prog,
                        sources=[loc],
                        verification=ver,
                        tags=tags,
                    )
                )

    def _table_facts(self, sc: SkillContext, ledger: FactLedger, tid: str, units: list[SourceUnit], mat, notes: str) -> None:
        grid: dict[tuple[int, int], SourceUnit] = {}
        for u in units:
            m = _CELL_PATH.match(u.locator.path)
            if not m:
                continue
            if m.group("sheet"):
                grid[(int(m.group("row")), _col_index(m.group("col")))] = u
            else:
                grid[(int(m.group("r")), int(m.group("c")))] = u
        if not grid:
            return
        rows = sorted({r for r, _ in grid})
        cols = sorted({c for _, c in grid})
        header_row = rows[0]
        header = {c: grid[(header_row, c)].text for c in cols if (header_row, c) in grid}
        label_col = cols[0]
        status, tags, ver = self._status_for(mat, Progress.NONE)
        caliber = notes
        col_inputs: dict[int, list[Fact]] = defaultdict(list)
        totals: dict[int, list[Fact]] = defaultdict(list)
        for r in rows[1:]:
            label_u = grid.get((r, label_col))
            label = label_u.text if label_u else f"第{r}行"
            is_total = bool(_TOTAL_RE.match(label.strip()))
            for c in cols:
                if c == label_col or (r, c) not in grid:
                    continue
                u = grid[(r, c)]
                val = _num(u.text)
                if val is None:
                    continue
                head = header.get(c, f"第{c}列")
                mu = _UNIT_IN_HEADER.search(head)
                unit = mu.group(1) if mu else ""
                kind = "money" if unit in MONEY_UNITS else ("percent" if unit in ("%", "％") or "率" in head or "占比" in head else ("count" if unit in COUNT_UNITS else "plain"))
                attr_head = _UNIT_IN_HEADER.sub("", head).strip()
                f = Fact(
                    fact_id=sc.ids.next("F"),
                    statement=f"{label}{attr_head}{u.text}{unit}",
                    attribute=f"{label}·{attr_head}",
                    value=val,
                    unit=unit or ("%" if kind == "percent" else ""),
                    kind=kind,
                    caliber=caliber,
                    status=status,
                    sources=[Locator(material_id=u.material_id, kind=u.kind, path=u.locator.path, excerpt=f"{label}｜{head}｜{u.text}")],
                    verification=ver,
                    tags=tags + ["table"],
                )
                ledger.facts.append(f)
                (totals if is_total else col_inputs)[c].append(f)
        # 合计核验 + 计算结果
        for c, inputs in col_inputs.items():
            head = header.get(c, "")
            if not inputs or any(k in head for k in ("率", "占比", "%", "％", "单价", "序号", "年份")):
                continue
            total_val = round(sum(float(f.value) for f in inputs), 6)
            unit = inputs[0].unit
            attr_head = _UNIT_IN_HEADER.sub("", head).strip()
            computed = Fact(
                fact_id=sc.ids.next("F"),
                statement=f"{attr_head}合计{total_val:g}{unit}",
                attribute=f"合计·{attr_head}",
                value=total_val,
                unit=unit,
                kind=inputs[0].kind,
                caliber=caliber,
                status=FactStatus.COMPUTED if all(f.status.usable_as_fact for f in inputs) else FactStatus.UNKNOWN,
                formula=Formula(
                    expression=" + ".join(f.fact_id for f in inputs),
                    inputs=[f.fact_id for f in inputs],
                    unit=unit,
                    caliber=caliber,
                    result_repr=f"{total_val:g}{unit}",
                ),
                depends_on=[f.fact_id for f in inputs],
                sources=[s for f in inputs for s in f.sources][:1],
                tags=tags + ["computed"],
            )
            ledger.facts.append(computed)
            for t in totals.get(c, []):
                ok = abs(float(t.value) - total_val) <= 1e-6 * max(1.0, abs(total_val))
                ledger.calc_checks.append(
                    CalcCheck(
                        check_id=sc.ids.next("K"),
                        kind="sum",
                        description=f"“{attr_head}”列合计",
                        expected=f"{total_val:g}{unit}",
                        actual=f"{t.display_value()}",
                        ok=ok,
                        locator=t.sources[0] if t.sources else None,
                        severity="重要",
                    )
                )
                if not ok:
                    t.status = FactStatus.CONFLICT
                    ledger.conflicts.append(
                        FactConflict(
                            conflict_id=sc.ids.next("X"),
                            attribute=f"合计·{attr_head}",
                            fact_ids=[t.fact_id, computed.fact_id],
                            description=f"表内合计 {t.display_value()} 与分项之和 {total_val:g}{unit} 不一致",
                        )
                    )
        # 比率列核验（如完成率 = 完成数 / 计划数）
        ratio_cols = [c for c, h in header.items() if any(k in h for k in ("率", "占比"))]
        for rc in ratio_cols:
            numeric_cols = [c for c in cols if c not in (label_col, rc) and c in col_inputs]
            pair = None
            for r in rows[1:]:
                if (r, rc) not in grid:
                    continue
                rv = _num(grid[(r, rc)].text.replace("%", "").replace("％", ""))
                if rv is None:
                    continue
                vals = {c: _num(grid[(r, c)].text) for c in numeric_cols if (r, c) in grid}
                if pair is None:
                    for a in numeric_cols:
                        for b in numeric_cols:
                            if a != b and vals.get(a) is not None and vals.get(b):
                                if abs(vals[a] / vals[b] * 100 - rv) <= 0.051:
                                    pair = (a, b)
                                    break
                        if pair:
                            break
                    continue
                a, b = pair
                if vals.get(a) is not None and vals.get(b):
                    exp = vals[a] / vals[b] * 100
                    ok = abs(exp - rv) <= 0.051
                    if not ok:
                        ledger.calc_checks.append(
                            CalcCheck(
                                check_id=sc.ids.next("K"),
                                kind="ratio",
                                description=f"“{header.get(rc)}”第{r}行比率",
                                expected=f"{exp:.1f}%",
                                actual=grid[(r, rc)].text,
                                ok=False,
                                locator=grid[(r, rc)].locator,
                            )
                        )

    def _conflicts(self, sc: SkillContext, ledger: FactLedger) -> None:
        candidates = [f for f in ledger.facts if isinstance(f.value, (int, float)) and f.kind != "plain" and "computed" not in f.tags and "example" not in f.tags]
        # 属性归并：“示范点”与“基层医疗示范点”指同一对象（短名是长名的后缀，且单位相同）
        attrs = sorted({f.attribute for f in candidates if f.attribute}, key=len)
        canon: dict[str, str] = {}
        for a in attrs:
            canon[a] = next((b for b in attrs if len(b) >= 2 and len(b) < len(a) and a.endswith(b) and canon.get(b) == b), a)
        groups: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
        for f in candidates:
            groups[(canon.get(f.attribute, f.attribute), f.kind, f.unit)].append(f)
        for (attr, kind, _unit), fs in groups.items():
            mats = {f.sources[0].material_id for f in fs if f.sources}
            if len(mats) < 2:
                continue
            by_scope: dict[tuple[str, str], list[Fact]] = defaultdict(list)
            unscoped = [f for f in fs if not f.caliber and not f.as_of]
            for f in fs:
                if f.caliber or f.as_of:
                    by_scope[(f.caliber, f.as_of)].append(f)
            if by_scope:
                for key in by_scope:  # 未注明口径与时点的数据无法排除冲突：与每个口径都比较
                    by_scope[key] += unscoped
            else:
                by_scope[("", "")] = unscoped
            seen_sets: set[frozenset[str]] = set()
            for scoped in by_scope.values():
                # 拟议值与现状值不同属正常：分别在“拟议”与“现状”两类内部比较
                proposed = [f for f in scoped if f.status == FactStatus.PROPOSED]
                actual = [f for f in scoped if f.status != FactStatus.PROPOSED]
                for items in (proposed, actual):
                    values = {round(float(f.value) * (MONEY_UNITS.get(f.unit, 1.0) if f.kind == "money" else 1.0), 6) for f in items}
                    key = frozenset(f.fact_id for f in items)
                    if len(values) < 2 or len({f.sources[0].material_id for f in items}) < 2 or key in seen_sets:
                        continue
                    seen_sets.add(key)
                    for f in items:
                        if f.status != FactStatus.VERIFIED:
                            f.status = FactStatus.CONFLICT
                    ledger.conflicts.append(
                        FactConflict(
                            conflict_id=sc.ids.next("X"),
                            attribute=attr,
                            fact_ids=[f.fact_id for f in items],
                            description="；".join(f"{f.sources[0].label()}：{f.display_value()}" for f in items),
                        )
                    )
            if len([k for k in by_scope if k != ("", "")]) > 1:
                ledger.calc_checks.append(
                    CalcCheck(
                        check_id=sc.ids.next("K"),
                        kind="caliber",
                        description="“{}”在不同材料中口径或时点不同（{}），不能直接比较或合并".format(attr, "；".join((c or "未注明口径") + "/" + (a or "未注明时点") for c, a in by_scope)),
                        expected="同一口径",
                        actual="口径不同",
                        ok=True,
                        severity="提示",
                    )
                )

    def _unit_checks(self, sc: SkillContext, ledger: FactLedger) -> None:
        money = [f for f in ledger.facts if f.kind == "money" and isinstance(f.value, (int, float))]
        by_attr: dict[str, list[Fact]] = defaultdict(list)
        for f in money:
            by_attr[f.attribute].append(f)
        for attr, fs in by_attr.items():
            units = {f.unit for f in fs}
            if len(units) > 1:
                vals = sorted({float(f.value) for f in fs})
                if len(vals) > 1 and vals[0] and abs(vals[-1] / vals[0] - 10000) < 1e-6:
                    ledger.calc_checks.append(
                        CalcCheck(check_id=sc.ids.next("K"), kind="unit", description=f"“{attr}”同时出现“元”与“万元”且数值相差一万倍，疑似单位未换算", expected="单位一致", actual="、".join(sorted(units)), ok=False)
                    )

    def _model_extract(self, sc: SkillContext, ledger: FactLedger, bundle: SourceBundle) -> None:
        narrative = [u for u in bundle.units if u.kind in ("paragraph", "page") and len(u.text) > 40][:60]
        if not narrative:
            return
        schema = {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "unit_id": {"type": "string"},
                            "statement": {"type": "string"},
                            "attribute": {"type": "string"},
                            "progress": {"type": "string", "enum": ["计划/拟议", "推进中", "已完成", "不适用"]},
                        },
                        "required": ["unit_id", "statement", "attribute", "progress"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["facts"],
            "additionalProperties": False,
        }
        blocks = "\n".join(wrap_untrusted(u.unit_id, u.text) for u in narrative)
        system = (
            "你是事实抽取助手。从资料中抽取与办文事项相关的事实陈述（不含数字的进展、问题、措施陈述），"
            "statement 必须是资料原文的连续片段，不得改写、不得推测。" + UNTRUSTED_NOTICE
        )
        try:
            resp = sc.router.call("light", [ChatMessage("user", blocks)], system=system, json_schema=schema, clearances=sc.clearances, purpose="fact_extraction", template_id="fact_ledger.v1", object_refs=[u.unit_id for u in narrative])
            data = resp.json()
        except (ModelUnavailable, ModelRefused, ValueError) as exc:
            sc.note("skill.model_skipped", {"skill": self.name, "reason": str(exc)})
            return
        units = {u.unit_id: u for u in narrative}
        existing = {f.statement for f in ledger.facts}
        accepted = rejected = 0
        mats = {m.material_id: m for m in bundle.materials}
        for item in data.get("facts", []):
            u = units.get(item.get("unit_id", ""))
            st = (item.get("statement") or "").strip()
            if u is None or not st or st not in u.text or st in existing:
                rejected += 1  # 不是原文片段：拒绝，避免模型改写事实
                continue
            prog = progress_of(st)
            status, tags, ver = self._status_for(mats.get(u.material_id), prog)
            ledger.facts.append(
                Fact(
                    fact_id=sc.ids.next("F"),
                    statement=st,
                    attribute=(item.get("attribute") or st[:16])[:20],
                    kind="text",
                    status=status,
                    progress=prog,
                    sources=[Locator(material_id=u.material_id, kind=u.kind, path=u.locator.path, excerpt=st[:80])],
                    verification=ver,
                    tags=tags + ["model_extracted"],
                )
            )
            accepted += 1
        sc.note("skill.model_extract", {"accepted": accepted, "rejected": rejected})


def confirm_facts(ledger: FactLedger, fact_ids: list[str], by: str) -> list[str]:
    """人工确认：材料记载 → 已核实事实（记录核验方式与确认人）。拟议内容不因确认而变成已完成。"""
    changed = []
    for fid in fact_ids:
        f = ledger.get(fid)
        if f is None or f.status in (FactStatus.PROPOSED, FactStatus.APPROVED):
            continue
        if f.status in (FactStatus.RECORDED, FactStatus.CONFLICT, FactStatus.UNKNOWN):
            f.status = FactStatus.VERIFIED
            f.verification = Verification(method="人工确认", by=by)
            changed.append(fid)
    for c in ledger.conflicts:
        if c.resolution is None and any(fid in c.fact_ids for fid in changed):
            c.resolution = f"人工确认 {','.join(fid for fid in changed if fid in c.fact_ids)}（{by}）"
            for other in c.fact_ids:
                if other not in changed:
                    f = ledger.get(other)
                    if f and f.status == FactStatus.CONFLICT:
                        f.status = FactStatus.UNKNOWN
                        f.notes = "冲突已由人工裁定，未被采信"
    return changed


def add_human_fact(ledger: FactLedger, ids, statement: str, value=None, unit: str = "", kind: str = "text", by: str = "human", status: FactStatus = FactStatus.VERIFIED) -> Fact:
    """人工补充事实（如经费测算金额），来源记为人工补充并留痕。"""
    f = Fact(
        fact_id=ids.next("F"),
        statement=statement,
        attribute=statement[:16],
        value=value,
        unit=unit,
        kind=kind,
        status=status,
        progress=progress_of(statement),
        sources=[Locator(material_id="human", kind="human", path=by, excerpt=statement[:80])],
        verification=Verification(method="人工补充", by=by),
        tags=["human"],
    )
    ledger.facts.append(f)
    return f


__all__ = ["FactLedgerSkill", "add_human_fact", "confirm_facts", "coverage"]
