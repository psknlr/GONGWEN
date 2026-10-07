"""12 项技能说明符合 Agent Skills 规范，并与代码中的技能注册表、输出 Schema 一一对应。"""

import re

import yaml

from gongwen.cli.main import main as cli
from gongwen.schemas import SKILL_OUTPUT_SCHEMAS
from gongwen.skills import build_registry
from gongwen.skills.base import SKILLS_DIR, skill_doc


def test_skill_docs_match_registry():
    names = {s.name for s in build_registry()}
    dirs = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    assert names == dirs == set(SKILL_OUTPUT_SCHEMAS) and len(names) == 12
    for name in names:
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm)
        assert meta["name"] == name and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
        assert 20 <= len(meta["description"]) <= 1024
        assert all(isinstance(v, str) for v in (meta.get("metadata") or {}).values())
        assert meta["metadata"]["output-schema"] == SKILL_OUTPUT_SCHEMAS[name].__name__
        assert body.strip() and skill_doc(name)["description"] == meta["description"]


def test_skills_install_and_schemas(tmp_path, capsys):
    assert cli(["-C", str(tmp_path), "skills", "install", "--dest", ".agents/skills"]) == 0
    assert len(list((tmp_path / ".agents" / "skills").glob("*/SKILL.md"))) == 12
    assert cli(["-C", str(tmp_path), "skills", "schemas", "--out", str(tmp_path / "schemas")]) == 0
    assert len(list((tmp_path / "schemas").glob("*.schema.json"))) == 12
