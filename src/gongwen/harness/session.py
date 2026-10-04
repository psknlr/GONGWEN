"""追加式会话日志（借鉴 Codex rollout 与 DeepSeek Harness 的 append-only session log）。

两条原则同时成立：
1. 可重建：凡进入模型请求的内容，都能由“日志 + 受控存储”重建——日志记录的是提示模板 ID、
   对象 ID 与内容哈希，而不是材料副本。
2. 最小化（设计 §7.3）：默认不保存材料全文与冗长推理文本；日志中的大字段只存哈希与摘要。

日志使用哈希链（每条记录包含上一条的哈希），便于发现篡改；支持恢复、分叉、检索与回放。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Iterator

from ..schemas.common import sha256_text

try:  # POSIX：同一任务的多个日志实例（多线程、多进程）经文件锁串行追加
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台退化为进程内锁
    fcntl = None

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
        with self.path.open("rb") as fh:
            return self._read_tail(fh)

    @staticmethod
    def _read_tail(fh: IO[bytes]) -> tuple[int, str]:
        """读取最后一条记录的序号与哈希（只读文件末尾）。"""
        fh.seek(0, os.SEEK_END)
        pos, buf = fh.tell(), b""
        while pos > 0 and b"\n" not in buf.rstrip():
            step = min(4096, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
        last = buf.rstrip().rsplit(b"\n", 1)[-1].strip()
        if not last:
            return 0, GENESIS
        rec = json.loads(last)
        return rec["seq"], rec["hash"]

    def append(self, type_: str, payload: dict[str, Any] | None = None, actor: str = "system", stage: str | None = None) -> dict[str, Any]:
        with self._lock, self.path.open("ab+") as fh:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)  # 文件关闭时释放
            # 同一任务可能同时存在多个日志实例：追加前重新读取链尾，不使用本实例缓存的序号与哈希
            self._seq, self._last_hash = self._read_tail(fh)
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
            fh.write((json.dumps(body, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            fh.flush()
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
