"""技能4 政策依据检索：文号与关键词精确检索 + BM25 + 适用范围过滤 + 版本核验 + 条款定位。

不仅寻找支持拟议措施的依据，还检索可能限制该措施的条件、例外和相反要求。
每条依据回答三个问题：原文确实存在吗？它适用于本主体、本事项和本时点吗？
它确实支持拟写的具体表述吗？（第三问在起草后由审校通道逐句判断）
"""

from __future__ import annotations

from ..knowledge.retrieval import coverage, find_doc_numbers, find_titles
from ..schemas.genre import GenreDecision
from ..schemas.policy import PolicyEvidence, PolicyPack
from ..schemas.sources import SourceBundle
from ..schemas.state import Stage
from ..schemas.task import TaskSpec
from .base import Skill, SkillContext

RELEVANCE_THRESHOLD = 0.3


def is_substantive(e: PolicyEvidence, lib, subject: str) -> bool:
    """能否在正文中作为本事项的实体依据：实体性文件 + 支持角色 + 适用 + 与事由相关。

    检索命中不等于可以引用：例如“基层医疗”与“基层减负”字面相近，但后者约束发文行为，
    不是申请医疗经费的依据。"""
    p = lib.get(e.policy_id)
    if p is None or p.basis_role != "substantive" or e.role != "support" or not e.applicability.applicable:
        return False
    hay = f"{p.title} {' '.join(p.matters)} {e.quote}"
    return coverage(subject, hay) >= RELEVANCE_THRESHOLD

GENRE_RULE_ARTICLES = {
    "请示": [("gongwen-tiaoli-2012", "第十五条"), ("gongwen-tiaoli-2012", "第八条")],
    "报告": [("gongwen-tiaoli-2012", "第十五条"), ("gongwen-tiaoli-2012", "第八条")],
    "通知": [("gongwen-tiaoli-2012", "第十六条"), ("gongwen-tiaoli-2012", "第十三条")],
    "函": [("gongwen-tiaoli-2012", "第八条"), ("gongwen-tiaoli-2012", "第十七条")],
    "纪要": [("gongwen-tiaoli-2012", "第八条")],
    "批复": [("gongwen-tiaoli-2012", "第八条")],
    "意见": [("gongwen-tiaoli-2012", "第八条")],
    "决定": [("gongwen-tiaoli-2012", "第八条")],
    "通报": [("gongwen-tiaoli-2012", "第八条")],
}


class PolicyRetrievalSkill(Skill):
    name = "gongwen-policy-retrieval"
    number = 4
    title = "政策依据检索"
    stage = Stage.EVIDENCE
    channel_name = "retriever"
    allowed_tools = ("gongwen_policy_search",)
    output_artifact = "policy_pack"

    def run(self, sc: SkillContext, spec: TaskSpec, genre: GenreDecision, bundle: SourceBundle | None = None) -> PolicyPack:
        lib = sc.runtime.policies
        profile = sc.runtime.profile
        subject_types = profile.get("subject_types") or []
        region = str(spec.region.value) if spec.region.known else None
        as_of = spec.policy_as_of
        pack = PolicyPack(as_of=as_of, region=region, subject_type="、".join(subject_types) or None)
        corpus = spec.request_text + "\n" + ("\n".join(u.text for u in bundle.units) if bundle else "")
        terms = [str(spec.subject.value or "")] + spec.purposes + spec.resource_mentions
        if genre.suggested_genre:
            terms.append(genre.suggested_genre)
        pack.query_terms = [t for t in terms if t]
        query = " ".join(pack.query_terms)
        seen: set[tuple[str, str | None]] = set()

        def add(policy, article, method, score, role="support"):
            key = (policy.policy_id, article.article_no if article else None)
            if key in seen:
                return
            seen.add(key)
            ap = lib.applicability(policy, as_of, region, subject_types) if sc.features.temporal_check else lib.applicability(policy, policy.effective_date or as_of)
            ev = PolicyEvidence(
                evidence_id=sc.ids.next("P"),
                policy_id=policy.policy_id,
                article_no=article.article_no if article else None,
                quote=(article.text if article else (policy.notes or policy.title))[:800],
                citation=policy.cite(article.article_no if article and article.article_no.startswith("第") else None),
                applicability=ap,
                retrieval=method,
                score=round(score, 3),
                role=role,
                version=policy.version,
                verification=article.verification if article else policy.verification,
            )
            if role == "rule":
                ev.support_status = "支持"
            (pack.excluded if ap.applicable is False else pack.items).append(ev)

        # 1) 精确检索：需求与材料中出现的文号、书名号标题
        for num in find_doc_numbers(corpus):
            p = lib.lookup_number(num)
            if p is None:
                pack.gaps.append(f"材料或需求引用了“{num}”，依据库中查无此文，无法核验原文与效力")
                continue
            arts = lib.search(query, k=3, policy_ids={p.policy_id})
            if arts:
                for h in arts:
                    add(p, h.article, "exact_docno", 100 + h.score)
            else:
                add(p, None, "exact_docno", 100)
        for title in find_titles(corpus):
            p = lib.lookup_title(title)
            if p is None:
                if any(k in title for k in ("办法", "规定", "条例", "意见", "通知", "细则", "法")):
                    pack.gaps.append(f"引用的《{title}》不在依据库中，需补充原文、发文机关、文号和施行日期后才能作为依据")
                continue
            arts = lib.search(query, k=2, policy_ids={p.policy_id})
            for h in arts or []:
                add(p, h.article, "exact_title", 90 + h.score)
        # 2) 文种相关的行文规则（程序性依据）
        for pid, art in GENRE_RULE_ARTICLES.get(genre.suggested_genre or "", []):
            p = lib.get(pid)
            a = p.article(art) if p else None
            if p and a:
                add(p, a, "manual", 50, role="rule")
        # 3) BM25：实体依据
        for h in lib.search(query, k=8):
            if h.score < 3.0:
                continue
            role = "rule" if h.policy.basis_role == "procedural" else "support"
            add(h.policy, h.article, "bm25", h.score, role=role)
        # 4) 限制条件、例外与相反要求
        for h in lib.constraints(query, k=4):
            add(h.policy, h.article, "bm25", h.score, role="constraint")
        # 5) 冲突：呈现给专业审核，不用“新文件优先”代替判断
        pack.conflicts = lib.conflicts([e.policy_id for e in pack.items], as_of)
        substantive = [e for e in pack.items if is_substantive(e, lib, str(spec.subject.value or ""))]
        if not substantive and (spec.resource_mentions or genre.suggested_genre in ("请示", "通知", "意见", "决定")):
            pack.gaps.append("未检索到本事项的实体依据（如专项资金管理办法、上级部署文件）。请补充相关文件；系统不会以相似地区文件替代本单位依据")
        for m in bundle.materials if bundle else []:
            if m.role == "policy_candidate":
                pack.gaps.append(f"材料 {m.material_id}（{m.filename}）疑似政策文件：需由有权人员登记发文机关、文号、施行日期和适用范围后，才能作为依据引用")
        sc.note("skill.policy_retrieval", {"items": len(pack.items), "excluded": len(pack.excluded), "conflicts": len(pack.conflicts), "gaps": len(pack.gaps)})
        return pack
