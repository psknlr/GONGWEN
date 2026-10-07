"""工具动作审批策略（借鉴 Codex：untrusted / on-request / never）。

注意区分两类“确认”：
* 工具动作审批（本模块）：如写出工作区以外的文件，按策略询问或拒绝；
* 办文人工审核节点（orchestrator.checkpoints）：任务确认、提纲确认、人工送审等，
  由状态机强制，任何审批策略都不能跳过“人工送审”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ApprovalRequest:
    tool: str
    summary: str
    risk: str = "low"  # low / medium / high
    side_effect: bool = False
    outside_workspace: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class ApprovalOutcome:
    approved: bool
    reason: str
    asked: bool = False


Approver = Callable[[ApprovalRequest], bool]


class ApprovalPolicy:
    MODES = ("untrusted", "on-request", "never")

    def __init__(self, mode: str = "on-request", approver: Approver | None = None):
        if mode not in self.MODES:
            raise ValueError(f"未知审批策略：{mode}（可选 {self.MODES}）")
        self.mode = mode
        self.approver = approver

    def needs_approval(self, req: ApprovalRequest) -> bool:
        if self.mode == "untrusted":
            return req.side_effect or req.risk != "low"
        # on-request / never：只有越出沙箱或高风险动作才需要审批
        return req.outside_workspace or req.risk == "high"

    def decide(self, req: ApprovalRequest) -> ApprovalOutcome:
        if not self.needs_approval(req):
            return ApprovalOutcome(True, "无需审批")
        if self.mode == "never" or self.approver is None:
            return ApprovalOutcome(False, f"该动作需要审批，但当前策略为 {self.mode} 且无人工审批通道：已拒绝")
        ok = bool(self.approver(req))
        return ApprovalOutcome(ok, "人工批准" if ok else "人工拒绝", asked=True)
