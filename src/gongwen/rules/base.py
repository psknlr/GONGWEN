"""检查器共享上下文与问题构造。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..kernel.config import FeatureFlags
from ..schemas.common import EvidenceRef, IdAllocator, Severity
from ..schemas.facts import FactLedger
from ..schemas.genre import GenreDecision
from ..schemas.ir import Block, DocumentIR, Sentence
from ..schemas.outline import OutlinePlan
from ..schemas.policy import PolicyPack
from ..schemas.review import IssueLocation, IssueType, ReviewIssue
from ..schemas.sources import SourceBundle
from ..schemas.task import TaskSpec
from .registry import RULES, effective_severity


@dataclass
class CheckContext:
    ir: DocumentIR
    task: TaskSpec | None = None
    genre: GenreDecision | None = None
    ledger: FactLedger | None = None
    policies: PolicyPack | None = None
    outline: OutlinePlan | None = None
    sources: SourceBundle | None = None
    previous: DocumentIR | None = None
    siblings: list[DocumentIR] = field(default_factory=list)  # 同一事项的其他文稿
    profile: dict = field(default_factory=dict)
    features: FeatureFlags = field(default_factory=FeatureFlags)
    ids: IdAllocator = field(default_factory=IdAllocator)
    severity_overrides: dict[str, str] = field(default_factory=dict)
    policy_library: object | None = None

    @property
    def direction(self) -> str:
        if self.ir.direction:
            return self.ir.direction
        if self.genre:
            return self.genre.direction
        return ""

    def issue(
        self,
        rule_id: str,
        itype: IssueType,
        message: str,
        *,
        block: Block | None = None,
        sentence: Sentence | None = None,
        field_name: str | None = None,
        original: str = "",
        evidence: list[EvidenceRef] | None = None,
        evidence_text: str = "",
        severity: Severity | None = None,
        impact: list[str] | None = None,
        needs_human: bool = False,
        auto_fixable: bool = False,
        fix_hint: dict[str, str] | None = None,
        channel: str = "deterministic",
        span: tuple[int, int] | None = None,
    ) -> ReviewIssue:
        meta = RULES[rule_id]
        sev = severity or effective_severity(rule_id, self.severity_overrides)
        if meta.conditional and sev.rank > Severity.MINOR.rank and severity is None:
            sev = Severity.MINOR
        loc = IssueLocation(
            doc_id=self.ir.doc_id,
            block_id=block.bid if block else None,
            sentence_id=sentence.sid if sentence else None,
            span=span,
            label=self.ir.location_label(block.bid) if block else (field_name or ""),
            field=field_name,
        )
        return ReviewIssue(
            issue_id=self.ids.next("R"),
            location=loc,
            type=itype,
            severity=sev,
            original=original or (sentence.text if sentence else ""),
            evidence=evidence or [],
            evidence_text=evidence_text,
            suggestion=message,
            impact=impact or [],
            rule=meta.citation(),
            channel=channel,
            needs_human=needs_human,
            auto_fixable=auto_fixable,
            fix_hint=fix_hint or {},
        )


Checker = Callable[[CheckContext], list[ReviewIssue]]
