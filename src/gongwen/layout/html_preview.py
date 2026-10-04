"""DocumentIR → HTML 预览（用于审阅工作台）：每句带 data-sid，可点击定位证据与问题。"""

from __future__ import annotations

import html

from ..schemas.ir import Block, DocumentIR
from .profile import split_title

CSS = """
.gw-doc{font-family:"FangSong","仿宋","仿宋_GB2312","STFangsong","Songti SC",serif;font-size:16pt;line-height:28.95pt;color:var(--doc-ink,#111);
  background:var(--doc-paper,#fff);width:156mm;max-width:100%;margin:0 auto;padding:12mm 10mm 16mm;box-sizing:content-box}
.gw-doc .mark{color:#d00;text-align:center;font-family:"方正小标宋简体","STZhongsong","Songti SC",serif;font-size:30pt;line-height:1.2;margin:8mm 0 6mm;letter-spacing:.05em}
.gw-doc .docno{display:flex;justify-content:center;gap:2em;padding-bottom:4mm;border-bottom:1.5px solid #d00}
.gw-doc .docno.up{justify-content:space-between;padding-left:1em;padding-right:1em}
.gw-doc .title{text-align:center;font-family:"方正小标宋简体","STZhongsong","Songti SC",serif;font-size:22pt;line-height:1.45;margin:14mm 0 7mm}
.gw-doc p{margin:0;text-indent:2em}
.gw-doc p.flush{text-indent:0}
.gw-doc .h1{font-family:"SimHei","黑体","Heiti SC",sans-serif}
.gw-doc .h2{font-family:"KaiTi","楷体","楷体_GB2312","STKaiti",serif}
.gw-doc .sig{text-align:right;margin-top:14mm}
.gw-doc .note{text-indent:2em}
.gw-doc .att{margin-top:14mm;border-top:1px dashed #999;padding-top:6mm}
.gw-doc table{border-collapse:collapse;margin:4mm auto;font-size:14pt;line-height:1.5}
.gw-doc td,.gw-doc th{border:1px solid #333;padding:1mm 3mm;text-align:center}
.gw-doc .imprint{margin-top:16mm;border-top:1.4px solid #111;border-bottom:1.4px solid #111;font-size:14pt;line-height:1.9}
.gw-doc .imprint div{padding:0 1em}
.gw-doc .imprint .printer{display:flex;justify-content:space-between;border-top:1px solid #111}
.gw-doc .ph{background:#fff3a3;color:#7a5b00;border-radius:2px;padding:0 2px}
.gw-doc span.s{cursor:pointer;border-radius:2px}
.gw-doc span.s:hover{background:rgba(64,120,220,.10)}
.gw-doc span.s.flag-blocking{text-decoration:underline wavy #c62828;text-underline-offset:4px}
.gw-doc span.s.flag-major{text-decoration:underline wavy #ef6c00;text-underline-offset:4px}
.gw-doc span.s.flag-minor{text-decoration:underline dotted #1565c0;text-underline-offset:4px}
.gw-doc span.s.active{background:rgba(64,120,220,.22)}
.gw-label{font:12px/1.4 system-ui,sans-serif;color:#666;text-align:center;margin-bottom:4mm}
"""


def esc(t: str) -> str:
    out = html.escape(t)
    return out.replace("【", '<span class="ph">【').replace("】", "】</span>")


def _sentences(b: Block, flags: dict[str, str]) -> str:
    parts = []
    for s in b.sentences:
        cls = "s" + (f" flag-{flags[s.sid]}" if s.sid in flags else "")
        parts.append(f'<span class="{cls}" data-sid="{s.sid}" data-bid="{b.bid}">{esc(s.text)}</span>')
    return "".join(parts)


def _blocks(blocks: list[Block], flags: dict[str, str]) -> str:
    out = []
    for b in blocks:
        if b.kind == "heading":
            cls = "h1" if b.level == 1 else ("h2" if b.level == 2 else "")
            inner = f'<span class="{cls}" data-bid="{b.bid}">{esc(b.label + b.heading)}</span>'
            if b.inline_heading:
                inner += _sentences(b, flags)
            out.append(f'<p data-bid="{b.bid}">{inner}</p>')
        elif b.kind == "table" and b.table:
            rows = []
            for i, r in enumerate(b.table):
                tag = "th" if i == 0 else "td"
                rows.append("<tr>" + "".join(f"<{tag}>{esc(c)}</{tag}>" for c in r) + "</tr>")
            out.append(f'<table data-bid="{b.bid}">' + "".join(rows) + "</table>")
        else:
            out.append(f'<p data-bid="{b.bid}">{_sentences(b, flags)}</p>')
    return "\n".join(out)


def render_document(ir: DocumentIR, flags: dict[str, str] | None = None, label: str = "") -> str:
    flags = flags or {}
    h = ir.header
    parts = ['<div class="gw-doc">']
    if label:
        parts.append(f'<div class="gw-label">{esc(label)}</div>')
    for v in (h.copy_no, h.secrecy, h.urgency):
        if v:
            parts.append(f'<p class="flush">{esc(v)}</p>')
    if h.organ_mark:
        parts.append(f'<div class="mark">{esc(h.organ_mark)}</div>')
    doc_number = "【待编号】" if h.doc_number.startswith("【待") else h.doc_number
    if ir.format_type not in ("jiyao",):
        if ir.direction == "上行文":
            signers = "　".join(h.signers) if h.signers else "【待签发人】"
            parts.append(f'<div class="docno up"><span>{esc(doc_number)}</span><span>签发人：{esc(signers)}</span></div>')
        else:
            parts.append(f'<div class="docno"><span>{esc(doc_number)}</span></div>')
    issuer = ir.signature.organs[0] if ir.signature.organs else ""
    parts.append('<div class="title">' + "<br>".join(esc(x) for x in split_title(ir.title, 20, issuer)) + "</div>")
    if ir.recipients:
        parts.append(f'<p class="flush">{esc("、".join(ir.recipients))}：</p>')
    parts.append(_blocks(ir.blocks, flags))
    if ir.attachment_notes:
        names = [f"{n.seq}.{n.name}" if len(ir.attachment_notes) > 1 else n.name for n in ir.attachment_notes]
        parts.append(f'<p style="margin-top:1em">附件：{esc("　".join(names))}</p>')
    if ir.attendees:
        for k, v in ir.attendees.items():
            parts.append(f'<p><b>{esc(k)}：</b>{esc("、".join(v))}</p>')
    parts.append('<div class="sig">' + "".join(f"<div>{esc(o)}</div>" for o in ir.signature.organs) + f"<div>{esc(ir.signature.date)}</div></div>")
    if ir.note:
        parts.append(f'<p class="note">（{esc(ir.note.strip("（）"))}）</p>')
    single = len(ir.attachments) == 1 and len(ir.attachment_notes) <= 1
    for att in ir.attachments:
        parts.append(f'<div class="att"><p class="flush h1">{"附件" if single else f"附件{att.seq}"}</p><div class="title">{esc(att.title)}</div>{_blocks(att.blocks, flags)}</div>')
    imp = ir.imprint
    if imp.cc or imp.printer:
        rows = []
        if imp.main_moved:
            rows.append(f"<div>主送：{esc('，'.join(imp.main_moved))}。</div>")
        if imp.cc:
            rows.append(f"<div>抄送：{esc('，'.join(imp.cc))}。</div>")
        rows.append(f'<div class="printer"><span>{esc(imp.printer)}</span><span>{esc(imp.print_date)}</span></div>')
        parts.append('<div class="imprint">' + "".join(rows) + "</div>")
    parts.append("</div>")
    return "\n".join(parts)


def standalone_html(ir: DocumentIR, title: str = "") -> str:
    body = render_document(ir, label=f"{ir.status.value}｜本稿由公文智能体辅助起草，须经人工审核")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title or ir.title)}</title><style>body{{background:#eceff1;margin:0;padding:16px}}{CSS}</style></head><body>{body}</body></html>"""
