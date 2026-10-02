"""权限控制到“人—事项—材料—工具动作”（设计 §7.2）。

关键边界：
* 检索只能检索获授权材料；
* 起草通道不能写入审批结果；
* 审校通道不能擅自修改正文；
* 发布、外发和电子签章不属于默认自动工具（动作存在于词表中，但任何通道都不被授予）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..schemas.common import Clearance


class Action(str, Enum):
    MATERIAL_ADD = "material.add"
    MATERIAL_READ = "material.read"
    ADMISSION_CONFIRM = "admission.confirm"
    POLICY_SEARCH = "policy.search"
    POLICY_PROMOTE = "policy.promote"  # 把材料登记为依据库文件：仅限人工并补全元数据
    CASE_READ_STYLE = "case.read_style"
    TASK_WRITE = "task.write"
    LEDGER_WRITE = "ledger.write"
    OUTLINE_WRITE = "outline.write"
    IR_WRITE = "ir.write"
    IR_PATCH = "ir.patch"
    ISSUE_WRITE = "issue.write"
    LAYOUT_COMPILE = "layout.compile"
    EXPORT_WRITE = "export.write"
    CHECKPOINT_RESOLVE = "checkpoint.resolve"
    APPROVAL_IMPORT = "approval.import"
    MODEL_CALL = "model.call"
    # 以下动作默认不授予任何主体
    EXTERNAL_SEND = "external.send"
    PUBLISH = "publish"
    E_SIGN = "e_sign"
    SEAL = "seal"


NEVER_AUTOMATIC = {Action.EXTERNAL_SEND, Action.PUBLISH, Action.E_SIGN, Action.SEAL}


@dataclass
class Principal:
    id: str
    kind: str = "human"  # human / channel
    roles: set[str] = field(default_factory=set)
    max_clearance: Clearance = Clearance.PUBLIC
    matters: set[str] = field(default_factory=lambda: {"*"})

    @property
    def is_human(self) -> bool:
        return self.kind == "human"


# 通道（模型或程序执行单元）的能力集合：由执行层强制，不依赖提示词
CHANNEL_GRANTS: dict[str, set[Action]] = {
    "channel:planner": {Action.MATERIAL_READ, Action.TASK_WRITE, Action.POLICY_SEARCH, Action.MODEL_CALL},
    "channel:parser": {Action.MATERIAL_READ},
    "channel:retriever": {Action.MATERIAL_READ, Action.POLICY_SEARCH, Action.CASE_READ_STYLE},
    "channel:ledger": {Action.MATERIAL_READ, Action.LEDGER_WRITE, Action.MODEL_CALL},
    "channel:outliner": {Action.MATERIAL_READ, Action.OUTLINE_WRITE, Action.CASE_READ_STYLE, Action.MODEL_CALL},
    "channel:drafter": {Action.MATERIAL_READ, Action.IR_WRITE, Action.CASE_READ_STYLE, Action.MODEL_CALL},
    "channel:reviewer": {Action.MATERIAL_READ, Action.POLICY_SEARCH, Action.ISSUE_WRITE, Action.MODEL_CALL},
    "channel:reviser": {Action.MATERIAL_READ, Action.IR_PATCH, Action.MODEL_CALL},
    "channel:layout": {Action.LAYOUT_COMPILE, Action.EXPORT_WRITE},
    "channel:packager": {Action.EXPORT_WRITE},
    "channel:agent": {
        Action.MATERIAL_ADD,
        Action.MATERIAL_READ,
        Action.POLICY_SEARCH,
        Action.TASK_WRITE,
        Action.EXPORT_WRITE,
        Action.MODEL_CALL,
    },
}

ROLE_GRANTS: dict[str, set[Action]] = {
    "drafter": {  # 经办人
        Action.MATERIAL_ADD,
        Action.MATERIAL_READ,
        Action.POLICY_SEARCH,
        Action.CASE_READ_STYLE,
        Action.TASK_WRITE,
        Action.CHECKPOINT_RESOLVE,
        Action.IR_PATCH,
        Action.EXPORT_WRITE,
        Action.ADMISSION_CONFIRM,
        Action.MODEL_CALL,
    },
    "reviewer": {Action.MATERIAL_READ, Action.POLICY_SEARCH, Action.ISSUE_WRITE, Action.CHECKPOINT_RESOLVE, Action.EXPORT_WRITE},
    "approver": {Action.MATERIAL_READ, Action.APPROVAL_IMPORT, Action.CHECKPOINT_RESOLVE},
    "admin": {Action.POLICY_PROMOTE, Action.ADMISSION_CONFIRM},
}

HUMAN_ONLY = {Action.CHECKPOINT_RESOLVE, Action.APPROVAL_IMPORT, Action.ADMISSION_CONFIRM, Action.POLICY_PROMOTE}


@dataclass
class Decision:
    allowed: bool
    reason: str


class PermissionEngine:
    def __init__(self, extra_role_grants: dict[str, set[Action]] | None = None):
        self.role_grants = {k: set(v) for k, v in ROLE_GRANTS.items()}
        for k, v in (extra_role_grants or {}).items():
            self.role_grants.setdefault(k, set()).update(v)

    def grants(self, principal: Principal) -> set[Action]:
        if principal.kind == "channel":
            return set(CHANNEL_GRANTS.get(principal.id, set()))
        out: set[Action] = set()
        for r in principal.roles:
            out |= self.role_grants.get(r, set())
        return out

    def check(
        self,
        principal: Principal,
        action: Action,
        matter_id: str | None = None,
        clearance: Clearance | None = None,
    ) -> Decision:
        if action in NEVER_AUTOMATIC:
            return Decision(False, f"{action.value} 不属于本系统的自动工具，须在真实办理流程中由有权人员完成")
        if action in HUMAN_ONLY and not principal.is_human:
            return Decision(False, f"{action.value} 只能由人工通道执行，模型通道不可代为确认")
        if action not in self.grants(principal):
            return Decision(False, f"{principal.id} 未被授予 {action.value}")
        if matter_id and "*" not in principal.matters and matter_id not in principal.matters:
            return Decision(False, f"{principal.id} 无权访问事项 {matter_id}")
        if clearance is not None and clearance.rank > principal.max_clearance.rank:
            return Decision(False, f"{principal.id} 的材料访问级别（{principal.max_clearance.value}）低于材料属性（{clearance.value}）")
        return Decision(True, "ok")

    def require(self, principal: Principal, action: Action, matter_id: str | None = None, clearance: Clearance | None = None) -> None:
        d = self.check(principal, action, matter_id, clearance)
        if not d.allowed:
            raise PermissionError(d.reason)


def channel(name: str, max_clearance: Clearance = Clearance.WORK_SECRET) -> Principal:
    return Principal(id=f"channel:{name}", kind="channel", max_clearance=max_clearance)


def human(user_id: str, *roles: str, max_clearance: Clearance = Clearance.WORK_SECRET, matters: set[str] | None = None) -> Principal:
    return Principal(id=user_id, kind="human", roles=set(roles or ("drafter",)), max_clearance=max_clearance, matters=matters or {"*"})
