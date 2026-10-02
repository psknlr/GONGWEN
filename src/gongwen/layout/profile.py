"""版式配置加载与换算工具。"""

from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROFILE_DIR = Path(__file__).parent / "profiles"
MM_PER_PT = 25.4 / 72.0


@functools.lru_cache(maxsize=None)
def _raw(profile_id: str) -> dict[str, Any]:
    path = PROFILE_DIR / f"{profile_id}.yaml"
    if not path.is_file():
        raise KeyError(f"未找到版式配置：{profile_id}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class LayoutProfile:
    data: dict[str, Any]
    margin_mode: str = "standard"

    @classmethod
    def load(cls, profile_id: str = "gbt9704-2012", margin_mode: str = "standard", overrides: dict[str, Any] | None = None) -> "LayoutProfile":
        data = dict(_raw(profile_id))
        if overrides:
            data = _merge(data, overrides)
        return cls(data, margin_mode)

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def version(self) -> str:
        return str(self.data.get("version", ""))

    def size(self, name: str) -> float:
        return float(self.data["sizes_pt"][name])

    def font(self, key: str) -> str:
        return self.data["fonts"][key]["name"]

    def font_category(self, key: str) -> str:
        return self.data["fonts"][key]["category"]

    def el(self, name: str) -> dict[str, Any]:
        return self.data["elements"][name]

    @property
    def margins(self) -> dict[str, float]:
        m = self.data["margins"]
        top, bottom = m["top_mm"], m["bottom_mm"]
        if self.margin_mode == "compensated":
            top = self.data["compensated_margins"]["top_mm"]
            bottom = self.data["compensated_margins"]["bottom_mm"]
        return {"top": top, "bottom": bottom, "left": m["left_mm"], "right": m["right_mm"]}

    @property
    def line_pt(self) -> float:
        return float(self.data["grid"]["line_pt"])

    @property
    def body_pt(self) -> float:
        return self.size(self.el("body")["size"])

    @property
    def char_pitch_pt(self) -> float:
        """3号字加字距调整后的字宽（用于“空N字”换算）。"""
        return self.body_pt + self.data["grid"]["char_spacing_twips"] / 20.0

    @property
    def type_width_pt(self) -> float:
        return self.data["type_area"]["width_mm"] / MM_PER_PT

    @property
    def type_height_pt(self) -> float:
        return self.data["type_area"]["height_mm"] / MM_PER_PT


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def text_width_chars(text: str) -> float:
    """按“字”计宽：全角字符记 1，半角字符记 0.5。"""
    w = 0.0
    for ch in text:
        w += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5
    return w


_BREAK_AFTER = ("、", "》", "”", "）")


def split_title(title: str, max_chars: int = 20, issuer: str = "") -> list[str]:
    """标题回行：词意完整、排列对称；多行时使用梯形（上短下长）或菱形（中间最长），不用长方形。"""
    if text_width_chars(title) <= max_chars:
        return [title]
    cands: set[int] = set()
    if issuer and title.startswith(issuer):
        cands.add(len(issuer))
    for m in re.finditer("关于", title):
        cands.add(m.start())
    for i, ch in enumerate(title):
        if ch in _BREAK_AFTER:
            cands.add(i + 1)
    for m in re.finditer(r"的(?=[^的]{1,6}$)", title):
        cands.add(m.start())
    for m in re.finditer(r"(?<=[和与及])", title):
        cands.add(m.start())
    cands = {c for c in cands if 2 <= c <= len(title) - 2}

    def ok2(a: str, b: str) -> bool:
        return text_width_chars(a) <= max_chars and text_width_chars(b) <= max_chars and text_width_chars(a) < text_width_chars(b)

    best = None
    for c in sorted(cands):
        a, b = title[:c], title[c:]
        if ok2(a, b):
            score = abs(text_width_chars(b) - text_width_chars(a))
            if best is None or score < best[0]:
                best = (score, [a, b])
    if best:
        return best[1]
    # 三行：菱形或梯形
    ordered = sorted(cands)
    best3 = None
    for i, c1 in enumerate(ordered):
        for c2 in ordered[i + 1 :]:
            parts = [title[:c1], title[c1:c2], title[c2:]]
            ws = [text_width_chars(p) for p in parts]
            if max(ws) > max_chars or min(ws) < 2:
                continue
            shape_ok = (ws[0] < ws[1] and ws[1] >= ws[2]) or (ws[0] <= ws[1] <= ws[2] and ws[0] < ws[2])
            if not shape_ok:
                continue
            score = max(ws) - min(ws)
            if best3 is None or score < best3[0]:
                best3 = (score, parts)
    if best3:
        return best3[1]
    # 兜底：按字数切分为上短下长
    n = len(title)
    cut = max(2, n // 2 - 1)
    return [title[:cut], title[cut:]]
