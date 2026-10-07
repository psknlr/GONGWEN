"""按行编辑工作区配置（TOML），保留用户的注释、顺序与其他设置。

只做三种改动：设置某个表中的一个键、新增（或替换）一个表、整体校验后原子写回。
每次改动后都用 tomllib 重新解析：结果不能解析或与预期不符时拒绝写入，由用户手工编辑。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # JSON 字符串转义均为 TOML 合法转义，另补 DEL
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"不支持写入的配置值类型：{type(value).__name__}")


def _key_pattern(key: str) -> str:
    return rf"(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')"


def _header_re(table: str) -> re.Pattern[str]:
    parts = r"\s*\.\s*".join(_key_pattern(p) for p in table.split("."))
    return re.compile(rf"^\s*\[\s*{parts}\s*\]\s*(?:#.*)?$")


_ANY_HEADER = re.compile(r"^\s*\[")


def find_table(lines: list[str], table: str) -> tuple[int, int] | None:
    """返回表头所在行与表体结束位置（下一个表头或文件末尾）。"""
    pat = _header_re(table)
    for i, line in enumerate(lines):
        if pat.match(line):
            end = next((j for j in range(i + 1, len(lines)) if _ANY_HEADER.match(lines[j])), len(lines))
            return i, end
    return None


def _comment_start(line: str) -> int:
    """行内注释的起始位置（字符串之外的 #），没有时返回 -1。"""
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "#":
            return i
        i += 1
    return -1


def _value_end(lines: list[str], start: int, limit: int) -> int:
    """键值对可能跨多行（如多行数组）：找到能单独解析的最短片段的末行。"""
    for j in range(start, min(limit, start + 200)):
        try:
            tomllib.loads("\n".join(lines[start : j + 1]))
            return j
        except tomllib.TOMLDecodeError:
            continue
    raise ValueError(f"无法解析配置第 {start + 1} 行的取值，请手工编辑")


def set_key(text: str, table: str, key: str, value: Any) -> str:
    """把 [table] 中的 key 设为 value（保留该行的行尾注释）；表不存在时追加到文件末尾。"""
    lines = text.splitlines()
    rendered = f"{key} = {toml_value(value)}"
    span = find_table(lines, table)
    if span is None:
        tail = [""] if lines and lines[-1].strip() else []
        lines += tail + [f"[{table}]", rendered]
        return "\n".join(lines) + "\n"
    start, end = span
    key_re = re.compile(rf"^(\s*){_key_pattern(key)}\s*=")
    for i in range(start + 1, end):
        m = key_re.match(lines[i])
        if m:
            j = _value_end(lines, i, end)
            c = _comment_start(lines[j])
            comment = lines[j][c:].rstrip() if c >= 0 else ""
            lines[i : j + 1] = [m.group(1) + rendered + (f"  {comment}" if comment else "")]
            return "\n".join(lines) + "\n"
    # 插在表内最后一个键值之后（其后的注释与空行通常属于下一节）
    last = max((k for k in range(start + 1, end) if lines[k].strip() and not lines[k].lstrip().startswith("#")), default=start)
    lines.insert(last + 1, rendered)
    return "\n".join(lines) + "\n"


def upsert_table(text: str, table: str, values: dict[str, Any], comment: str = "", replace: bool = False, marker: str = "") -> str:
    """新增 [table]（追加到文件末尾）；已存在时只有 replace=True 才替换其中的键值（保留其后的注释与空行）。

    marker：自动生成的说明注释的前缀，替换时一并去掉旧的说明注释，避免重复。"""
    lines = text.splitlines()
    block = ([comment] if comment else []) + [f"[{table}]"] + [f"{k} = {toml_value(v)}" for k, v in values.items() if v is not None]
    span = find_table(lines, table)
    if span is None:
        tail = [""] if lines and lines[-1].strip() else []
        return "\n".join(lines + tail + block) + "\n"
    if not replace:
        raise FileExistsError(f"配置中已有 [{table}]（如需覆盖请加 --force）")
    start, end = span
    own_end = start + 1
    for k in range(start + 1, end):
        s = lines[k].strip()
        if s and not s.startswith("#"):
            own_end = k + 1
    if marker and start > 0 and lines[start - 1].lstrip().startswith(marker):
        start -= 1
    lines[start:own_end] = block
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str) -> None:
    tomllib.loads(text)  # 写回前再校验一次：不能解析的内容绝不落盘
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
