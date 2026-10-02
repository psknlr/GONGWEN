"""追加式会话日志（借鉴 Codex rollout 与 DeepSeek Harness 的 append-only session log）。

两条原则同时成立：
1. 可重建：凡进入模型请求的内容，都能由“日志 + 受控存储”重建——日志记录的是提示模板 ID、
   对象 ID 与内容哈希，而不是材料副本。
2. 最小化（设计 §7.3）：默认不保存材料全文与冗长推理文本；日志中的大字段只存哈希与摘要。

日志使用哈希链（每条记录包含上一条的哈希），便于发现篡改；支持恢复、分叉、检索与回放。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..schemas.common import sha256_text

GENESIS = "0" * 64
_MAX_INLINE = 400  # 超过该长度的字符串字段只记录哈希与前缀


def _minimize(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(value, str):
        if len(value) > _MAX_INLINE:
            return {"sha256": sha256_text(value), "chars": len(value), "head": value[:80]}
        return value
    if isinstance(value, dict):
        return {k: _minimize(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 50:
            return {"items": len(value), "sha256": sha256_text(json.dumps(value, ensure_ascii=False, default=str))}
        return [_minimize(v, depth + 1) for v in value]
    return value


class SessionLog:
    def __init__(self, path: str | Path, minimize: bool = True, on_append: Callable[[dict[str, Any]], None] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.minimize = minimize
        self.on_append = on_append  # 事件订阅（如无头模式输出 NDJSON）；只收到最小化后的记录
        self._lock = threading.Lock()
        self._seq, self._last_hash = self._tail()

    # ------------------------------------------------------------------
    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        seq, last = 0, GENESIS
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                seq, last = rec["seq"], rec["hash"]
        return seq, last

    def append(self, type_: str, payload: dict[str, Any] | None = None, actor: str = "system", stage: str | None = None) -> dict[str, Any]:
        with self._lock:
            body = {
                "seq": self._seq + 1,
                "ts": datetime.now(timezone.utc).isoformat(),
                "type": type_,
                "actor": actor,
                "stage": stage,
                "payload": _minimize(payload or {}) if self.minimize else (payload or {}),
                "prev": self._last_hash,
            }
            digest = sha256_text(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str))
            body["hash"] = digest
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, ensure_ascii=False, default=str) + "\n")
            self._seq, self._last_hash = body["seq"], digest
        if self.on_append is not None:
            try:
                self.on_append(body)
            except Exception:  # 订阅方异常不影响审计记录本身
                pass
        return body

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self.replay()

    def replay(self, types: set[str] | None = None) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if types is None or rec["type"] in types:
                    yield rec

    def verify_chain(self) -> tuple[bool, int | None]:
        """校验哈希链；返回 (是否完整, 第一处断裂的序号)。"""
        prev = GENESIS
        for rec in self.replay():
            body = {k: v for k, v in rec.items() if k != "hash"}
            if body.get("prev") != prev:
                return False, rec["seq"]
            digest = sha256_text(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str))
            if digest != rec["hash"]:
                return False, rec["seq"]
            prev = rec["hash"]
        return True, None

    def search(self, text: str) -> list[dict[str, Any]]:
        out = []
        for rec in self.replay():
            if text in json.dumps(rec, ensure_ascii=False):
                out.append(rec)
        return out

    def fork(self, new_path: str | Path, upto_seq: int | None = None) -> "SessionLog":
        """复制到指定序号为止的日志，用于从某一步分叉尝试另一方案。"""
        new_path = Path(new_path)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        with new_path.open("w", encoding="utf-8") as out:
            for rec in self.replay():
                if upto_seq is not None and rec["seq"] > upto_seq:
                    break
                out.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        forked = SessionLog(new_path, self.minimize)
        forked.append("session.forked", {"from": str(self.path), "upto_seq": upto_seq})
        return forked

    def completed_idempotency_keys(self) -> set[str]:
        """恢复执行时用于跳过已完成的副作用动作（外发、导出等）。"""
        keys = set()
        for rec in self.replay({"tool.result"}):
            key = rec.get("payload", {}).get("idempotency_key")
            if key and rec.get("payload", {}).get("ok"):
                keys.add(key)
        return keys

    @property
    def seq(self) -> int:
        return self._seq
