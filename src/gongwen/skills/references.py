"""来文识别与称谓：批复、复函等需要引用来文标题和发文字号，并按受文机关确定“你委/你局/贵局”。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..knowledge.retrieval import find_doc_numbers
from ..schemas.sources import SourceBundle

_INCOMING_TITLE = re.compile(r"^(.{0,40}?关于.{2,60}的(请示|报告|函|意见))$")
_SHORT = [("委员会", "委"), ("委", "委"), ("医院", "院"), ("研究院", "院"), ("学院", "院"), ("大学", "校"), ("学校", "校"), ("研究所", "所"), ("局", "局"), ("厅", "厅"), ("办公室", "办"), ("中心", "中心"), ("公司", "公司")]


@dataclass
class Incoming:
    material_id: str
    title: str
    genre: str
    doc_number: str


def short_name(organ: str) -> str:
    """机关简称用字：示例市卫生健康委员会→委，示例区人民政府→区。"""
    organ = organ.strip()
    if organ.endswith("人民政府"):
        ch = organ[-5:-4]
        return ch if ch in "省市区县镇乡" else "市"
    for suffix, ch in _SHORT:
        if organ.endswith(suffix):
            return ch
    return "单位"


_REPLY_RE = re.compile(r"复函|函复|回函|(答复|回复)[^，。]{0,12}(来函|函|询问|征求意见)")


def is_reply(request_text: str, doc_kind: str) -> bool:
    """答复类文稿：批复，或答复来函的复函。"""
    return doc_kind == "批复" or (doc_kind == "函" and bool(_REPLY_RE.search(request_text or "")))


def incoming_core(title: str) -> str:
    """来文事由：“示例市财政局关于商请提供……情况的函”→“提供……情况”。"""
    core = re.sub(r"^.*?关于", "", title)
    core = re.sub(r"的(请示|报告|函|意见)$", "", core)
    return re.sub(r"^(商请|恳请|请求|请)(贵[^\s]{1,2})?", "", core) or core


def addressee(organ: str, direction: str = "下行文") -> str:
    """下行文用“你X”，平行文与不相隶属机关用“贵X”。"""
    return ("你" if direction == "下行文" else "贵") + short_name(organ)


def find_incoming(bundle: SourceBundle | None) -> Incoming | None:
    """在材料中找来文：标题形如“××关于××的请示”，发文字号取标题前后几行内的第一个文号。"""
    if not bundle:
        return None
    by_mat: dict[str, list[str]] = {}
    for u in bundle.units:
        if u.kind in ("comment", "table_cell", "sheet_cell"):
            continue
        by_mat.setdefault(u.material_id, []).append(u.text.strip())
    for mid, lines in by_mat.items():
        for i, line in enumerate(lines[:8]):
            m = _INCOMING_TITLE.match(line)
            if not m:
                continue
            nearby = " ".join(lines[max(0, i - 2) : i + 3])
            nums = find_doc_numbers(nearby)
            return Incoming(material_id=mid, title=line, genre=m.group(2), doc_number=nums[0] if nums else "")
    return None


__all__ = ["Incoming", "addressee", "find_incoming", "incoming_core", "is_reply", "short_name"]
