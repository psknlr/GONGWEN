"""把已有文稿（纯文本、Markdown、DOCX）导入为 DocumentIR，供规则检查与审阅使用。

导入只做结构识别（版头、标题、主送、层次、附件说明、署名日期、附注、版记），不改写任何内容。
外部文稿没有证据关联（句子的 refs 为空），因此依赖事实账本与证据链的规则在“外部文稿模式”下
不作判定，而是在检查结果中提示由人工核验（见 check_external）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .knowledge import kb
from .knowledge.retrieval import find_doc_numbers
from .parsing import parse_bytes
from .rules import CheckContext, run_checks
from .rules.textutil import split_sentences
from .schemas.common import IdAllocator, Severity, slot
from .schemas.ir import Attachment, AttachmentNote, Block, DocumentIR, Header, Imprint, Sentence, Signature
from .schemas.review import ReviewIssue
from .schemas.task import TaskSpec

LEVELS = [
    (1, re.compile(r"^([一二三四五六七八九十百]+、)(.*)$")),
    (2, re.compile(r"^(（[一二三四五六七八九十百]+）)(.*)$")),
    (3, re.compile(r"^(\d{1,2}\.)(?!\d)(.*)$")),
    (4, re.compile(r"^(（\d{1,2}）)(.*)$")),
]
DATE_LINE = re.compile(r"^(\d{4}年\d{1,2}月\d{1,2}日|[〇○零一二三四五六七八九]{4}年[一二三四五六七八九十]{1,3}月[一二三四五六七八九十]{1,3}日)$")
ATT_NOTE = re.compile(r"^附件[：:]\s*(.*)$")
ATT_ITEM = re.compile(r"^(\d{1,2})[.．、]\s*(.+)$")
ATT_HEAD = re.compile(r"^附件\s*(\d{0,2})$")
PRINT_LINE = re.compile(r"^(.+?)\s+(\d{4}年\d{1,2}月\d{1,2}日)\s*印发$")
SECRECY = re.compile(r"(绝密|机密|秘密)(★.*)?$")
URGENCY = {"特急", "加急", "特提", "平急"}
PUNCT_END = "。；！？，、：:;!?"
# 版头发文字号（含不规范写法，如“示卫发[2026]第05号”，以便格式检查指出问题）
LOOSE_DOCNO = re.compile(r"^([一-鿿]{1,12}\s*[〔\[［(（【]\s*\d{4}\s*[〕\]］)）】]\s*第?\s*\d+\s*号)")

# 依赖证据账本、提纲或前一版本的规则：外部文稿无法判定，统一改为人工核验提示
EVIDENCE_RULES = {"GW-FACT-002", "GW-SEM-005", "GW-BASIS-003", "GW-PH-002"}


def _clean(line: str) -> str:
    line = line.replace("　", " ").strip()
    line = re.sub(r"^#{1,6}\s*", "", line)
    line = re.sub(r"^\*\*(.+)\*\*$", r"\1", line)
    return line.strip()


def _genre_of_title(title: str) -> str | None:
    title = title.rstrip("。，；：！？.,;:!? ")  # 标题末尾误加的标点另由格式检查指出，不影响文种识别
    names = sorted(set(kb.genres()) | set((kb.seed("genres.yaml").get("aliases") or {}).keys()), key=len, reverse=True)
    for n in names:
        if title.endswith(n):
            return kb.canonical_genre(n)
    if title.endswith("令"):
        return "命令（令）"
    return None


def _is_header_line(line: str) -> str | None:
    if re.fullmatch(r"\d{6}", line):
        return "copy_no"
    if SECRECY.search(line) and len(line) <= 16:
        return "secrecy"
    if line in URGENCY:
        return "urgency"
    if line.startswith("签发人"):
        return "signer"
    if (find_doc_numbers(line) or LOOSE_DOCNO.match(line)) and len(line) <= 40 and not line.endswith("。"):
        return "doc_number"
    if line.endswith("文件") and len(line) <= 30:
        return "organ_mark"
    return None


def _organ_like(line: str) -> bool:
    """署名行：短、无句末标点、无阿拉伯数字，且不是表格行、附件说明或层次标题。"""
    return (
        len(line) <= 30
        and line[-1:] not in PUNCT_END
        and not line.startswith("|")
        and not re.search(r"\d", line)
        and not ATT_NOTE.match(line)
        and not any(p.match(line) for _, p in LEVELS)
    )


@dataclass
class _Builder:
    ids: IdAllocator = field(default_factory=IdAllocator)

    def sentences(self, text: str) -> list[Sentence]:
        return [Sentence(sid=self.ids.next("S"), text=t, origin="human") for t in split_sentences(text)]

    def block(self, line: str) -> Block:
        for level, pat in LEVELS:
            m = pat.match(line)
            if not m:
                continue
            label, rest = m.group(1), m.group(2).strip()
            cut = rest.find("。")
            if cut == -1:
                return Block(bid=self.ids.next("B"), kind="heading", level=level, label=label, heading=rest)
            if level <= 2:
                # 一、二级标题按惯例不带句号；带句号的是列项式正文（如“一、拟申请……。”），按句子检查
                return Block(bid=self.ids.next("B"), kind="heading", level=level, label=label, heading="", sentences=self.sentences(rest), inline_heading=True)
            # 三、四级标题常与正文同段：“1.标题。正文……”
            head, body = rest[: cut + 1], rest[cut + 1 :]
            return Block(bid=self.ids.next("B"), kind="heading", level=level, label=label, heading=head, sentences=self.sentences(body), inline_heading=True)
        return Block(bid=self.ids.next("B"), kind="paragraph", sentences=self.sentences(line))

    def table(self, rows: list[str]) -> Block:
        cells = []
        for r in rows:
            parts = [c.strip() for c in r.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in parts if c):
                continue
            cells.append(parts)
        return Block(bid=self.ids.next("B"), kind="table", table=cells)


def ir_from_text(text: str, *, doc_id: str = "EXT", genre: str | None = None, direction: str | None = None) -> DocumentIR:
    lines = [_clean(x) for x in text.splitlines()]
    lines = [x for x in lines if x]
    b = _Builder()
    header = Header()
    signers: list[str] = []
    i = 0
    while i < len(lines):
        kind = _is_header_line(lines[i])
        if kind is None:
            break
        if kind == "signer":
            signers = [s for s in re.split(r"[\s、，]+", lines[i].split("：", 1)[-1].split(":", 1)[-1]) if s]
            # “签发人”与发文字号同行时一并识别
            nums = find_doc_numbers(lines[i])
            if nums:
                header.doc_number = nums[0]
        elif kind == "doc_number":
            m0 = LOOSE_DOCNO.match(lines[i])
            header.doc_number = re.sub(r"\s+", "", m0.group(1)) if m0 else find_doc_numbers(lines[i])[0]
            m = re.search(r"签发人[：:]\s*(.+)$", lines[i])
            if m:
                signers = [s for s in re.split(r"[\s、，]+", m.group(1)) if s]
        else:
            setattr(header, kind, lines[i])
        i += 1
    header.signers = signers
    title = lines[i] if i < len(lines) else ""
    i += 1
    # 标题分行（PDF、手工断行常见）：其后至多两行都无句末标点、最后一行以文种结尾时合并为标题
    if not _genre_of_title(title) and title[-1:] not in PUNCT_END:
        for k in (1, 2):
            tail = lines[i : i + k]
            if len(tail) == k and all(len(x) <= 40 and x[-1:] not in PUNCT_END and not x.endswith(("：", ":")) for x in tail) and _genre_of_title(tail[-1]):
                title += "".join(tail)
                i += k
                break
    recipients: list[str] = []
    if i < len(lines) and lines[i][-1:] in "：:" and len(lines[i]) <= 160 and not any(p.match(lines[i]) for _, p in LEVELS):
        recipients = [r for r in re.split(r"[、，,]", lines[i][:-1]) if r.strip()]
        i += 1
    rest = lines[i:]

    # ---- 从尾部识别版记、附件正文、附注、署名与日期
    imprint = Imprint(print_date="")
    while rest and (PRINT_LINE.match(rest[-1]) or rest[-1].startswith(("抄送", "主送"))):
        last = rest.pop()
        m = PRINT_LINE.match(last)
        if m:
            imprint.printer, imprint.print_date = m.group(1).strip(), m.group(2) + "印发"
        elif last.startswith("抄送"):
            imprint.cc = [c for c in re.split(r"[，、,]", last.split("：", 1)[-1].rstrip("。")) if c.strip()]
        else:
            imprint.main_moved = [c for c in re.split(r"[，、,]", last.split("：", 1)[-1].rstrip("。")) if c.strip()]
    attachments: list[Attachment] = []
    att_start = next((k for k, x in enumerate(rest) if ATT_HEAD.match(x) and k > 0 and any(DATE_LINE.match(y) for y in rest[:k])), None)
    if att_start is not None:
        chunk, cur = rest[att_start:], None
        rest = rest[:att_start]
        att_table: list[str] = []
        for x in chunk:
            m = ATT_HEAD.match(x)
            if cur is not None and att_table and (m or not x.startswith("|")):
                cur.blocks.append(b.table(att_table))  # 附件中的表格同样按表格块处理
                att_table = []
            if m:
                cur = Attachment(seq=int(m.group(1) or 1), title="")
                attachments.append(cur)
            elif cur is not None and not cur.title:
                cur.title = x
            elif cur is not None and x.startswith("|"):
                att_table.append(x)
            elif cur is not None:
                cur.blocks.append(b.block(x))
        if cur is not None and att_table:
            cur.blocks.append(b.table(att_table))
    note = ""
    if rest and re.fullmatch(r"[（(].+[）)]", rest[-1]) and len(rest[-1]) <= 120:
        note = rest.pop()[1:-1]
    sig = Signature(organs=[], date="")
    if rest and DATE_LINE.match(rest[-1]):
        sig.date = rest.pop()
        while rest and len(sig.organs) < 4 and _organ_like(rest[-1]):
            sig.organs.insert(0, rest.pop())
    notes: list[AttachmentNote] = []
    k = next((n for n, x in enumerate(rest) if ATT_NOTE.match(x)), None)
    if k is not None:
        first = ATT_NOTE.match(rest[k]).group(1).strip()
        items = [first] + rest[k + 1 :]
        rest = rest[:k]
        for n, it in enumerate(x for x in items if x):
            m = ATT_ITEM.match(it)
            label = it[: m.start(2)].strip() if m else ""
            notes.append(AttachmentNote(seq=int(m.group(1)) if m else n + 1, name=(m.group(2) if m else it).strip(), label=label))

    # ---- 正文
    blocks: list[Block] = []
    table_buf: list[str] = []
    for x in rest:
        if x.startswith("|"):
            table_buf.append(x)
            continue
        if table_buf:
            blocks.append(b.table(table_buf))
            table_buf = []
        blocks.append(b.block(x))
    if table_buf:
        blocks.append(b.table(table_buf))

    g = genre or _genre_of_title(title)
    info = kb.genre(g) if g else None
    if direction is None:
        direction = "上行文" if signers else (info.directions[0] if info and info.directions else "")
    return DocumentIR(
        doc_id=doc_id,
        matter_id="EXT",
        genre=g if info and info.statutory else None,
        material_type=None if info and info.statutory else g,
        format_type=(info.format if info else "general"),
        direction=direction,
        header=header,
        title=title,
        recipients=recipients,
        blocks=blocks,
        attachment_notes=notes,
        signature=sig,
        note=note,
        attachments=attachments,
        imprint=imprint,
        meta={"source": "imported", "author": "external"},
    )


def ir_from_file(filename: str, data: bytes, **kw) -> DocumentIR:
    """DOCX/PDF/TXT/MD：先用材料解析器按原顺序取出可见文本，再识别结构。

    文件无法解析或解析不出文字时报错，不把空文稿当作“已检查/已排版”；其余解析提示记入 meta。
    """
    if filename.lower().endswith((".txt", ".md", ".markdown")):
        text = data.decode("utf-8", errors="replace")
        if not text.strip():
            raise ValueError(f"无法从 {filename} 读取文稿内容：文件为空")
        return ir_from_text(text, **kw)
    res = parse_bytes("EXT", filename, data)
    tables = {t.table_id: t for t in res.tables}
    emitted: set[str] = set()
    lines: list[str] = []
    for u in res.units:
        if u.kind == "table_cell":
            tid = u.attrs.get("table", "")
            if tid in tables and tid not in emitted:  # 表格放回原位置
                t = tables[tid]
                rows = ([t.header] if t.header else []) + t.rows
                lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
                emitted.add(tid)
            continue
        if u.kind in ("comment", "footnote"):
            continue
        lines.append(u.text)
    if not any(x.strip() for x in lines):
        raise ValueError(f"无法从 {filename} 读取文稿内容：{'；'.join(res.warnings) or '未解析出任何文字（扫描件须先做文字识别）'}")
    ir = ir_from_text("\n".join(lines), **kw)
    if res.warnings:
        ir.meta["import_warnings"] = "；".join(res.warnings)
    return ir


@dataclass
class ExternalCheck:
    ir: DocumentIR
    issues: list[ReviewIssue]
    unverifiable: list[str]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.issues:
            out[i.severity.value] = out.get(i.severity.value, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "title": self.ir.title,
            "genre": self.ir.genre or self.ir.material_type,
            "direction": self.ir.direction,
            "counts": self.counts(),
            "issues": [
                {
                    "id": i.issue_id,
                    "severity": i.severity.value,
                    "type": i.type.value,
                    "location": i.location.label or i.location.field or "",
                    "original": i.original,
                    "suggestion": i.suggestion,
                    "rule": f"{i.rule.rule_id} {i.rule.source}（{i.rule.level.value}）" if i.rule else "",
                    "needs_human": i.needs_human,
                }
                for i in self.issues
            ],
            "unverifiable": self.unverifiable,
        }


def check_external(ir: DocumentIR, runtime=None, *, as_of: date | None = None, region: str | None = None, groups: list[str] | None = None) -> ExternalCheck:
    """对外部文稿运行确定性检查。依赖证据账本的规则不作判定，转为“须人工核验”清单。"""
    profile = runtime.profile if runtime is not None else kb.unit_profile("party_gov")
    lib = runtime.policies if runtime is not None else None
    if lib is None:
        from .knowledge.policy_library import PolicyLibrary

        lib = PolicyLibrary()
    task = TaskSpec(task_id="EXT", request_text=ir.title, policy_as_of=as_of or date.today())
    if region:
        task.region = slot(region, "已确认", "命令行参数")
    ctx = CheckContext(ir=ir, task=task, profile=profile, policy_library=lib, ids=IdAllocator())
    if runtime is not None:
        ctx.features = runtime.config.features
    issues = [i for i in run_checks(ctx, groups or ["genre", "language", "burden", "basis", "consistency", "format"]) if not (i.rule and i.rule.rule_id in EVIDENCE_RULES)]
    unverifiable = []
    nums = sum(1 for _, s in ir.iter_sentences() if any(c.isdigit() for c in s.text))
    if nums:
        unverifiable.append(f"文稿中有 {nums} 句含数字：外部文稿没有证据链，数字、时间、名称须对照原始材料逐项核验（条例第二十条（四））")
    unverifiable.append("事实状态（已完成/拟议）、措施是否获批、依据是否支持具体表述：须结合材料与审批记录人工核验")
    issues.sort(key=lambda x: -x.severity.rank)
    return ExternalCheck(ir=ir, issues=issues, unverifiable=unverifiable)


__all__ = ["ExternalCheck", "check_external", "ir_from_file", "ir_from_text", "Severity"]
