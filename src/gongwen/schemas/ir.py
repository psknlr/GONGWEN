"""公文中间表示 DocumentIR（设计 §5 阶段9）。

不把一大段生成文本当作最终文件：标题、主送机关、正文层级、附件说明、署名、
日期、版记分别存储，正文细化到“句”，每句携带证据引用，支撑：
* 证据侧栏（点击句子看到来源、公式、确认状态）；
* 定向修订（只改受影响的句子）；
* 跨文件一致性（同一事实在不同文稿中的引用）。
"""

from __future__ import annotations

from typing import Iterator

from pydantic import Field

from .common import DocStatus, EvidenceRef, GWModel

PLACEHOLDER_OPEN = "【"
PLACEHOLDER_CLOSE = "】"


class Sentence(GWModel):
    sid: str
    text: str
    refs: list[EvidenceRef] = Field(default_factory=list)
    function: str = Field(default="", description="依据/事实/分析/措施/条件/要求/请求/结语/背景/过渡")
    measure_id: str | None = None
    origin: str = Field(default="system", description="system/model/human/patch")

    @property
    def has_placeholder(self) -> bool:
        return PLACEHOLDER_OPEN + "待" in self.text


class Block(GWModel):
    bid: str
    kind: str = Field(description="heading/paragraph/table")
    level: int = Field(default=0, description="0=正文段落；1-4=层次标题")
    label: str = Field(default="", description="层次序数，如 一、（一）1.（1）")
    heading: str = Field(default="", description="层次标题文字（不含序数）")
    sentences: list[Sentence] = Field(default_factory=list)
    table: list[list[str]] | None = None
    plan_ref: str | None = None
    inline_heading: bool = Field(default=False, description="三四级标题与正文同段（如“1.标题。正文……”）")

    def text(self) -> str:
        body = "".join(s.text for s in self.sentences)
        if self.kind == "heading":
            if self.inline_heading and body:
                return f"{self.label}{self.heading}{body}"
            return f"{self.label}{self.heading}"
        return body


class Header(GWModel):
    copy_no: str | None = Field(default=None, description="份号；如需标注，6 位")
    secrecy: str | None = Field(default=None, description="密级和保密期限；涉密材料不在本系统处理")
    urgency: str | None = None
    organ_mark: str = Field(default="", description="发文机关标志")
    doc_number: str = Field(default="【待编号：发文字号由办理流程确定】")
    signers: list[str] = Field(default_factory=list, description="上行文签发人")


class Signature(GWModel):
    organs: list[str] = Field(default_factory=list)
    date: str = Field(default="【待签发后填写成文日期】")
    seal_mode: str = Field(default="seal", description="seal/no_seal/signature_stamp")
    signer_title: str = ""


class AttachmentNote(GWModel):
    seq: int
    name: str
    label: str = Field(default="", description="外部文稿中附件顺序号的原样写法（如“1、”），用于格式检查")


class Attachment(GWModel):
    seq: int
    title: str
    blocks: list[Block] = Field(default_factory=list)


class Imprint(GWModel):
    """版记：抄送机关、印发机关和印发日期。"""

    main_moved: list[str] = Field(default_factory=list, description="主送机关过多时移至版记")
    cc: list[str] = Field(default_factory=list)
    printer: str = ""
    print_date: str = Field(default="【待印发时填写】")


class Placeholder(GWModel):
    field: str
    reason: str
    location: str = ""


class DocumentIR(GWModel):
    doc_id: str
    matter_id: str
    version: int = 1
    status: DocStatus = DocStatus.DISCUSSION
    genre: str | None = None
    material_type: str | None = None
    format_type: str = "general"
    direction: str = ""
    header: Header = Field(default_factory=Header)
    title: str = ""
    recipients: list[str] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    attachment_notes: list[AttachmentNote] = Field(default_factory=list)
    signature: Signature = Field(default_factory=Signature)
    note: str = Field(default="", description="附注")
    attachments: list[Attachment] = Field(default_factory=list)
    imprint: Imprint = Field(default_factory=Imprint)
    attendees: dict[str, list[str]] = Field(default_factory=dict, description="纪要：出席/请假/列席")
    placeholders: list[Placeholder] = Field(default_factory=list)
    based_on_version: int | None = None
    meta: dict[str, str] = Field(default_factory=dict)

    # ---- 遍历与定位 -------------------------------------------------
    def iter_blocks(self, include_attachments: bool = True) -> Iterator[tuple[str, Block]]:
        for b in self.blocks:
            yield "body", b
        if include_attachments:
            for att in self.attachments:
                for b in att.blocks:
                    yield f"attachment{att.seq}", b

    def iter_sentences(self, include_attachments: bool = True) -> Iterator[tuple[Block, Sentence]]:
        for _, b in self.iter_blocks(include_attachments):
            for s in b.sentences:
                yield b, s

    def find_sentence(self, sid: str) -> tuple[Block, Sentence] | None:
        for b, s in self.iter_sentences():
            if s.sid == sid:
                return b, s
        return None

    def find_block(self, bid: str) -> Block | None:
        for _, b in self.iter_blocks():
            if b.bid == bid:
                return b
        return None

    def location_label(self, bid: str) -> str:
        """人类可读位置，如“第二部分第三段”。"""
        section_idx = 0
        para_idx = 0
        cn = "零一二三四五六七八九十"
        for scope, b in self.iter_blocks():
            if b.kind == "heading" and b.level == 1:
                section_idx += 1
                para_idx = 0
            elif b.kind != "heading" or b.level > 1:
                para_idx += 1
            if b.bid == bid:
                prefix = "" if scope == "body" else f"附件{scope.replace('attachment', '')}"
                sec = f"第{cn[section_idx] if section_idx < 11 else section_idx}部分" if section_idx else "开头部分"
                return f"{prefix}{sec}第{para_idx}段" if para_idx else f"{prefix}{sec}标题"
        return bid

    def body_text(self, include_attachments: bool = False) -> str:
        lines = [b.text() for _, b in self.iter_blocks(include_attachments)]
        return "\n".join(x for x in lines if x)

    def full_text(self) -> str:
        parts = [self.title]
        if self.recipients:
            parts.append("、".join(self.recipients) + "：")
        parts.append(self.body_text(include_attachments=False))
        if self.attachment_notes:
            parts.append("附件：" + "　".join(f"{n.seq}.{n.name}" if len(self.attachment_notes) > 1 else n.name for n in self.attachment_notes))
        parts.extend(self.signature.organs)
        parts.append(self.signature.date)
        if self.note:
            parts.append(f"（{self.note}）")
        for att in self.attachments:
            parts.append(f"附件{att.seq}" if len(self.attachments) > 1 else "附件")
            parts.append(att.title)
            parts.extend(b.text() for b in att.blocks)
        return "\n".join(p for p in parts if p)

    def to_markdown(self) -> str:
        out = [f"# {self.title}", ""]
        if self.recipients:
            out += ["、".join(self.recipients) + "：", ""]
        for _, b in self.iter_blocks(include_attachments=False):
            if b.kind == "heading" and not b.inline_heading:
                out += [f"{'#' * (b.level + 1)} {b.label}{b.heading}", ""]
            elif b.kind == "table" and b.table:
                out += ["| " + " | ".join(b.table[0]) + " |", "|" + "---|" * len(b.table[0])]
                out += ["| " + " | ".join(r) + " |" for r in b.table[1:]] + [""]
            else:
                out += [b.text(), ""]
        if self.attachment_notes:
            out += ["附件：" + "；".join(f"{n.seq}.{n.name}" if len(self.attachment_notes) > 1 else n.name for n in self.attachment_notes), ""]
        out += [*self.signature.organs, self.signature.date, ""]
        if self.note:
            out += [f"（{self.note}）", ""]
        for att in self.attachments:
            out += ["---", "", f"附件{att.seq}" if len(self.attachments) > 1 else "附件", "", f"## {att.title}", ""]
            for b in att.blocks:
                if b.kind == "heading" and not b.inline_heading:
                    out += [f"{'#' * (b.level + 2)} {b.label}{b.heading}", ""]
                elif b.kind == "table" and b.table:
                    out += ["| " + " | ".join(b.table[0]) + " |", "|" + "---|" * len(b.table[0])]
                    out += ["| " + " | ".join(r) + " |" for r in b.table[1:]] + [""]
                else:
                    out += [b.text(), ""]
        return "\n".join(out)
