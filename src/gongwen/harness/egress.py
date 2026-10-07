"""出网网关：保密边界不能靠提示词解决（设计 §7.1）。

所有模型调用和外部访问都必须经过本网关，由执行层判定：
* 环境路线（公开材料研发版 / 单位批准的业务环境）；
* 本次请求所含材料的最高属性 vs 目标模型允许的最高属性；
* 目标主机是否在白名单内。

教训借鉴：曾有开发工具在“生成仓库知识库”时把本地工作区快照直接上传云端对象存储，
绕过了服务端审计。本网关的设计目标就是让任何功能都无法在网关之外“顺手”上传材料：
网关是唯一出口，默认拒绝，所有判定写入审计日志（仅记录哈希与属性，不记录内容）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..schemas.common import Clearance, EnvironmentRoute


class EgressDenied(PermissionError):
    pass


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_local_host(host: str | None) -> bool:
    """本机回环地址无需列入白名单。“*.local”是局域网 mDNS 主机（其他机器），不属于本机，须列入白名单。"""
    return (host or "").lower() in LOCAL_HOSTS


@dataclass
class EgressRequest:
    url: str
    purpose: str
    clearances: list[Clearance] = field(default_factory=list)
    model_max_clearance: Clearance = Clearance.PUBLIC
    payload_hash: str = ""


@dataclass
class EgressVerdict:
    allowed: bool
    reason: str
    host: str = ""


class EgressGateway:
    def __init__(
        self,
        route: EnvironmentRoute,
        allowed_hosts: list[str] | None = None,
        block_all: bool = False,
        audit=None,
    ):
        self.route = route
        self.allowed_hosts = {h.lower() for h in (allowed_hosts or [])}
        self.block_all = block_all
        self.audit = audit  # callable(type, payload)

    def evaluate(self, req: EgressRequest) -> EgressVerdict:
        host = (urlparse(req.url).hostname or "").lower()
        top = max(req.clearances, key=lambda c: c.rank, default=Clearance.PUBLIC)
        if self.route == EnvironmentRoute.CLASSIFIED:
            return EgressVerdict(False, "涉密应用不在本系统能力承诺范围内", host)
        if top == Clearance.CLASSIFIED:
            return EgressVerdict(False, "涉密材料不得进入本系统，更不得发送至任何模型", host)
        if top == Clearance.UNKNOWN:
            return EgressVerdict(False, "存在属性未确认的材料：须先完成准入确认，不允许“先上传模型再判断是否敏感”", host)
        if self.block_all:
            return EgressVerdict(False, "当前配置禁止一切出网", host)
        if self.route == EnvironmentRoute.PUBLIC_DEV and top.rank > Clearance.PUBLIC.rank:
            return EgressVerdict(False, f"公开材料研发版仅允许公开材料出网，本次包含“{top.value}”材料", host)
        if top.rank > req.model_max_clearance.rank:
            return EgressVerdict(
                False,
                f"目标模型仅获准处理“{req.model_max_clearance.value}”及以下材料，本次包含“{top.value}”材料",
                host,
            )
        if not is_local_host(host) and host not in self.allowed_hosts:
            return EgressVerdict(False, f"主机 {host or '(空)'} 不在出网白名单内", host)
        return EgressVerdict(True, "ok", host)

    def check(self, req: EgressRequest) -> EgressVerdict:
        v = self.evaluate(req)
        if self.audit is not None:
            self.audit(
                "egress.decision",
                {
                    "host": v.host,
                    "purpose": req.purpose,
                    "allowed": v.allowed,
                    "reason": v.reason,
                    "max_clearance": max(req.clearances, key=lambda c: c.rank, default=Clearance.PUBLIC).value,
                    "payload_sha256": req.payload_hash,
                },
            )
        if not v.allowed:
            raise EgressDenied(v.reason)
        return v
