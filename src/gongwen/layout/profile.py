"""版式配置加载与换算工具。"""

from __future__ import annotations

import functools
import math
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
    # 兜底：均衡分为 ⌈字数/每行上限⌉ 行，优先在“关于”“印发”“的”之后、“《”“关于”之前回行
    preferred = set(cands)
    for w in ("关于", "印发", "的"):
        preferred |= {m.end() for m in re.finditer(w, title)}
    for w in ("关于", "《"):
        preferred |= {m.start() for m in re.finditer(w, title)}
    return _balanced_split(title, max_chars, preferred)


_NO_LINE_START = "，。、；：！？》”）〕】"
_NO_LINE_END = "《“（〔【"
# 标题常用词（不做分词，只避免把这些词拆到两行）
_TITLE_WORDS = (
    "进一步 委员会 加强 规范 做好 开展 推进 落实 完善 建立 健全 实施 印发 转发 批转 关于 基层 医疗 卫生 健康 机构 示范 "
    "建设 管理 运行 维护 工作 服务 体系 能力 质量 安全 监督 检查 考核 评估 培训 人员 设备 经费 资金 项目 申请 安排 计划 "
    "方案 办法 意见 规定 细则 暂行 试行 通知 报告 请示 批复 有关 事项 问题 若干 情况 年度 单位 部门 政府 改革 发展 制度 "
    "保障 组织 领导 专项 整治 行动 应急 教育 科研 学校 医院 社会 经济 信息 平台 数据"
).split()


def _cut_penalty(title: str, c: int, preferred: set[int]) -> float:
    """在 c 处回行的代价：优选断点为 0，其余位置可能拆开词语；拆开书名号、数字或使标点落在行首行尾的代价很高（不可避免时才用）。"""
    p = 0.0 if c in preferred else 4.0
    if any(title.startswith(w, s) for w in _TITLE_WORDS for s in range(max(0, c - len(w) + 1), c)):
        p += 6.0
    if title[:c].count("《") > title[:c].count("》"):
        p += 200.0
    if title[c] in _NO_LINE_START or title[c - 1] in _NO_LINE_END:
        p += 50.0
    if title[c - 1].isascii() and title[c].isascii() and title[c - 1].isalnum() and title[c].isalnum():
        p += 50.0
    return p


def _balanced_split(title: str, max_chars: float, preferred: set[int]) -> list[str]:
    """分成 k=⌈字数/每行上限⌉ 行：每行不超过上限，长短尽量均衡，断点代价最小（动态规划）；
    多排一行就能不拆书名号等时可以多排一行（每多一行计代价 10）。"""
    n = len(title)
    pre = [0.0]
    for ch in title:
        pre.append(pre[-1] + text_width_chars(ch))
    pen = [0.0] + [_cut_penalty(title, b, preferred) for b in range(1, n)]
    inf = float("inf")
    k0 = max(2, math.ceil(pre[-1] / max_chars))
    best: tuple[float, list[str]] | None = None
    for k in range(k0, k0 + 3):
        target = pre[-1] / k
        cost = [[inf] * (n + 1) for _ in range(k + 1)]
        back = [[0] * (n + 1) for _ in range(k + 1)]
        cost[0][0] = 0.0
        for j in range(1, k + 1):
            for c in range(j, n + 1):
                for b in range(c - 1, j - 2, -1):
                    w = pre[c] - pre[b]
                    if w > max_chars:  # 再往前只会更长
                        break
                    if cost[j - 1][b] == inf:
                        continue
                    v = cost[j - 1][b] + (w - target) ** 2 / k + (100.0 if w < 2 else 0.0) + pen[b]
                    if v < cost[j][c]:
                        cost[j][c], back[j][c] = v, b
        if cost[k][n] < inf and (best is None or cost[k][n] + 10.0 * (k - k0) < best[0]):
            cuts, c = [], n
            for j in range(k, 0, -1):
                cuts.append(c)
                c = back[j][c]
            cuts.reverse()
            best = (cost[k][n] + 10.0 * (k - k0), [title[a:b] for a, b in zip([0, *cuts[:-1]], cuts, strict=True)])
    if best:
        return best[1]
    # 不应到达：逐字累计，满一行即回行
    lines, cur = [], ""
    for ch in title:
        if cur and text_width_chars(cur + ch) > max_chars:
            lines.append(cur)
            cur = ""
        cur += ch
    return lines + [cur]
