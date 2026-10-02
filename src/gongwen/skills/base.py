"""技能基础：每项技能是可组合的能力模块，不是常驻的“角色”。

技能包含：操作说明（skills/<name>/SKILL.md，按需加载）、输入输出 Schema、工具白名单、
规则依据、示例与测试。真正的权限约束由执行层（权限引擎、工具管线、出网网关）实施，
不依赖技能文档中的文字。
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from ..harness.permissions import Principal, channel
from ..harness.session import SessionLog
from ..llm.router import ModelRouter
from ..schemas.common import Clearance, IdAllocator
from ..schemas.state import Stage, TaskState

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"


@functools.lru_cache(maxsize=None)
def skill_doc(name: str) -> dict[str, Any]:
    """按需加载 Agent Skills 格式的 SKILL.md（元信息 + 指令正文）。"""
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        return {"name": name, "description": "", "body": ""}
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
        meta["body"] = body.strip()
        return meta
    return {"name": name, "description": "", "body": text}


@dataclass
class SkillContext:
    runtime: "Runtime"
    state: TaskState
    log: SessionLog
    router: ModelRouter
    clearances: list[Clearance] = field(default_factory=lambda: [Clearance.PUBLIC])
    principal: Principal | None = None

    @property
    def ids(self) -> IdAllocator:
        return self.state.ids

    @property
    def store(self):
        return self.runtime.tasks

    @property
    def features(self):
        return self.runtime.config.features

    def load(self, name: str, cls):
        return self.store.load_model(self.state.task_id, name, cls)

    def save(self, name: str, obj) -> str:
        digest = self.store.save_model(self.state.task_id, name, obj)
        self.state.artifacts[name] = digest
        self.log.append("artifact.saved", {"name": name, "sha256": digest}, stage=self.state.stage.value)
        return digest

    def model_available(self, role: str = "heavy") -> bool:
        return self.router.available(role, self.clearances)

    def note(self, type_: str, payload: dict[str, Any]) -> None:
        self.log.append(type_, payload, stage=self.state.stage.value)


class Skill:
    name: ClassVar[str] = ""
    number: ClassVar[int] = 0
    title: ClassVar[str] = ""
    stage: ClassVar[Stage] = Stage.ADMISSION
    channel_name: ClassVar[str] = "planner"
    allowed_tools: ClassVar[tuple[str, ...]] = ()
    output_artifact: ClassVar[str] = ""

    def principal(self) -> Principal:
        return channel(self.channel_name)

    def instructions(self) -> str:
        return skill_doc(self.name).get("body", "")

    def describe(self) -> dict[str, Any]:
        doc = skill_doc(self.name)
        return {
            "name": self.name,
            "number": self.number,
            "title": self.title,
            "stage": self.stage.value,
            "channel": f"channel:{self.channel_name}",
            "allowed_tools": list(self.allowed_tools),
            "output": self.output_artifact,
            "description": doc.get("description", ""),
        }


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        return self._skills[name]

    def __iter__(self):
        return iter(sorted(self._skills.values(), key=lambda s: s.number))

    def __len__(self) -> int:
        return len(self._skills)
