"""静态知识加载：文种库、词表、单位配置档。"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SEED_DIR = Path(__file__).parent / "seed"
PROFILE_DIR = Path(__file__).parent.parent / "profiles"


@functools.lru_cache(maxsize=None)
def _load_yaml(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def seed(name: str) -> Any:
    return _load_yaml(str(SEED_DIR / name))


@dataclass
class GenreInfo:
    name: str
    statutory: bool
    article: str = ""
    definition: str = ""
    directions: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    format: str = "general"
    closings: dict[str, Any] = field(default_factory=dict)
    contract: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    forbidden_phrases: list[str] = field(default_factory=list)
    issue_vehicle: str | None = None
    variants: list[str] = field(default_factory=list)

    def closing_candidates(self, direction: str | None = None, purpose: str | None = None) -> list[str]:
        c = self.closings or {}
        out: list[str] = []
        if direction and c.get("by_direction", {}).get(direction):
            out += c["by_direction"][direction]
        if purpose and c.get("by_purpose", {}).get(purpose):
            out += c["by_purpose"][purpose]
        out += list(c.get("preferred", []) or []) + list(c.get("acceptable", []) or [])
        return list(dict.fromkeys(out))


STATUTORY_GENRES = [
    "决议", "决定", "命令（令）", "公报", "公告", "通告", "意见", "通知",
    "通报", "报告", "请示", "批复", "议案", "函", "纪要",
]


@functools.lru_cache(maxsize=None)
def genres() -> dict[str, GenreInfo]:
    data = seed("genres.yaml")
    out: dict[str, GenreInfo] = {}
    for name, g in data["statutory"].items():
        out[name] = GenreInfo(
            name=name,
            statutory=True,
            article=g.get("article", ""),
            definition=g.get("definition", ""),
            directions=g.get("directions", []),
            subjects=g.get("subjects", []),
            format=g.get("format", "general"),
            closings=g.get("closings", {}),
            contract=g.get("contract", []),
            risks=g.get("risks", []),
            forbidden_phrases=g.get("forbidden_phrases", []),
            variants=g.get("variants", []),
        )
    for name, g in data["affairs"].items():
        if "alias_of" in g:
            continue
        out[name] = GenreInfo(
            name=name,
            statutory=False,
            contract=g.get("contract", []),
            risks=g.get("risks", []),
            issue_vehicle=g.get("issue_vehicle"),
        )
    return out


def canonical_genre(name: str | None) -> str | None:
    if not name:
        return None
    data = seed("genres.yaml")
    name = name.strip()
    aliases = data.get("aliases", {})
    if name in aliases:
        return aliases[name]
    for k, g in data["affairs"].items():
        if k == name and "alias_of" in g:
            return g["alias_of"]
    return name


def genre(name: str | None) -> GenreInfo | None:
    cname = canonical_genre(name)
    return genres().get(cname) if cname else None


@functools.lru_cache(maxsize=None)
def lexicon() -> dict[str, Any]:
    return seed("lexicon.yaml")


@functools.lru_cache(maxsize=None)
def obligation_pattern() -> re.Pattern[str]:
    words = sorted(lexicon()["obligation"].keys(), key=len, reverse=True)
    return re.compile("|".join(map(re.escape, words)))


def obligation_strength(word: str) -> float:
    return float(lexicon()["obligation"].get(word, 0))


@functools.lru_cache(maxsize=None)
def unit_profile(name: str) -> dict[str, Any]:
    path = PROFILE_DIR / f"{name}.yaml"
    if not path.is_file():
        raise KeyError(f"未找到单位配置档：{name}")
    return _load_yaml(str(path))


def list_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.yaml"))
