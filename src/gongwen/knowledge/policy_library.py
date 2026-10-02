"""权威规范与政策依据库（数据区一）。

每份依据记录：文件标识、标题、发文机关、发文字号、发布/施行/失效或废止日期、
适用地域、适用主体、事项范围、效力状态、版本与替代关系、原文来源、采集时间、
内容校验值、条款定位、人工核验状态（设计 §4.3）。

规则冲突时呈现相关文件、适用条件和冲突点，转交专业审核；不使用“新文件总是优先”
或“级别更高就一定适用”的简单排序代替判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import yaml

from ..schemas.common import RuleLevel, sha256_text
from ..schemas.policy import Applicability, PolicyArticle, PolicyConflict, PolicyDocument
from .kb import SEED_DIR
from .retrieval import BM25, Doc, find_doc_numbers, find_titles, normalize_doc_number

LIMITING_MARKERS = ("不得", "禁止", "严禁", "除", "应当经", "未经", "原则上", "须经", "不应", "限于", "例外")


@dataclass
class Hit:
    policy: PolicyDocument
    article: PolicyArticle | None
    score: float
    method: str


def _parse_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _load_policy(raw: dict, base_dir: Path) -> PolicyDocument:
    raw = dict(raw)
    articles_file = raw.pop("articles_file", None)
    articles = [PolicyArticle(**a) for a in raw.pop("articles", [])]
    if articles_file:
        data = json.loads((base_dir / articles_file).read_text(encoding="utf-8"))
        for a in data["articles"]:
            vs = a.get("verified_sources", 0)
            articles.append(
                PolicyArticle(
                    article_no=a["no"],
                    chapter=a.get("chapter", ""),
                    text=a["text"],
                    verbatim=True,
                    verification=f"已核（{vs}个独立来源逐字一致）" if vs >= 2 else ("已核（单源）" if vs == 1 else "待核"),
                )
            )
    for k in ("publish_date", "effective_date", "expiry_date", "repeal_date"):
        raw[k] = _parse_date(raw.get(k))
    if "level" in raw and not isinstance(raw["level"], RuleLevel):
        raw["level"] = RuleLevel(raw["level"])
    doc = PolicyDocument(**raw, articles=articles)
    if doc.content_hash is None:
        doc.content_hash = sha256_text("\n".join(f"{a.article_no}{a.text}" for a in doc.articles))
    return doc


class PolicyLibrary:
    def __init__(self, extra_paths: Iterable[str | Path] = (), include_seed: bool = True, allow_synthetic: bool = False):
        self.docs: dict[str, PolicyDocument] = {}
        self.allow_synthetic = allow_synthetic  # 仅用于演示与评测：示例依据可参与流程，但始终标注“示例数据”
        if include_seed:
            self.load_file(SEED_DIR / "policies.yaml")
        for p in extra_paths:
            p = Path(p)
            if p.is_dir():
                for f in sorted(p.glob("*.yaml")):
                    self.load_file(f)
            elif p.is_file():
                self.load_file(p)
        self._reindex()

    # ------------------------------------------------------------ 加载
    def load_file(self, path: str | Path) -> None:
        path = Path(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(data, dict):
            data = data.get("policies", [])
        for raw in data:
            doc = _load_policy(raw, path.parent)
            self.docs[doc.policy_id] = doc

    def add(self, doc: PolicyDocument) -> None:
        """登记新依据。调用方必须先通过权限引擎（policy.promote 仅限人工）。"""
        if not doc.issuers or (doc.effective_date is None and doc.status == "现行有效"):
            raise ValueError("登记依据须补全发文机关与施行日期，不能把转载日期当作施行日期")
        self.docs[doc.policy_id] = doc
        self._reindex()

    def _reindex(self) -> None:
        self.index = BM25()
        docs = []
        self.by_number: dict[str, str] = {}
        self.by_title: dict[str, str] = {}
        for p in self.docs.values():
            if p.doc_number:
                self.by_number[normalize_doc_number(p.doc_number)] = p.policy_id
                self.by_number[p.doc_number] = p.policy_id
            self.by_title[p.title] = p.policy_id
            if not p.articles:
                docs.append(Doc(f"{p.policy_id}#", f"{p.title} {p.notes}", {"policy_id": p.policy_id, "article": None}))
            for a in p.articles:
                docs.append(
                    Doc(
                        f"{p.policy_id}#{a.article_no}",
                        f"{p.title} {a.chapter} {a.text} {' '.join(a.tags)}",
                        {"policy_id": p.policy_id, "article": a.article_no},
                    )
                )
        self.index.add(docs)

    # ------------------------------------------------------------ 查询
    def get(self, policy_id: str) -> PolicyDocument | None:
        return self.docs.get(policy_id)

    def lookup_number(self, number: str) -> PolicyDocument | None:
        pid = self.by_number.get(normalize_doc_number(number)) or self.by_number.get(number)
        return self.docs.get(pid) if pid else None

    def lookup_title(self, title: str) -> PolicyDocument | None:
        title = title.strip("《》 ")
        pid = self.by_title.get(title)
        if pid:
            return self.docs[pid]
        for t, pid in self.by_title.items():
            if title and (title in t or t in title) and min(len(title), len(t)) >= 6:
                return self.docs[pid]
        return None

    def applicability(
        self,
        policy: PolicyDocument,
        as_of: date,
        region: str | None = None,
        subject_type: str | list[str] | None = None,
    ) -> Applicability:
        reasons: list[str] = []
        temporal_ok: bool | None = True
        synthetic_blocked = policy.synthetic and not self.allow_synthetic
        if policy.synthetic:
            reasons.append("示例/合成数据，不得作为真实依据" if synthetic_blocked else "示例数据（仅演示/评测环境可用，正式办文不得引用）")
        if policy.effective_date and as_of < policy.effective_date:
            temporal_ok = False
            reasons.append(f"适用时点 {as_of} 早于施行日期 {policy.effective_date}")
        end = policy.repeal_date or policy.expiry_date
        if end and as_of >= end:
            temporal_ok = False
            reasons.append(f"适用时点 {as_of} 时该文件已失效或废止（{end}）")
        if policy.status in ("已废止", "失效") and end is None:
            temporal_ok = None
            reasons.append("文件状态为已废止/失效但缺少废止日期，需人工核实适用时点")
        if policy.effective_date is None and policy.status not in ("已废止", "失效"):
            if policy.publish_date is not None:
                if as_of < policy.publish_date:
                    temporal_ok = False
                    reasons.append(f"适用时点 {as_of} 早于该文件成文/发布日期 {policy.publish_date}")
                else:
                    reasons.append(f"该文件无施行日期条款，以成文/发布日期（{policy.publish_date}）作为适用起点")
            else:
                temporal_ok = None if temporal_ok else temporal_ok
                reasons.append("缺少施行日期，时点适用性需人工核实")
        region_ok: bool | None = True
        if "全国" not in policy.regions:
            if not region:
                region_ok = None
                reasons.append(f"地方性文件（{'、'.join(policy.regions)}），任务地域未确认")
            elif not any(r in region or region in r for r in policy.regions):
                region_ok = False
                reasons.append(f"适用地域为{'、'.join(policy.regions)}，与任务地域“{region}”不符；不能把相似地区文件当作本单位依据")
        subject_ok: bool | None = True
        types = [subject_type] if isinstance(subject_type, str) else list(subject_type or [])
        if types and "通用" not in policy.subjects and not set(types) & set(policy.subjects):
            label = "、".join(types)
            if policy.subject_mode == "参照" or "其他机关和单位（参照）" in policy.subjects:
                reasons.append(f"本单位（{label}）为参照执行，不能视为与适用主体同等的发文权限")
            else:
                subject_ok = None
                reasons.append(f"适用主体为{'、'.join(policy.subjects)}，本单位类型“{label}”需人工确认是否适用")
        verdicts = [temporal_ok, region_ok, subject_ok]
        if synthetic_blocked or False in verdicts:
            applicable: bool | None = False
        elif None in verdicts:
            applicable = None
        else:
            applicable = True
        return Applicability(
            applicable=applicable,
            as_of=as_of,
            reasons=reasons,
            temporal_ok=temporal_ok,
            region_ok=region_ok,
            subject_ok=subject_ok,
        )

    def exact(self, text: str) -> list[Hit]:
        hits: list[Hit] = []
        for num in find_doc_numbers(text):
            p = self.lookup_number(num)
            if p:
                hits.append(Hit(p, None, 100.0, "exact_docno"))
        for t in find_titles(text):
            p = self.lookup_title(t)
            if p and all(h.policy.policy_id != p.policy_id for h in hits):
                hits.append(Hit(p, None, 90.0, "exact_title"))
        return hits

    def search(self, query: str, k: int = 8, policy_ids: set[str] | None = None) -> list[Hit]:
        flt = (lambda d: d.meta["policy_id"] in policy_ids) if policy_ids else None
        out = []
        for d, s in self.index.search(query, k=k, filter_fn=flt):
            p = self.docs[d.meta["policy_id"]]
            a = p.article(d.meta["article"]) if d.meta["article"] else None
            out.append(Hit(p, a, s, "bm25"))
        return out

    def constraints(self, query: str, k: int = 5) -> list[Hit]:
        """检索可能限制拟议措施的条件、例外和相反要求，防止只找支持预设结论的材料。"""
        hits = self.search(query, k=k * 4)
        out = [h for h in hits if h.article and any(m in h.article.text for m in LIMITING_MARKERS)]
        return out[:k]

    def conflicts(self, policy_ids: list[str], as_of: date) -> list[PolicyConflict]:
        out: list[PolicyConflict] = []
        ids = set(policy_ids)
        for pid in ids:
            p = self.docs.get(pid)
            if not p:
                continue
            for other in p.supersedes:
                if other in ids:
                    out.append(
                        PolicyConflict(
                            policies=[pid, other],
                            point=f"《{p.title}》与其所替代的文件同时被检索为依据",
                            conditions="需核对两者在适用时点上的效力衔接，不能简单认定新文件优先",
                        )
                    )
        # 结构化约束冲突：同一约束键、不同取值
        keyed: dict[str, list[tuple[str, str]]] = {}
        for pid in ids:
            p = self.docs.get(pid)
            if not p:
                continue
            for a in p.articles:
                for tag in a.tags:
                    if tag.startswith("约束:") and "=" in tag:
                        key, val = tag[3:].split("=", 1)
                        keyed.setdefault(key, []).append((f"{pid}{a.article_no}", val))
        for key, vals in keyed.items():
            if len({v for _, v in vals}) > 1:
                out.append(
                    PolicyConflict(
                        policies=[x for x, _ in vals],
                        point=f"关于“{key}”的规定不一致：" + "；".join(f"{x}={v}" for x, v in vals),
                        conditions="请专业人员结合适用主体、地域和时点判断",
                    )
                )
        return out
