"""公文模板（可自行调整）：在基础版式配置档（默认 GB/T 9704—2012）之上叠加的具名改动，外加本单位信息。

* 存放：内置（只读，``layout/profiles/templates/``）、工作区 ``templates/``、数据目录 ``<data_dir>/templates/``（用户模板，
  可写）；同名时数据目录优先，其次工作区、内置。基础配置档本身（gbt9704-2012）也作为内置模板列出；
* 模板 YAML：``name``、``base``（默认 gbt9704-2012）、``description``，以及对 ``fonts``、``page``、``margins``、``type_area``、
  ``grid``、``elements.*``、``letter``、``command``、``jiyao``、``brief`` 的改动（深度合并），另有 ``unit``（单位信息）；
* 校验：未知设置项、字号名不在字号表、颜色不是十六进制、毫米数超出合理范围、版心与页边距不一致等一律拒绝，并给出中文说明；
* 偏离：逐项对照基础配置档，按条款与强度（规定 / 一般 / 推荐 / 可以 / 实务 / 推算）列出。“规定”“一般”“推荐”“可以”类参数的改动
  记为“偏离国标”；字库名、线宽等国标未规定的数值记为“实务调整”。各单位可依本单位公文处理细则偏离，但必须可见：
  模板查看、校验与排版报告（TEMPLATE、TPL-DEV 核验项）都会列出；渲染核验按模板生效参数比对，不把有意的改动判为不符合。
"""

from __future__ import annotations

import copy
import difflib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..schemas.ir import DocumentIR
from ..schemas.layout import LayoutCheck
from .fonts import ROLE_LABELS, ROLES
from .profile import MM_PER_PT, PROFILE_DIR, LayoutProfile, _raw

BUILTIN_DIR = PROFILE_DIR / "templates"
BASE_PROFILES = ("gbt9704-2012",)
NAME_RE = re.compile(r"^[^\W_][\w-]{0,39}$")
NATIONAL = ("规定", "一般", "推荐", "可以")

ROLE_ALIASES = {
    "仿宋": "fangsong", "仿宋体": "fangsong", "楷体": "kaiti", "楷": "kaiti", "黑体": "heiti", "黑": "heiti",
    "小标宋": "xiaobiaosong", "小标宋体": "xiaobiaosong", "宋体": "songti", "宋": "songti",
}


class TemplateError(ValueError):
    """模板内容不合规：errors 为逐项中文说明。"""

    def __init__(self, errors: list[str], name: str = ""):
        self.errors = errors
        head = f"模板“{name}”不合规" if name else "模板不合规"
        super().__init__(head + "：\n" + "\n".join(f"  · {e}" for e in errors))


# ---------------------------------------------------------------- 可设置项
R, S, C, B, SL = ("role",), ("size",), ("color",), ("bool",), ("strlist",)


def _mm(lo: float, hi: float) -> tuple:
    return ("num", lo, hi, "mm")


def _num(lo: float, hi: float, unit: str = "") -> tuple:
    return ("num", lo, hi, unit)


def _int(lo: int, hi: int, unit: str = "") -> tuple:
    return ("int", lo, hi, unit)


def _str(n: int) -> tuple:
    return ("str", n)


def _enum(*values: str) -> tuple:
    return ("enum", values)


SCHEMA: dict[str, Any] = {
    "fonts": {role: {"name": _str(64), "alternates": SL} for role in ROLES},
    "page": {"width_mm": _mm(100, 500), "height_mm": _mm(100, 500)},
    "margins": {"top_mm": _mm(5, 100), "left_mm": _mm(5, 100), "bottom_mm": _mm(5, 100), "right_mm": _mm(5, 100)},
    "type_area": {"width_mm": _mm(50, 400), "height_mm": _mm(50, 450)},
    "grid": {"lines_per_page": _int(10, 40, "行"), "chars_per_line": _int(10, 40, "字"), "line_pt": _num(12, 60, "磅"), "char_spacing_twips": _int(-60, 60, "缇")},
    "elements": {
        "default": {"font": R, "size": S},
        "copy_no": {"font": R, "size": S},
        "secrecy": {"font": R, "size": S},
        "urgency": {"font": R, "size": S},
        "organ_mark": {"font": R, "color": C, "top_from_type_area_mm": _mm(0, 120), "max_size": S},
        "doc_number": {"font": R, "size": S, "blank_lines_after_mark": _int(0, 6, "行")},
        "signer": {"label_font": R, "name_font": R, "size": S},
        "red_rule": {"below_doc_number_mm": _mm(0, 20), "color": C, "width_pt": _num(0.25, 6, "磅")},
        "title": {"font": R, "size": S, "blank_lines_before": _int(0, 6, "行"), "max_chars_per_line": _int(8, 30, "字")},
        "title_note": {"font": R, "size": S},
        "recipients": {"blank_lines_before": _int(0, 4, "行")},
        "body": {"font": R, "size": S, "first_indent_chars": _num(0, 4, "字")},
        "levels": {1: R, 2: R, 3: R, 4: R},
        "attachment_note": {"blank_lines_before": _int(0, 4, "行"), "left_indent_chars": _num(0, 6, "字")},
        "signature_seal": {"date_right_indent_chars": _num(0, 10, "字")},
        "signature_noseal": {"organ_right_indent_chars": _num(0, 10, "字"), "date_shift_chars": _num(0, 6, "字")},
        "note": {"left_indent_chars": _num(0, 6, "字")},
        "attachment": {"label_font": R, "size": S},
        "imprint": {"font": R, "size": S, "outer_rule_mm": _mm(0.1, 2), "inner_rule_mm": _mm(0.1, 2), "indent_chars": _num(0, 4, "字")},
        "page_number": {"font": R, "size": S, "dash_offset_mm": _mm(0, 20), "indent_chars": _num(0, 4, "字"), "style": _enum("dash", "plain")},
    },
    "letter": {
        "organ_mark_top_from_page_mm": _mm(5, 80), "rule_below_mark_mm": _mm(0, 20), "bottom_rule_from_page_mm": _mm(5, 60),
        "rule_length_mm": _mm(100, 200), "element_gap_ratio": _num(0, 3), "title_blank_lines": _int(0, 6, "行"),
        "rule_thick_pt": _num(0.25, 6, "磅"), "rule_thin_pt": _num(0.25, 6, "磅"), "rule_gap_pt": _num(0.25, 6, "磅"), "first_page_number": B,
    },
    "command": {"organ_mark_top_from_type_area_mm": _mm(0, 120), "number_blank_lines": _int(0, 6, "行")},
    "jiyao": {"mark_top_from_type_area_mm": _mm(0, 120), "attendee_label_font": R},
    "brief": {"mark_top_from_type_area_mm": _mm(0, 120), "mark_size": S, "issue_blank_lines": _int(0, 4, "行"), "issuer_blank_lines": _int(0, 4, "行"), "rule_below_mm": _mm(0, 20), "rule_width_pt": _num(0.25, 6, "磅")},
    "unit": {"organ_mark": _str(40), "letter_organ_mark": _str(40), "doc_number_prefix": _str(16), "printer": _str(60), "cc": SL, "brief_name": _str(30), "brief_issuer": _str(60)},
}
META_KEYS = ("name", "base", "description")

SECTION_LABELS = {"fonts": "字体", "page": "纸张", "margins": "页边距", "type_area": "版心", "grid": "行与字", "elements": "要素", "letter": "信函格式", "command": "命令（令）格式", "jiyao": "纪要格式", "brief": "简报报头", "unit": "单位信息"}
ELEMENT_LABELS = {
    "default": "默认文字", "copy_no": "份号", "secrecy": "密级和保密期限", "urgency": "紧急程度", "organ_mark": "发文机关标志", "doc_number": "发文字号",
    "signer": "签发人", "red_rule": "版头红色分隔线", "title": "标题", "title_note": "题注", "recipients": "主送机关", "body": "正文", "levels": "层次标题",
    "attachment_note": "附件说明", "signature_seal": "加盖印章的署名", "signature_noseal": "不加盖印章的署名", "note": "附注", "attachment": "附件标识",
    "imprint": "版记", "page_number": "页码",
}
FIELD_LABELS = {
    "font": "字体", "size": "字号", "color": "颜色", "top_from_type_area_mm": "上边缘至版心上边缘", "max_size": "字号（上限，排不下一行时缩小）",
    "blank_lines_after_mark": "与发文机关标志间空行", "label_font": "标签字体", "name_font": "姓名字体", "below_doc_number_mm": "距发文字号",
    "width_pt": "线宽", "blank_lines_before": "前空行", "max_chars_per_line": "每行最多字数", "first_indent_chars": "首行缩进",
    "left_indent_chars": "左空", "date_right_indent_chars": "成文日期右空", "organ_right_indent_chars": "署名右空", "date_shift_chars": "成文日期右移",
    "outer_rule_mm": "首末条分隔线粗", "inner_rule_mm": "中间分隔线粗", "indent_chars": "左右各空", "dash_offset_mm": "一字线距版心下边缘",
    "style": "样式", "width_mm": "宽", "height_mm": "高", "top_mm": "天头（上白边）", "left_mm": "订口（左白边）", "bottom_mm": "下白边", "right_mm": "切口（右白边）",
    "lines_per_page": "每面行数", "chars_per_line": "每行字数", "line_pt": "正文行距", "char_spacing_twips": "正文字距",
    "organ_mark_top_from_page_mm": "发文机关标志上边缘至上页边", "rule_below_mark_mm": "第一条双线距标志", "bottom_rule_from_page_mm": "第二条双线距下页边",
    "rule_length_mm": "双线长度", "element_gap_ratio": "要素距双线（3 号字高的倍数）", "title_blank_lines": "标题与上一要素间空行",
    "rule_thick_pt": "粗线宽", "rule_thin_pt": "细线宽", "rule_gap_pt": "双线间隔", "first_page_number": "首页显示页码",
    "organ_mark_top_from_type_area_mm": "发文机关标志上边缘至版心上边缘", "number_blank_lines": "令号上下空行", "mark_top_from_type_area_mm": "标志上边缘至版心上边缘",
    "attendee_label_font": "出席名单标签字体", "mark_size": "简报名称字号", "issue_blank_lines": "期号前空行", "issuer_blank_lines": "编印单位前空行",
    "rule_below_mm": "分隔线距编印单位行", "rule_width_pt": "分隔线宽", "name": "字库名", "alternates": "备选字库名",
}
UNIT_LABELS = {"organ_mark": "发文机关标志", "letter_organ_mark": "信函格式机关名称", "doc_number_prefix": "发文字号代字", "printer": "印发机关", "cc": "默认抄送机关", "brief_name": "简报名称", "brief_issuer": "简报编印单位"}

# 逐项的条款与强度（优先于所属要素的整体标注）。依据 GB/T 9704—2012 原文；“实务”“推算”不是国标数值
LEAF_ANNOT: dict[str, tuple[str, str]] = {
    "elements.organ_mark.top_from_type_area_mm": ("7.2.4", "规定"),
    "elements.organ_mark.font": ("7.2.4", "推荐"),
    "elements.organ_mark.color": ("7.2.4", "推荐"),
    "elements.organ_mark.max_size": ("7.2.4", "实务"),
    "elements.red_rule.width_pt": ("7.2.7", "实务"),
    "elements.title.max_chars_per_line": ("7.3.1", "实务"),
    "elements.body.first_indent_chars": ("7.3.3", "规定"),
    "elements.page_number.style": ("7.5", "一般"),
    "grid.line_pt": ("5.2.3", "实务"),
    "grid.char_spacing_twips": ("5.2.3", "实务"),
    "margins.bottom_mm": ("5.2.1", "推算"),
    "margins.right_mm": ("5.2.1", "推算"),
    "letter.rule_thick_pt": ("10.1", "实务"),
    "letter.rule_thin_pt": ("10.1", "实务"),
    "letter.rule_gap_pt": ("10.1", "实务"),
    "letter.title_blank_lines": ("10.1", "推算"),
}
SECTION_ANNOT = {"command": ("10.2", "规定"), "brief": ("", "实务"), "fonts": ("5.2.2", "实务"), "jiyao": ("10.3", "可以")}
ELEMENT_ANNOT = {"levels": ("7.3.3", "一般"), "title_note": ("", "实务")}
TOLERANCE = {"margins.top_mm": "top_tol_mm", "margins.left_mm": "left_tol_mm"}


# ---------------------------------------------------------------- 改动与偏离
@dataclass
class Change:
    path: str
    label: str
    base: Any
    value: Any
    clause: str = ""
    strength: str = ""
    derived: bool = False
    within_tolerance: bool = False

    @property
    def deviates(self) -> bool:
        """偏离国标：改动了“规定”“一般”“推荐”“可以”类参数，且不在国标允许误差内。"""
        return self.strength in NATIONAL and not self.within_tolerance

    @property
    def kind(self) -> str:
        if self.deviates:
            return "偏离国标"
        if self.within_tolerance:
            return "国标允许误差内"
        return "实务调整（国标未规定该数值）" if self.strength in ("实务", "推算") else "调整"

    def basis(self) -> str:
        return f"GB/T 9704—2012 {self.clause}，{self.strength}" if self.clause else self.strength

    def describe(self) -> str:
        tail = "，随其他改动推算" if self.derived else ""
        return f"{self.label}：{fmt_value(self.base, self.path)} → {fmt_value(self.value, self.path)}（{self.basis()}{tail}）"

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "label": self.label, "base": self.base, "value": self.value, "clause": self.clause, "strength": self.strength, "deviates": self.deviates, "kind": self.kind, "derived": self.derived, "text": self.describe()}


def _unit_of(path: str) -> str:
    spec = _spec(path.split("."))
    return spec[3] if spec and spec[0] in ("num", "int") and len(spec) > 3 else ""


def fmt_value(v: Any, path: str = "") -> str:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (list, tuple)):
        return "、".join(map(str, v)) or "（空）"
    if isinstance(v, str) and v in ROLE_LABELS and (path.endswith("font") or ".levels." in path):
        return f"{ROLE_LABELS[v]}"
    if path.endswith("color") and isinstance(v, str):
        return f"#{v}"
    if v is None or v == "":
        return "（空）"
    if isinstance(v, float):
        v = round(v, 2)
        v = int(v) if v == int(v) else v
    unit = _unit_of(path)
    return f"{v}{unit}" if unit else str(v)


def _spec(parts: list[str]) -> Any:
    node: Any = SCHEMA
    for p in parts:
        if not isinstance(node, dict):
            return None
        key: Any = int(p) if isinstance(p, str) and p.isdigit() and 1 in node else p
        if key not in node:
            return None
        node = node[key]
    return node


def _label(path: str) -> str:
    parts = path.split(".")
    sec = parts[0]
    if sec == "fonts" and len(parts) == 3:
        return f"{ROLE_LABELS.get(parts[1], parts[1])}{FIELD_LABELS.get(parts[2], parts[2])}"
    if sec == "elements" and len(parts) >= 3:
        el = ELEMENT_LABELS.get(parts[1], parts[1])
        if parts[1] == "levels":
            return f"{'一二三四'[int(parts[2]) - 1] if parts[2].isdigit() and 1 <= int(parts[2]) <= 4 else parts[2]}级标题字体"
        return f"{el}{FIELD_LABELS.get(parts[2], parts[2])}"
    if sec == "unit" and len(parts) == 2:
        return UNIT_LABELS.get(parts[1], parts[1])
    head = SECTION_LABELS.get(sec, sec)
    if sec in ("page", "type_area") and len(parts) == 2:
        return f"{head}{FIELD_LABELS.get(parts[1], parts[1])}"
    return f"{head}：{FIELD_LABELS.get(parts[-1], parts[-1])}" if len(parts) > 1 else head


def _annot(path: str, base: dict[str, Any]) -> tuple[str, str]:
    if path in LEAF_ANNOT:
        return LEAF_ANNOT[path]
    parts = path.split(".")
    sec = parts[0]
    if sec in SECTION_ANNOT:
        return SECTION_ANNOT[sec]
    if sec == "elements" and len(parts) >= 2:
        if parts[1] in ELEMENT_ANNOT:
            return ELEMENT_ANNOT[parts[1]]
        el = base.get("elements", {}).get(parts[1], {})
        return str(el.get("clause", "")), str(el.get("strength", "实务" if not el.get("clause") else ""))
    node = base.get(sec, {})
    if isinstance(node, dict):
        return str(node.get("clause", "")), str(node.get("strength", ""))
    return "", ""


def _leaves(spec: dict[str, Any], prefix: str = "") -> list[str]:
    out = []
    for k, v in spec.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out += _leaves(v, p)
        else:
            out.append(p)
    return out


def _get(d: dict[str, Any], path: str) -> Any:
    node: Any = d
    for p in path.split("."):
        if not isinstance(node, dict):
            return None
        if p.isdigit() and int(p) in node:
            node = node[int(p)]
        elif p in node:
            node = node[p]
        else:
            return None
    return node


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, p))
        else:
            out[p] = v
    return out


def changes_between(base: dict[str, Any], eff: dict[str, Any], explicit: set[str]) -> list[Change]:
    out = []
    for path in _leaves({k: v for k, v in SCHEMA.items() if k != "unit"}):
        a, b = _get(base, path), _get(eff, path)
        if b is None or a == b or (isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and abs(float(a) - float(b)) < 1e-9):
            continue
        clause, strength = _annot(path, base)
        within = False
        if path in TOLERANCE and isinstance(a, (int, float)) and isinstance(b, (int, float)):
            tol = base.get("margins", {}).get(TOLERANCE[path], 0)
            within = abs(float(b) - float(a)) <= float(tol)
        out.append(Change(path=path, label=_label(path), base=a, value=b, clause=clause, strength=strength, derived=path not in explicit, within_tolerance=within))
    return out


# ---------------------------------------------------------------- 校验与生效参数
def _check_value(path: str, spec: tuple, v: Any, sizes: dict[str, Any], errors: list[str]) -> Any:
    kind = spec[0]
    label = _label(path)
    if kind == "role":
        if isinstance(v, str) and v in ROLE_ALIASES:
            v = ROLE_ALIASES[v]
        if v not in ROLES:
            errors.append(f"{path}（{label}）应为字体类别之一：{'、'.join(f'{r}（{ROLE_LABELS[r]}）' for r in ROLES)}；实际为“{v}”")
        return v
    if kind == "size":
        if not isinstance(v, str) or v not in sizes:
            errors.append(f"{path}（{label}）应为字号名之一：{'、'.join(sizes)}；实际为“{v}”")
        return v
    if kind == "color":
        s = str(v).strip().lstrip("#").upper() if isinstance(v, (str, int)) else ""
        if not re.fullmatch(r"[0-9A-F]{6}", s):
            errors.append(f"{path}（{label}）应为 6 位十六进制颜色，如 FF0000；实际为“{v}”")
        return s
    if kind == "bool":
        if isinstance(v, str) and v.strip().lower() in ("true", "false", "是", "否", "yes", "no"):
            v = v.strip().lower() in ("true", "是", "yes")
        if not isinstance(v, bool):
            errors.append(f"{path}（{label}）应为 true 或 false；实际为“{v}”")
        return v
    if kind in ("num", "int"):
        lo, hi = spec[1], spec[2]
        unit = spec[3] if len(spec) > 3 else ""
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            try:
                v = float(str(v).strip().removesuffix("mm"))
            except ValueError:
                errors.append(f"{path}（{label}）应为数值；实际为“{v}”")
                return v
        if kind == "int":
            if float(v) != int(float(v)):
                errors.append(f"{path}（{label}）应为整数；实际为 {v}")
                return v
            v = int(float(v))
        elif float(v) == int(float(v)):
            v = int(float(v)) if isinstance(v, int) else float(v)
        if not lo <= float(v) <= hi:
            errors.append(f"{path}（{label}）应在 {lo}～{hi}{unit} 之间；实际为 {v}{unit}")
        return v
    if kind == "str":
        if not isinstance(v, str) or not v.strip():
            errors.append(f"{path}（{label}）应为非空文字")
            return v
        v = v.strip()
        if len(v) > spec[1] or re.search(r"[\x00-\x1f\x7f]", v):
            errors.append(f"{path}（{label}）过长（不超过 {spec[1]} 字）或含控制字符")
        return v
    if kind == "strlist":
        if isinstance(v, str):
            v = [x.strip() for x in re.split(r"[,，、;；]", v) if x.strip()]
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() and len(x) <= 64 and not re.search(r"[\x00-\x1f\x7f]", x) for x in v):
            errors.append(f"{path}（{label}）应为文字列表（如 [仿宋, FangSong]）")
            return v
        return [x.strip() for x in v]
    if kind == "enum":
        if v not in spec[1]:
            errors.append(f"{path}（{label}）应为 {'、'.join(spec[1])} 之一；实际为“{v}”")
        return v
    return v


def _validate_section(data: Any, spec: dict[str, Any], prefix: str, sizes: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        errors.append(f"{prefix} 应为键值表（如 {prefix}: {{…}}）")
        return {}
    out: dict[Any, Any] = {}
    for k, v in data.items():
        key: Any = k
        if 1 in spec and isinstance(k, str) and k.isdigit():
            key = int(k)
        path = f"{prefix}.{key}"
        if key in ("clause", "strength", "note", "category", "tol"):
            errors.append(f"{path}：条款、强度、说明与字体类别是标准注记，模板中不可修改")
            continue
        if key not in spec:
            names = [str(x) for x in spec]
            hint = difflib.get_close_matches(str(key), names, n=1)
            errors.append(f"未知设置项：{path}" + (f"（是否要写 {prefix}.{hint[0]}？）" if hint else f"（可用：{'、'.join(names)}）"))
            continue
        sub = spec[key]
        if isinstance(sub, dict):
            out[key] = _validate_section(v, sub, path, sizes, errors)
        else:
            out[key] = _check_value(path, sub, v, sizes, errors)
    return out


def validate_overrides(data: dict[str, Any], base: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """校验模板中的改动与单位信息：返回 (版式改动, 单位信息, 错误清单)。"""
    errors: list[str] = []
    sizes = base.get("sizes_pt", {})
    overrides: dict[str, Any] = {}
    unit: dict[str, Any] = {}
    for k, v in data.items():
        if k in META_KEYS:
            continue
        if k not in SCHEMA:
            hint = difflib.get_close_matches(str(k), list(SCHEMA) + list(META_KEYS), n=1)
            errors.append(f"未知设置项：{k}" + (f"（是否要写 {hint[0]}？）" if hint else f"（可用：{'、'.join([*META_KEYS, *SCHEMA])}）"))
            continue
        clean = _validate_section(v, SCHEMA[k], k, sizes, errors)
        if k == "unit":
            unit = clean
        elif clean:
            overrides[k] = clean
    return overrides, unit, errors


def _merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def effective_data(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """合并改动并推算相关数值（未显式设置时）：下白边、切口随天头、订口、版心推算；行距随每面行数、字距随每行字数与
    正文字号推算（均向下取整到缇，不超出版心）；标题每行最多字数随标题字号推算。"""
    data = _merge(copy.deepcopy(base), copy.deepcopy(overrides))
    om, ot, og = overrides.get("margins", {}), overrides.get("type_area", {}), overrides.get("grid", {})
    oe = overrides.get("elements", {})
    pg, m, ta, g = data["page"], data["margins"], data["type_area"], data["grid"]
    if (om or ot or overrides.get("page")) and "bottom_mm" not in om:
        m["bottom_mm"] = round(pg["height_mm"] - m["top_mm"] - ta["height_mm"], 2)
    if (om or ot or overrides.get("page")) and "right_mm" not in om:
        m["right_mm"] = round(pg["width_mm"] - m["left_mm"] - ta["width_mm"], 2)
    if "top_mm" in om or "bottom_mm" in om:
        comp = base.get("compensated_margins", {})
        shift_t = base["margins"]["top_mm"] - comp.get("top_mm", base["margins"]["top_mm"])
        shift_b = base["margins"]["bottom_mm"] - comp.get("bottom_mm", base["margins"]["bottom_mm"])
        data["compensated_margins"] = {"top_mm": round(m["top_mm"] - shift_t, 2), "bottom_mm": round(m["bottom_mm"] - shift_b, 2)}
    if "line_pt" not in og and ("lines_per_page" in og or "height_mm" in ot):
        g["line_pt"] = math.floor(ta["height_mm"] / 25.4 * 1440 / g["lines_per_page"]) / 20
    body_pt = float(data["sizes_pt"][data["elements"]["body"]["size"]])
    body_changed = "size" in oe.get("body", {})
    if "char_spacing_twips" not in og and ("chars_per_line" in og or "width_mm" in ot or body_changed):
        width_pt = ta["width_mm"] / MM_PER_PT
        g["char_spacing_twips"] = math.floor((width_pt / g["chars_per_line"] - body_pt) * 20)
    if "size" in oe.get("title", {}) and "max_chars_per_line" not in oe.get("title", {}):
        t_pt = float(data["sizes_pt"][data["elements"]["title"]["size"]])
        data["elements"]["title"]["max_chars_per_line"] = int(math.floor(ta["width_mm"] / MM_PER_PT / t_pt))
    return data


def cross_check(data: dict[str, Any]) -> list[str]:
    """生效参数之间的一致性：版心 + 页边距 = 纸张；行数 × 行距、字数 × 字宽不超出版心。"""
    errors = []
    pg, m, ta, g = data["page"], data["margins"], data["type_area"], data["grid"]
    if abs(m["top_mm"] + ta["height_mm"] + m["bottom_mm"] - pg["height_mm"]) > 0.5:
        errors.append(f"天头 {m['top_mm']}mm + 版心高 {ta['height_mm']}mm + 下白边 {m['bottom_mm']}mm ≠ 纸张高 {pg['height_mm']}mm：请同时调整 margins.bottom_mm（应为 {round(pg['height_mm'] - m['top_mm'] - ta['height_mm'], 2)}mm）或 type_area.height_mm")
    if abs(m["left_mm"] + ta["width_mm"] + m["right_mm"] - pg["width_mm"]) > 0.5:
        errors.append(f"订口 {m['left_mm']}mm + 版心宽 {ta['width_mm']}mm + 切口 {m['right_mm']}mm ≠ 纸张宽 {pg['width_mm']}mm：请同时调整 margins.right_mm（应为 {round(pg['width_mm'] - m['left_mm'] - ta['width_mm'], 2)}mm）或 type_area.width_mm")
    height_pt = ta["height_mm"] / MM_PER_PT
    if g["lines_per_page"] * g["line_pt"] > height_pt + 0.5:
        errors.append(f"每面 {g['lines_per_page']} 行 × 行距 {g['line_pt']} 磅 = {g['lines_per_page'] * g['line_pt']:.1f} 磅，超出版心高 {height_pt:.1f} 磅：请减小 grid.line_pt 或 grid.lines_per_page")
    body_pt = float(data["sizes_pt"][data["elements"]["body"]["size"]])
    width_pt = ta["width_mm"] / MM_PER_PT
    pitch = body_pt + g["char_spacing_twips"] / 20
    if g["chars_per_line"] * pitch > width_pt + 0.5:
        errors.append(f"每行 {g['chars_per_line']} 字 × 字宽 {pitch:.2f} 磅超出版心宽 {width_pt:.1f} 磅：请减小 grid.chars_per_line 或 grid.char_spacing_twips")
    t = data["elements"]["title"]
    t_pt = float(data["sizes_pt"][t["size"]])
    if t["max_chars_per_line"] * t_pt > width_pt + 1:
        errors.append(f"标题每行 {t['max_chars_per_line']} 字 × {t_pt:g} 磅超出版心宽 {width_pt:.1f} 磅：请减小 elements.title.max_chars_per_line")
    return errors


# ---------------------------------------------------------------- 模板
@dataclass
class Template:
    name: str
    base: str = "gbt9704-2012"
    description: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)
    unit: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    kind: str = "用户"  # 内置 / 工作区 / 用户

    @property
    def builtin(self) -> bool:
        return self.kind == "内置"

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, name: str | None = None, source: str = "", kind: str = "用户") -> "Template":
        if not isinstance(data, dict):
            raise TemplateError(["模板文件应为 YAML 键值表"], name or "")
        errors: list[str] = []
        tname = str(data.get("name") or name or "")
        if not NAME_RE.match(tname):
            errors.append(f"name：模板名“{tname}”应为 1～40 个汉字、字母、数字、下划线或连字符（不能含空格、点或斜杠）")
        if name and tname and tname != name:
            errors.append(f"name：文件名（{name}）与模板名（{tname}）不一致")
        base = str(data.get("base") or "gbt9704-2012")
        if base not in BASE_PROFILES:
            errors.append(f"base：基础配置档应为 {'、'.join(BASE_PROFILES)}；实际为“{base}”（模板不能以另一个模板为基础，可用 template new --from 复制）")
            raise TemplateError(errors, tname)
        desc = data.get("description") or ""
        if not isinstance(desc, str) or len(desc) > 200:
            errors.append("description：说明应为不超过 200 字的文字")
            desc = ""
        overrides, unit, errs = validate_overrides(data, _raw(base))
        errors += errs
        tpl = cls(name=tname, base=base, description=desc, overrides=overrides, unit=unit, source=source, kind=kind)
        if not errors:
            errors += cross_check(tpl.effective_data())
        if errors:
            raise TemplateError(errors, tname)
        return tpl

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "base": self.base, "description": self.description}
        d.update(copy.deepcopy(self.overrides))
        if self.unit:
            d["unit"] = copy.deepcopy(self.unit)
        return d

    def base_data(self) -> dict[str, Any]:
        return copy.deepcopy(_raw(self.base))

    def effective_data(self) -> dict[str, Any]:
        return effective_data(_raw(self.base), self.overrides)

    def changes(self) -> list[Change]:
        explicit = set(_flatten(self.overrides))
        return changes_between(_raw(self.base), self.effective_data(), explicit)

    def deviations(self) -> list[Change]:
        return [c for c in self.changes() if c.deviates]

    def profile(self, margin_mode: str = "standard") -> LayoutProfile:
        return LayoutProfile(self.effective_data(), margin_mode, template=self.name if self.name not in BASE_PROFILES else "", changes=self.changes(), unit=dict(self.unit))


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TemplateError([f"YAML 格式错误：{exc}"], path.stem) from exc
    return data or {}


class TemplateStore:
    """模板存放：数据目录（用户，可写）＞工作区 templates/（只读）＞内置（只读）。"""

    def __init__(self, data_dir: str | Path, workspace: str | Path | None = None):
        self.user_dir = Path(data_dir) / "templates"
        self.workspace_dir = Path(workspace) / "templates" if workspace else None

    def _dirs(self) -> list[tuple[str, Path]]:
        dirs = [("用户", self.user_dir)]
        if self.workspace_dir is not None and self.workspace_dir.resolve() != self.user_dir.resolve():
            dirs.append(("工作区", self.workspace_dir))
        dirs.append(("内置", BUILTIN_DIR))
        return dirs

    @staticmethod
    def check_name(name: str) -> str:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"模板名“{name}”无效：应为 1～40 个汉字、字母、数字、下划线或连字符（不能含空格、点或斜杠）")
        return name

    def names(self) -> list[str]:
        out = list(BASE_PROFILES)
        for _, d in self._dirs():
            if d.is_dir():
                out += [p.stem for p in sorted(d.glob("*.yaml")) if NAME_RE.match(p.stem)]
        return list(dict.fromkeys(out))

    def path(self, name: str) -> tuple[str, Path] | None:
        if name in BASE_PROFILES:
            return ("内置", PROFILE_DIR / f"{name}.yaml")
        for kind, d in self._dirs():
            p = d / f"{name}.yaml"
            if p.is_file():
                return kind, p
        return None

    def get(self, name: str) -> Template:
        self.check_name(name)
        if name in BASE_PROFILES:
            return Template(name=name, base=name, description=str(_raw(name).get("title", "")) + "（基础配置档，默认参数）", source=str(PROFILE_DIR / f"{name}.yaml"), kind="内置")
        found = self.path(name)
        if found is None:
            raise KeyError(f"未找到模板：{name}（可用：{'、'.join(self.names())}；用 gongwen template new {name} 创建）")
        kind, p = found
        return Template.from_dict(_read_yaml(p), name=name, source=str(p), kind=kind)

    def list(self) -> list[tuple[str, Template | None, str]]:
        """全部模板：(名称, 模板或 None, 错误说明)。损坏的模板也列出，便于修正。"""
        out = []
        for n in self.names():
            try:
                out.append((n, self.get(n), ""))
            except (TemplateError, KeyError, ValueError) as exc:
                out.append((n, None, str(exc)))
        return out

    def save(self, tpl: Template) -> Path:
        self.check_name(tpl.name)
        if tpl.name in BASE_PROFILES or (BUILTIN_DIR / f"{tpl.name}.yaml").is_file():
            raise ValueError(f"“{tpl.name}”是内置模板，不能覆盖；请另起名称：gongwen template new <名称> --from {tpl.name}")
        # 重新校验（含一致性），不合规时不写入
        Template.from_dict(tpl.to_dict(), name=tpl.name)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        p = self.user_dir / f"{tpl.name}.yaml"
        header = "# 公文模板（gongwen template）。只写与基础配置档不同的设置；偏离 GB/T 9704—2012 的项会在报告中列出。\n"
        p.write_text(header + yaml.safe_dump(tpl.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8")
        tpl.source, tpl.kind = str(p), "用户"
        return p

    def new(self, name: str, from_: str | None = None, description: str = "") -> Template:
        self.check_name(name)
        if name in self.names():
            raise ValueError(f"模板“{name}”已存在（可用 gongwen template set 修改，或换一个名称）")
        src = self.get(from_) if from_ else None
        tpl = Template(name=name, base=src.base if src else "gbt9704-2012", description=description or (f"复制自“{from_}”" if from_ else ""), overrides=copy.deepcopy(src.overrides) if src else {}, unit=copy.deepcopy(src.unit) if src else {})
        self.save(tpl)
        return tpl

    def update(self, name: str, assignments: list[str] | None = None, unsets: list[str] | None = None) -> Template:
        """按 key.path=value 修改用户模板（值按 YAML 解析，如 [仿宋, FangSong]、true、16）；unsets 为要恢复为基础值的设置项。"""
        tpl = self.get(name)
        if tpl.kind != "用户":
            raise ValueError(f"“{name}”是{tpl.kind}模板，不能直接修改；请先复制：gongwen template new <名称> --from {name}")
        data = tpl.to_dict()
        for item in assignments or []:
            key, sep, raw = item.partition("=")
            key = key.strip()
            if not sep or not key:
                raise ValueError(f"设置应为 key.path=value：{item}")
            try:
                # “#C00000”在 YAML 中是注释：以 # 开头的值按原文处理
                value = yaml.safe_load(raw) if raw.strip() and not raw.strip().startswith("#") else raw
            except yaml.YAMLError:
                value = raw
            if isinstance(value, str):
                value = value.strip()
            parts = key.split(".")
            if parts[0] in META_KEYS:
                if len(parts) != 1:
                    raise ValueError(f"未知设置项：{key}")
                if parts[0] == "name":
                    raise ValueError("不能用 set 修改模板名：请用 template new <新名> --from <旧名> 复制后删除旧模板")
                data[parts[0]] = value
                continue
            cur = data
            for p in parts[:-1]:
                nxt = cur.get(p)
                if nxt is None and p.isdigit():
                    nxt = cur.get(int(p))
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[p] = nxt
                cur = nxt
            cur[parts[-1]] = value
        for key in unsets or []:
            parts = key.strip().split(".")
            cur = data
            for p in parts[:-1]:
                cur = cur.get(p) if isinstance(cur.get(p), dict) else cur.get(int(p)) if p.isdigit() and isinstance(cur.get(int(p)), dict) else {}
            last = parts[-1]
            if last in cur:
                del cur[last]
            elif last.isdigit() and int(last) in cur:
                del cur[int(last)]
            else:
                raise ValueError(f"模板中没有设置项：{key}")
        new = Template.from_dict(_prune(data), name=name)
        self.save(new)
        return new

    def delete(self, name: str) -> Path:
        found = self.path(self.check_name(name))
        if found is None:
            raise KeyError(f"未找到模板：{name}")
        kind, p = found
        if kind != "用户":
            raise ValueError(f"“{name}”是{kind}模板，不能删除" + ("（请在工作区 templates/ 目录中自行处理）" if kind == "工作区" else ""))
        p.unlink()
        return p


def _prune(d: dict[str, Any]) -> dict[str, Any]:
    """去掉空的设置节（如恢复全部设置后剩下的 elements: {title: {}}）。"""
    out: dict[Any, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = _prune(v)
            if not v:
                continue
        out[k] = v
    return out


def resolve_profile(template: str | None, *, data_dir: str | Path, workspace: str | Path | None = None, profile_id: str = "gbt9704-2012", margin_mode: str = "standard") -> LayoutProfile:
    """排版用的版式参数：未指定模板时为基础配置档（与以往完全相同），指定时为模板的生效参数。"""
    if not template or template == profile_id:
        return LayoutProfile.load(profile_id, margin_mode)
    tpl = TemplateStore(data_dir, workspace).get(template)
    return tpl.profile(margin_mode)


# ---------------------------------------------------------------- 单位信息与核验项
def apply_unit(ir: DocumentIR, unit: dict[str, Any]) -> tuple[DocumentIR, list[str]]:
    """按模板的单位信息补全文稿中空缺（或仍为占位）的版头、版记要素；不覆盖文稿已有内容。返回 (新文稿, 已填入的要素)。

    发文字号只填代字，年份与序号仍留待办理流程确定（不用生成时间或猜测填充）。"""
    if not unit:
        return ir, []
    ir = ir.model_copy(deep=True)
    filled: list[str] = []
    fmt, h = ir.format_type, ir.header
    if fmt == "general" and not h.organ_mark and unit.get("organ_mark"):
        h.organ_mark = unit["organ_mark"]
        filled.append("发文机关标志")
    if fmt == "letter" and not h.organ_mark and unit.get("letter_organ_mark"):
        h.organ_mark = unit["letter_organ_mark"]
        filled.append("信函格式机关名称")
    if fmt == "brief" and unit.get("brief_name") and h.organ_mark in ("", "工作简报"):
        h.organ_mark = unit["brief_name"]
        filled.append("简报名称")
    if fmt in ("general", "letter") and unit.get("doc_number_prefix") and (not h.doc_number or h.doc_number.startswith("【待")):
        h.doc_number = f"{unit['doc_number_prefix']}〔【待补：年份】〕【待编号】号"
        filled.append("发文字号代字")
    if fmt not in ("letter", "plain", "brief") and unit.get("printer") and (not ir.imprint.printer or ir.imprint.printer.startswith("【待")):
        ir.imprint.printer = unit["printer"]
        filled.append("印发机关")
    if fmt not in ("plain", "brief") and unit.get("cc") and not ir.imprint.cc:
        ir.imprint.cc = list(unit["cc"])
        filled.append("抄送机关（模板默认）")
    if fmt == "brief" and unit.get("brief_issuer") and not ir.meta.get("brief_issuer"):
        ir.meta["brief_issuer"] = unit["brief_issuer"]
        filled.append("简报编印单位")
    return ir, filled


def template_checks(profile: LayoutProfile) -> list[LayoutCheck]:
    """排版报告中的模板核验项：按哪个模板排版、偏离国标几项（逐项列出）。未用模板时为空。"""
    if not profile.template:
        return []
    devs = [c for c in profile.changes if c.deviates]
    others = [c for c in profile.changes if not c.deviates]
    by_strength = "、".join(f"{s} {n} 项" for s in NATIONAL if (n := sum(1 for c in devs if c.strength == s)))
    out = [
        LayoutCheck(
            rule_id="TEMPLATE",
            item="版式模板",
            expected="GB/T 9704—2012 默认参数（未用模板时）",
            actual=f"按模板“{profile.template}”排版（基于 {profile.id}）：偏离国标 {len(devs)} 项" + (f"（{by_strength}）" if by_strength else "") + f"，其他调整 {len(others)} 项（字库名等国标未规定的数值、推算值或允许误差内）",
            status="warn" if devs else "pass",
            clause="GB/T 9704—2012",
            level="单位制度",
            conditional=True,
            note="各项核验以模板生效参数为准；偏离项逐项见“模板偏离”" + ("；其他调整：" + "；".join(c.describe() for c in others) if others else ""),
        )
    ]
    for c in devs:
        out.append(
            LayoutCheck(
                rule_id="TPL-DEV",
                item=f"模板偏离：{c.label}",
                expected=f"{fmt_value(c.base, c.path)}（{c.basis()}）",
                actual=f"{fmt_value(c.value, c.path)}（模板“{profile.template}”{'，随其他改动推算' if c.derived else ''}）",
                status="warn",
                clause=f"GB/T 9704—2012 {c.clause}" if c.clause else "",
                level="单位制度",
                conditional=c.strength != "规定",
                note="不符合国标“规定”类要求，按单位模板（公文处理细则）执行，须有制度依据" if c.strength == "规定" else "国标为条件性要求（一般/推荐/可以），按单位模板调整",
            )
        )
    return out


def sample_ir(fmt: str = "general", unit: dict[str, Any] | None = None) -> DocumentIR:
    """模板预览用的示例公文（合成内容）：版头、标题、主送、多级标题、附件说明、署名、版记齐全。"""
    from ..schemas.ir import AttachmentNote, Block, Header, Imprint, Sentence, Signature

    unit = unit or {}
    organ = (unit.get("organ_mark") or "示例市卫生健康委员会文件").strip()
    signer = organ[:-2] if organ.endswith("文件") else organ
    para = "为进一步规范公文格式，提高办文质量，现就有关事项通知如下。各单位要高度重视，按照本通知要求认真组织实施，确保各项工作落到实处。"
    blocks = [
        Block(bid="b1", kind="paragraph", sentences=[Sentence(sid="s1", text=para)]),
        Block(bid="b2", kind="heading", level=1, label="一、", heading="总体要求"),
        Block(bid="b3", kind="paragraph", sentences=[Sentence(sid="s3", text="以规范、高效为原则，统一公文格式，做到要素齐全、版式规范、标注准确。")]),
        Block(bid="b4", kind="heading", level=2, label="（一）", heading="明确责任分工"),
        Block(bid="b5", kind="paragraph", sentences=[Sentence(sid="s5", text="办公室负责公文格式审核，各业务处室负责本处室文稿的拟制与校对。")]),
        Block(bid="b6", kind="heading", level=3, label="1.", heading="加强审核把关。", inline_heading=True, sentences=[Sentence(sid="s6", text="文稿送审前须对照格式要求逐项核对。")]),
        Block(bid="b7", kind="heading", level=4, label="（1）", heading="核对版头要素。", inline_heading=True, sentences=[Sentence(sid="s7", text="核对发文机关标志、发文字号与签发人。")]),
    ]
    return DocumentIR(
        doc_id="SAMPLE",
        matter_id="sample",
        format_type=fmt,
        genre="通知",
        header=Header(organ_mark=organ, doc_number=f"{unit.get('doc_number_prefix') or '示卫发'}〔2026〕1号"),
        title="关于进一步规范公文格式的通知",
        recipients=["各区卫生健康局", "委机关各处室"],
        blocks=blocks,
        attachment_notes=[AttachmentNote(seq=1, name="公文格式要素一览表")],
        signature=Signature(organs=[signer], date="2026年10月8日"),
        imprint=Imprint(cc=list(unit.get("cc") or ["示例市人民政府办公室"]), printer=unit.get("printer") or f"{signer}办公室", print_date="2026年10月8日"),
        meta={"source": "sample"},
    )


__all__ = [
    "BUILTIN_DIR",
    "Change",
    "Template",
    "TemplateError",
    "TemplateStore",
    "apply_unit",
    "changes_between",
    "cross_check",
    "effective_data",
    "fmt_value",
    "resolve_profile",
    "sample_ir",
    "template_checks",
    "validate_overrides",
]
