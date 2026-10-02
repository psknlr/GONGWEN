"""确定性检查通道：规则检查器汇总、去重与报告。"""

from __future__ import annotations

from . import basis, burden_style, consistency, format_rules, genre_rules, language, semantics
from .base import CheckContext, Checker
from .registry import RULES, RULESET_VERSION, citation, rule

GROUPS: dict[str, list[Checker]] = {
    "genre": genre_rules.CHECKERS,
    "language": language.CHECKERS,
    "semantics": semantics.CHECKERS,
    "basis": basis.CHECKERS,
    "burden": burden_style.CHECKERS,
    "consistency": consistency.CHECKERS,
    "format": format_rules.CHECKERS,
}


def run_checks(ctx: CheckContext, groups: list[str] | None = None) -> list:
    issues = []
    seen: set[tuple] = set()
    for g in groups or list(GROUPS):
        if g == "consistency" and not ctx.features.consistency_check:
            continue
        for fn in GROUPS[g]:
            for i in fn(ctx):
                key = (i.rule.rule_id if i.rule else "", i.type, i.location.sentence_id, i.location.block_id, i.location.field, i.suggestion[:40])
                if key in seen:
                    continue
                seen.add(key)
                issues.append(i)
    issues.sort(key=lambda i: -i.severity.rank)
    return issues


def checker_names(groups: list[str] | None = None) -> list[str]:
    return [f"{g}.{fn.__name__}" for g in (groups or list(GROUPS)) for fn in GROUPS[g]]


__all__ = ["CheckContext", "GROUPS", "RULES", "RULESET_VERSION", "citation", "rule", "run_checks", "checker_names"]
