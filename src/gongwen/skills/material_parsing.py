"""技能2 材料解析：解析正文、表格、附件及其结构关系，输出带原文定位的 SourceBundle。

原则：原始材料不变、结构化副本可加工；金额、日期、人名等关键字段不只依赖一次自动识别；
保留“正文—附件—表格注释”的关系（如表格脚注“仅统计已验收项目”不能在汇总时被丢弃）。
"""

from __future__ import annotations

import re

from ..parsing import parse_bytes
from ..schemas.common import AdmissionDecision
from ..schemas.sources import Material, SourceBundle, SourceRelation
from ..schemas.state import Stage
from .base import Skill, SkillContext

_ROLE_CUES = [
    ("example", re.compile(r"(示例|样例|模板|范文|参考格式|填写说明|×××|XXX)")),
    ("approval_candidate", re.compile(r"(批复|同意.{0,20}的(复函|批复)|签批|审批意见|已批准)")),
    ("meeting_record", re.compile(r"(会议记录|会议纪要|会议议定|出席人员|主持人)")),
    ("policy_candidate", re.compile(r"(管理办法|实施细则|暂行规定|规定》|条例》|第[一二三四五六七八九十]+条)")),
    ("statistics", re.compile(r"(统计表|汇总表|测算表|明细表|台账)")),
    ("existing_doc", re.compile(r"(关于.{2,40}的(通知|意见|决定|方案))")),
]


def guess_role(mat: Material, head_text: str) -> str:
    if mat.role and mat.role != "material":
        return mat.role
    probe = mat.filename + "\n" + head_text[:600]
    for role, rx in _ROLE_CUES:
        if rx.search(probe):
            return role
    return "material"


class MaterialParsingSkill(Skill):
    name = "gongwen-material-parsing"
    number = 2
    title = "材料解析"
    stage = Stage.PARSING
    channel_name = "parser"
    allowed_tools = ()
    output_artifact = "source_bundle"

    def run(self, sc: SkillContext, materials: list[Material]) -> SourceBundle:
        store = sc.runtime.materials
        bundle = SourceBundle()
        for mat in materials:
            if mat.admission != AdmissionDecision.ALLOW:
                bundle.warnings.append(f"{mat.material_id}（{mat.filename}）未获准入，未解析")
                continue
            data = store.read_bytes(mat)
            res = parse_bytes(mat.material_id, mat.filename, data)
            head = "\n".join(u.text for u in res.units[:20])
            role = guess_role(mat, head)
            if role != mat.role:
                mat.role = role
                store.save_meta(mat)
            bundle.materials.append(mat)
            bundle.units.extend(res.units)
            bundle.tables.extend(res.tables)
            bundle.relations.extend(res.relations)
            bundle.warnings.extend(f"{mat.material_id}：{w}" for w in res.warnings)
            # 附件关系：文件名或首段中的“附件N”
            m = re.search(r"附件\s*(\d+)", mat.filename + head[:50])
            if m:
                bundle.relations.append(SourceRelation(kind="attachment_of", src=mat.material_id, dst=f"attachment:{m.group(1)}"))
        sc.note(
            "skill.material_parsing",
            {
                "materials": [m.material_id for m in bundle.materials],
                "units": len(bundle.units),
                "tables": len(bundle.tables),
                "relations": len(bundle.relations),
                "warnings": len(bundle.warnings),
            },
        )
        return bundle
