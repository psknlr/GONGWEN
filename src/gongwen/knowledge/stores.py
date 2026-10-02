"""数据区二～五的存储实现（逻辑隔离）：

* 本事项材料库 MaterialStore：原始文件按内容哈希只读存放，结构化副本另存；
* 历史案例与文风库 CaseLibrary：只提供结构与表达参考，检索结果中的数字、机构、日期
  一律脱敏，杜绝“范文里的事实”进入本次事实账本；
* 任务与审计成果库 TaskStore：任务状态、阶段产物、版本与输出，访问需经权限引擎。
（数据区一 权威依据库见 policy_library.py；数据区四 可执行规则库见 rules/registry.py）
"""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from ..schemas.common import Clearance, sha256_bytes, stable_hash
from ..schemas.sources import Material
from .kb import SEED_DIR
from .retrieval import BM25, Doc

T = TypeVar("T", bound=BaseModel)


class Zone(str, Enum):
    POLICY = "权威规范与政策依据库"
    MATTER = "本事项材料库"
    CASES = "历史案例与文风库"
    RULES = "可执行规则库"
    AUDIT = "任务与审计成果库"


# 各数据区可以支撑什么：事实只能来自本事项材料库；依据只能来自依据库；案例库只提供文风
ZONE_CAPABILITIES = {
    Zone.POLICY: {"basis": True, "fact": False, "style": False},
    Zone.MATTER: {"basis": False, "fact": True, "style": False},
    Zone.CASES: {"basis": False, "fact": False, "style": True},
    Zone.RULES: {"basis": False, "fact": False, "style": False},
    Zone.AUDIT: {"basis": False, "fact": False, "style": False},
}

MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
}


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def safe_id(value: str, what: str = "标识") -> str:
    """任务、事项、材料标识只允许字母数字与 _ . -，防止路径穿越（例如经本地审阅服务传入的 ID）。"""
    if not isinstance(value, str) or not SAFE_ID_RE.match(value) or ".." in value:
        raise KeyError(f"非法{what}：{value!r}")
    return value


def guess_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return MEDIA_TYPES.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


class MaterialStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _mdir(self, matter_id: str) -> Path:
        d = self.root / "matters" / safe_id(matter_id, "事项标识") / "materials"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put(
        self,
        matter_id: str,
        material_id: str,
        filename: str,
        data: bytes,
        uploaded_by: str,
        declared: Clearance | None = None,
        role: str = "material",
        description: str = "",
    ) -> Material:
        digest = sha256_bytes(data)
        mdir = self._mdir(matter_id)
        blob = mdir / "blobs" / digest
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists():
            blob.write_bytes(data)
            blob.chmod(0o444)  # 原始材料不变
        mat = Material(
            material_id=material_id,
            filename=Path(filename).name,
            media_type=guess_media_type(filename),
            sha256=digest,
            size=len(data),
            matter_id=matter_id,
            declared_clearance=declared,
            uploaded_by=uploaded_by,
            role=role,
            description=description,
        )
        self.save_meta(mat)
        return mat

    def save_meta(self, mat: Material) -> None:
        (self._mdir(mat.matter_id) / f"{mat.material_id}.json").write_text(
            mat.model_dump_json(indent=2), encoding="utf-8"
        )

    def get(self, matter_id: str, material_id: str) -> Material:
        p = self._mdir(matter_id) / f"{material_id}.json"
        return Material.model_validate_json(p.read_text(encoding="utf-8"))

    def list(self, matter_id: str) -> list[Material]:
        return [
            Material.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self._mdir(matter_id).glob("*.json"))
        ]

    def blob_path(self, mat: Material) -> Path:
        return self._mdir(mat.matter_id) / "blobs" / mat.sha256

    def read_bytes(self, mat: Material) -> bytes:
        data = self.blob_path(mat).read_bytes()
        if sha256_bytes(data) != mat.sha256:
            raise IOError(f"材料 {mat.material_id} 校验值不符，原始文件可能被篡改")
        return data

    def purge(self, matter_id: str, material_id: str) -> None:
        """删除不予准入的材料（禁止进入当前环境）。"""
        mat = self.get(matter_id, material_id)
        blob = self.blob_path(mat)
        if blob.exists():
            blob.chmod(0o644)
            blob.unlink()
        (self._mdir(matter_id) / f"{material_id}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
_MASKS = [
    (re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"), "×年×月×日"),
    (re.compile(r"\d{4}年\d{1,2}月"), "×年×月"),
    (re.compile(r"\d{4}年"), "×年"),
    (re.compile(r"\d+(?:\.\d+)?(?:万元|亿元|元|%|个|家|人|项|所|次|台|套|平方米|公里)?"), "×"),
    (re.compile(r"[一-鿿]{2,12}(?:人民政府|委员会|局|厅|办公室|医院|大学|学院|中心|公司)"), "××单位"),
    (re.compile(r"〔×〕×号|〔\d{4}〕\d+号"), "〔×〕×号"),
]


def mask_specifics(text: str) -> str:
    for rx, rep in _MASKS:
        text = rx.sub(rep, text)
    return text


class CaseLibrary:
    """历史案例与文风库：经授权、审核的范文与模板，仅用于学习结构、术语和表达。"""

    def __init__(self, extra_paths: list[str | Path] | None = None):
        data = yaml.safe_load((SEED_DIR / "cases.yaml").read_text(encoding="utf-8")) or []
        for p in extra_paths or []:
            p = Path(p)
            if p.is_file():
                data += yaml.safe_load(p.read_text(encoding="utf-8")) or []
        self.cases = data
        self.index = BM25()
        self.index.add(Doc(c["case_id"], f"{c['genre']} {c.get('scenario', '')} {c.get('text', '')}", c) for c in data)

    def style_refs(self, genre: str | None, query: str = "", k: int = 3) -> list[dict[str, Any]]:
        cands = [c for c in self.cases if not genre or c["genre"] == genre]
        if query:
            ranked = self.index.search(query, k=k * 3, filter_fn=lambda d: not genre or d.meta["genre"] == genre)
            ids = [d.doc_id for d, _ in ranked]
            cands = sorted(cands, key=lambda c: ids.index(c["case_id"]) if c["case_id"] in ids else 99)
        out = []
        for c in cands[:k]:
            out.append(
                {
                    "case_id": c["case_id"],
                    "genre": c["genre"],
                    "scenario": c.get("scenario", ""),
                    "structure": c.get("structure", []),
                    "phrases": c.get("phrases", {}),
                    # 脱敏：范文中的数字、机构、日期不得进入本次事实账本
                    "text_masked": mask_specifics(c.get("text", "")),
                    "zone": Zone.CASES.value,
                    "usage": "仅供结构与表达参考，不提供本次事项的任何事实",
                }
            )
        return out


# ---------------------------------------------------------------------------
class TaskStore:
    """任务与审计成果库：任务状态、阶段产物（带内容哈希）、文稿版本与输出。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def task_dir(self, task_id: str) -> Path:
        d = self.root / "tasks" / safe_id(task_id, "任务标识")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def exists(self, task_id: str) -> bool:
        try:
            return (self.root / "tasks" / safe_id(task_id, "任务标识") / "state.json").is_file()
        except KeyError:
            return False

    def list_tasks(self) -> list[str]:
        base = self.root / "tasks"
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if (p / "state.json").is_file())

    def save_model(self, task_id: str, name: str, obj: BaseModel) -> str:
        d = self.task_dir(task_id) / "artifacts"
        d.mkdir(exist_ok=True)
        text = obj.model_dump_json(indent=2)
        (d / f"{name}.json").write_text(text, encoding="utf-8")
        return stable_hash(obj)

    def load_model(self, task_id: str, name: str, cls: type[T]) -> T | None:
        p = self.task_dir(task_id) / "artifacts" / f"{name}.json"
        if not p.is_file():
            return None
        return cls.model_validate_json(p.read_text(encoding="utf-8"))

    def has(self, task_id: str, name: str) -> bool:
        return (self.task_dir(task_id) / "artifacts" / f"{name}.json").is_file()

    def save_version(self, task_id: str, doc_id: str, version: int, obj: BaseModel) -> str:
        d = self.task_dir(task_id) / "versions"
        d.mkdir(exist_ok=True)
        (d / f"{doc_id}.v{version}.json").write_text(obj.model_dump_json(indent=2), encoding="utf-8")
        return stable_hash(obj)

    def load_version(self, task_id: str, doc_id: str, version: int, cls: type[T]) -> T | None:
        p = self.task_dir(task_id) / "versions" / f"{doc_id}.v{version}.json"
        if not p.is_file():
            return None
        return cls.model_validate_json(p.read_text(encoding="utf-8"))

    def versions(self, task_id: str, doc_id: str) -> list[int]:
        d = self.task_dir(task_id) / "versions"
        if not d.is_dir():
            return []
        out = []
        for p in d.glob(f"{doc_id}.v*.json"):
            try:
                out.append(int(p.stem.rsplit(".v", 1)[1]))
            except ValueError:
                continue
        return sorted(out)

    def out_dir(self, task_id: str) -> Path:
        d = self.task_dir(task_id) / "out"
        d.mkdir(exist_ok=True)
        return d

    def write_json(self, task_id: str, name: str, data: Any) -> Path:
        p = self.task_dir(task_id) / name
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return p

    def remove(self, task_id: str) -> None:
        shutil.rmtree(self.root / "tasks" / task_id, ignore_errors=True)
