"""阶段产物的结构化契约（12 项技能的输入输出 Schema 均在此定义）。"""

from .common import (
    AdmissionDecision,
    Clearance,
    DocStatus,
    EnvironmentRoute,
    EvidenceRef,
    GWModel,
    IdAllocator,
    Locator,
    RuleLevel,
    Severity,
    Slot,
    slot,
    stable_hash,
)
from .facts import CalcCheck, Fact, FactConflict, FactLedger, FactStatus, Formula, Progress, Verification
from .genre import AuthorityFinding, GenreDecision, ProcedureRequirement, RuleCitation
from .ir import (
    Attachment,
    AttachmentNote,
    Block,
    DocumentIR,
    Header,
    Imprint,
    Placeholder,
    Sentence,
    Signature,
)
from .layout import LayoutCheck, LayoutReport, OutputFile, RenderInfo
from .outline import AlternativePlan, Measure, OutlinePlan, ParagraphPlan, SectionPlan
from .package import EvidenceRow, PendingItem, ReviewPackage, VersionEntry
from .patch import FactChange, Patch, PatchSet, RecheckResult, SemanticChange
from .policy import (
    Applicability,
    PolicyArticle,
    PolicyConflict,
    PolicyDocument,
    PolicyEvidence,
    PolicyPack,
)
from .review import (
    ConsistencyFinding,
    ConsistencyReport,
    IssueLocation,
    IssueType,
    ReviewIssue,
    ReviewReport,
)
from .sources import (
    AdmissionFinding,
    AdmissionResult,
    Material,
    SourceBundle,
    SourceRelation,
    SourceUnit,
    TableData,
)
from .state import (
    ApprovalRecord,
    BudgetUsage,
    Checkpoint,
    CheckpointKind,
    CheckpointOption,
    MatterDocument,
    MatterModel,
    Stage,
    StageRecord,
    TaskState,
)
from .task import Direction, Gap, GapImpact, Organ, Purpose, TaskLayer, TaskSpec

# 技能编号 → 输出契约（用于生成 Agent Skills 的 schema 文件与契约测试）
SKILL_OUTPUT_SCHEMAS = {
    "gongwen-task-modeling": TaskSpec,
    "gongwen-material-parsing": SourceBundle,
    "gongwen-genre-authority": GenreDecision,
    "gongwen-policy-retrieval": PolicyPack,
    "gongwen-fact-ledger": FactLedger,
    "gongwen-outline-planning": OutlinePlan,
    "gongwen-constrained-drafting": DocumentIR,
    "gongwen-consistency-check": ConsistencyReport,
    "gongwen-independent-review": ReviewReport,
    "gongwen-targeted-revision": PatchSet,
    "gongwen-layout-compile": LayoutReport,
    "gongwen-review-package": ReviewPackage,
}

__all__ = [name for name in dir() if not name.startswith("_")]
