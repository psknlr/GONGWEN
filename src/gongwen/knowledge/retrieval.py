"""检索工具：文号与标题精确检索 + BM25（中文字二元组）+ 可选语义检索。

不依赖分词词典：中文按字二元组切分，英文与数字按词切分，对公文这类术语密集的
短文本效果稳定、可复现。语义检索通过可插拔的 Embedder 接入（例如单位私有部署的
向量模型），默认关闭。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

_CJK = re.compile(r"[一-鿿]")
_TOKEN = re.compile(r"[一-鿿]+|[A-Za-z]+|\d+(?:\.\d+)?")
_STOP = set("的了和与及或在对于是为等其以将把被就也都而且并")

# 发文字号：机关代字〔年份〕顺序号号；兼容常见误写的括号
DOC_NUMBER_RE = re.compile(
    r"([一-鿿]{1,12}(?:发|函|字|办|令|通|电|复|厅字|办发|办函)?)\s*[〔\[［(（](\d{4})[〕\]］)）]\s*(\d{1,5})\s*号"
)
ORDER_NUMBER_RE = re.compile(r"(国务院令|[一-鿿]{2,10}令)\s*第\s*(\d{1,4})\s*号")
TITLE_RE = re.compile(r"《([^《》]{2,80})》")
STANDARD_RE = re.compile(r"GB/?T?\s*\d{3,6}\s*[—\-–]\s*\d{4}")


def tokenize(text: str) -> list[str]:
    toks: list[str] = []
    for m in _TOKEN.finditer(text or ""):
        seg = m.group(0)
        if _CJK.match(seg):
            chars = [c for c in seg if c not in _STOP]
            if len(chars) == 1:
                toks.append(chars[0])
            toks.extend(a + b for a, b in zip(chars, chars[1:]))
        else:
            toks.append(seg.lower())
    return toks


_PREFIX_WORDS = (
    "根据", "依据", "按照", "依照", "参照", "对照", "遵照", "贯彻", "落实", "执行", "印发", "转发",
    "关于", "通过", "经", "据", "见", "按", "和", "与", "及", "或", "的", "以", "号", "在", "对", "由", "将", "为", "即",
)


def _trim_code(code: str) -> str:
    """去掉机关代字前粘连的介词、连词（如“依据国办发”→“国办发”）。"""
    changed = True
    while changed:
        changed = False
        for w in _PREFIX_WORDS:
            if code.startswith(w) and len(code) > len(w) + 1:
                code = code[len(w):]
                changed = True
    return code


def normalize_doc_number(s: str) -> str:
    m = DOC_NUMBER_RE.search(s)
    if m:
        return f"{_trim_code(m.group(1))}〔{m.group(2)}〕{int(m.group(3))}号"
    m = ORDER_NUMBER_RE.search(s)
    if m:
        return f"{_trim_code(m.group(1))}第{int(m.group(2))}号"
    return s.strip()


def find_doc_numbers(text: str) -> list[str]:
    out = [normalize_doc_number(m.group(0)) for m in DOC_NUMBER_RE.finditer(text)]
    out += [normalize_doc_number(m.group(0)) for m in ORDER_NUMBER_RE.finditer(text)]
    return list(dict.fromkeys(out))


def find_titles(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in TITLE_RE.finditer(text)))


@dataclass
class Doc:
    doc_id: str
    text: str
    meta: dict = field(default_factory=dict)


class BM25:
    def __init__(self, k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs: list[Doc] = []
        self.tfs: list[Counter] = []
        self.df: Counter = Counter()
        self.avgdl = 0.0

    def add(self, docs: Iterable[Doc]) -> None:
        for d in docs:
            tf = Counter(tokenize(d.text))
            self.docs.append(d)
            self.tfs.append(tf)
            self.df.update(tf.keys())
        total = sum(sum(tf.values()) for tf in self.tfs)
        self.avgdl = total / len(self.tfs) if self.tfs else 0.0

    def idf(self, term: str) -> float:
        n = len(self.docs)
        df = self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, k: int = 10, filter_fn: Callable[[Doc], bool] | None = None) -> list[tuple[Doc, float]]:
        q = tokenize(query)
        if not q or not self.docs:
            return []
        qtf = Counter(q)
        scored = []
        for d, tf in zip(self.docs, self.tfs):
            if filter_fn and not filter_fn(d):
                continue
            dl = sum(tf.values()) or 1
            s = 0.0
            for term, qn in qtf.items():
                f = tf.get(term, 0)
                if not f:
                    continue
                s += self.idf(term) * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))) * (1 + math.log(qn))
            if s > 0:
                scored.append((d, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


def coverage(claim: str, evidence: str) -> float:
    """声明在证据中的词元覆盖率：用于“依据是否支持具体表述”的确定性初判。"""
    ct = set(tokenize(claim))
    if not ct:
        return 0.0
    et = set(tokenize(evidence))
    return len(ct & et) / len(ct)
