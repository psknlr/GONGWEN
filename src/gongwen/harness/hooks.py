"""钩子：在工具调用前后、阶段进入退出、审核节点产生时运行单位自定义检查。

借鉴 grok-cli / Claude Code 的 hooks 约定：
* 命令钩子从标准输入接收 JSON 事件；退出码 0 放行，2 阻断（stderr 作为理由）；
* 标准输出若为 JSON 且含 {"decision": "block", "reason": "..."} 也视为阻断；
* 进程内钩子通过内核事件 ``hook/<Event>`` 注册，返回非 None 字符串即阻断。
"""

from __future__ import annotations

import fnmatch
import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

from ..kernel.config import HookSpec, HooksConfig
from ..kernel.context import Context


@dataclass
class HookResult:
    blocked: bool
    reason: str = ""
    outputs: list[str] | None = None


class HookRunner:
    def __init__(self, config: HooksConfig, ctx: Context | None = None, cwd: str | None = None):
        self.config = config
        self.ctx = ctx
        self.cwd = cwd

    def _specs(self, event: str) -> list[HookSpec]:
        return list(getattr(self.config, event, []) or [])

    def run(self, event: str, subject: str, payload: dict[str, Any]) -> HookResult:
        outputs: list[str] = []
        # 进程内钩子
        if self.ctx is not None:
            # 只有非空字符串（阻断理由）才中断：前面的监听者返回 False 等放行值时，后面监听者的阻断仍然生效
            r = self.ctx.bail_if(lambda x: isinstance(x, str) and bool(x), f"hook/{event}", subject, payload)
            if r:
                return HookResult(True, r, outputs)
        for spec in self._specs(event):
            if not fnmatch.fnmatch(subject, spec.matcher):
                continue
            event_json = json.dumps({"event": event, "subject": subject, **payload}, ensure_ascii=False, default=str)
            try:
                proc = subprocess.run(
                    shlex.split(spec.command),
                    input=event_json,
                    capture_output=True,
                    text=True,
                    timeout=spec.timeout,
                    cwd=self.cwd,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return HookResult(True, f"钩子执行失败（{spec.command}）：{exc}", outputs)
            out = (proc.stdout or "").strip()
            if out:
                outputs.append(out)
                try:
                    data = json.loads(out)
                    if isinstance(data, dict) and data.get("decision") == "block":
                        return HookResult(True, str(data.get("reason", "钩子阻断")), outputs)
                except json.JSONDecodeError:
                    pass
            if proc.returncode == 2:
                return HookResult(True, (proc.stderr or "钩子阻断").strip(), outputs)
            if proc.returncode not in (0, 2):
                return HookResult(True, f"钩子返回异常退出码 {proc.returncode}：{(proc.stderr or '').strip()}", outputs)
        return HookResult(False, "", outputs)
