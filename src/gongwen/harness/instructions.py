"""单位制度与项目指令文件加载。

借鉴 Codex/grok-cli 的 AGENTS.md 分层合并：从 git 根目录到当前目录逐级读取
``GONGWEN.md``（单位文风与制度配置）和 ``AGENTS.md``，后者覆盖前者；
存在 ``GONGWEN.override.md`` 时以其替换同级文件。

这些文件只作为“写作参考与单位制度说明”进入上下文，不能扩大工具权限。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

NAMES = ("GONGWEN.md", "AGENTS.md")
OVERRIDE = "GONGWEN.override.md"
MAX_BYTES = 64 * 1024


@dataclass
class InstructionFile:
    path: Path
    text: str


def _git_root(start: Path) -> Path | None:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


def discover(cwd: str | Path) -> list[InstructionFile]:
    cwd = Path(cwd).resolve()
    root = _git_root(cwd) or cwd
    chain = [root]
    try:
        rel = cwd.relative_to(root)
        cur = root
        for part in rel.parts:
            cur = cur / part
            chain.append(cur)
    except ValueError:
        chain = [cwd]
    out: list[InstructionFile] = []
    for d in chain:
        override = d / OVERRIDE
        if override.is_file():
            out.append(InstructionFile(override, override.read_text(encoding="utf-8")[:MAX_BYTES]))
            continue
        for name in NAMES:
            f = d / name
            if f.is_file():
                out.append(InstructionFile(f, f.read_text(encoding="utf-8")[:MAX_BYTES]))
    return out


def merged(cwd: str | Path) -> str:
    files = discover(cwd)
    parts = [f"<!-- {f.path} -->\n{f.text.strip()}" for f in files if f.text.strip()]
    return "\n\n".join(parts)
