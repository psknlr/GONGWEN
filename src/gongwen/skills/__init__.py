"""12 项可组合技能（设计 §3.3）。技能按任务逐步加载，不意味着每项都常驻一个 Agent。"""

from .base import Skill, SkillContext, SkillRegistry, skill_doc
from .consistency_check import ConsistencyCheckSkill
from .drafting import DraftingSkill
from .fact_ledger import FactLedgerSkill
from .genre_authority import GenreAuthoritySkill
from .independent_review import IndependentReviewSkill
from .layout_compile import LayoutCompileSkill
from .material_parsing import MaterialParsingSkill
from .outline_planning import OutlinePlanningSkill
from .policy_retrieval import PolicyRetrievalSkill
from .review_package import ReviewPackageSkill
from .revision import RevisionSkill
from .task_modeling import TaskModelingSkill

ALL_SKILLS = [
    TaskModelingSkill,
    MaterialParsingSkill,
    GenreAuthoritySkill,
    PolicyRetrievalSkill,
    FactLedgerSkill,
    OutlinePlanningSkill,
    DraftingSkill,
    ConsistencyCheckSkill,
    IndependentReviewSkill,
    RevisionSkill,
    LayoutCompileSkill,
    ReviewPackageSkill,
]


def build_registry() -> SkillRegistry:
    reg = SkillRegistry()
    for cls in ALL_SKILLS:
        reg.register(cls())
    return reg


__all__ = ["ALL_SKILLS", "Skill", "SkillContext", "SkillRegistry", "build_registry", "skill_doc"]
