"""审阅工作台页面：同时呈现文稿、证据、问题、待确认项和修改差异，并能一键定位。

同一页面既可作为离线静态文件（送审包内的 workbench.html），也可由本地服务提供
（gongwen serve），后者额外开放“处理待确认事项”的人工通道接口。
"""

from __future__ import annotations

import html
import json
from typing import Any

from ..layout.html_preview import CSS as DOC_CSS
from ..layout.html_preview import render_document
from ..schemas.ir import DocumentIR

PAGE_CSS = """
:root{--bg:#f4f5f7;--panel:#fff;--ink:#1f2328;--muted:#5f6b7a;--line:#d8dde3;--accent:#1f5fbf;--blocking:#c62828;--major:#ef6c00;--minor:#1565c0;--ok:#2e7d32;--doc-paper:#fff;--doc-ink:#111}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15181c;--panel:#1d2126;--ink:#e6e8eb;--muted:#9aa4af;--line:#2d333b;--accent:#6ea8ff;--doc-paper:#fbfaf7;--doc-ink:#111}}
:root[data-theme="dark"]{--bg:#15181c;--panel:#1d2126;--ink:#e6e8eb;--muted:#9aa4af;--line:#2d333b;--accent:#6ea8ff;--doc-paper:#fbfaf7;--doc-ink:#111}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
header.top{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
header.top h1{font-size:16px;margin:0;flex:1 1 320px}
.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-size:12px;border:1px solid var(--line)}
.badge.discussion{background:#fff4e5;color:#8a4b00;border-color:#f3c98b}
.badge.submission{background:#e8f5e9;color:#1b5e20;border-color:#a5d6a7}
.badge.approved{background:#e3f2fd;color:#0d47a1;border-color:#90caf9}
.counts span{margin-right:10px;font-size:12px;color:var(--muted)}
.counts b.blocking{color:var(--blocking)} .counts b.major{color:var(--major)} .counts b.minor{color:var(--minor)}
main{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:16px;padding:16px;max-width:1500px;margin:0 auto}
@media (max-width:1000px){main{grid-template-columns:1fr}}
.doc-wrap{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px 8px}
aside{background:var(--panel);border:1px solid var(--line);border-radius:8px;display:flex;flex-direction:column;min-height:60vh;max-height:calc(100vh - 90px);position:sticky;top:70px}
@media (max-width:1000px){aside{position:static;max-height:none}}
.tabs{display:flex;border-bottom:1px solid var(--line);overflow-x:auto}
.tabs button{flex:1;min-width:72px;border:0;background:none;padding:10px 6px;color:var(--muted);font:inherit;cursor:pointer;border-bottom:2px solid transparent}
.tabs button.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.pane{display:none;overflow:auto;padding:12px 14px;flex:1}
.pane.on{display:block}
.card{border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin-bottom:8px;background:var(--bg)}
.card h4{margin:0 0 4px;font-size:13px}
.card .meta{color:var(--muted);font-size:12px}
.card.blocking{border-left:4px solid var(--blocking)} .card.major{border-left:4px solid var(--major)} .card.minor{border-left:4px solid var(--minor)} .card.info{border-left:4px solid var(--line)}
.card.clickable{cursor:pointer}
.kv{display:grid;grid-template-columns:72px 1fr;gap:2px 8px;font-size:12px}
.kv dt{color:var(--muted)} .kv dd{margin:0;word-break:break-all}
.st{font-size:11px;padding:0 6px;border-radius:4px;border:1px solid var(--line);margin-left:4px}
.st.ok{color:var(--ok)} .st.warn{color:var(--major)} .st.bad{color:var(--blocking)}
del{background:#ffebee;color:#b71c1c;text-decoration:line-through} ins{background:#e8f5e9;color:#1b5e20;text-decoration:none}
.empty{color:var(--muted);font-size:13px;padding:8px 0}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:4px 10px;font:inherit;font-size:12px;cursor:pointer;margin:4px 4px 0 0}
.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
textarea{width:100%;min-height:52px;font:inherit;border:1px solid var(--line);border-radius:6px;padding:6px;background:var(--panel);color:var(--ink)}
.notice{font-size:12px;color:var(--muted);padding:8px 16px;text-align:center}
"""

PAGE_JS = r"""
const D = JSON.parse(document.getElementById('gw-data').textContent);
const $ = (s, r=document) => r.querySelector(s); const $$ = (s, r=document) => [...r.querySelectorAll(s)];
function tab(name){ $$('.tabs button').forEach(b=>b.classList.toggle('on', b.dataset.t===name)); $$('.pane').forEach(p=>p.classList.toggle('on', p.id==='p-'+name)); }
$$('.tabs button').forEach(b=>b.onclick=()=>tab(b.dataset.t));
function esc(t){return String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function focusSid(sid){ $$('.gw-doc span.s').forEach(x=>x.classList.remove('active')); const el=$(`.gw-doc span.s[data-sid="${sid}"]`); if(el){el.classList.add('active'); el.scrollIntoView({behavior:'smooth',block:'center'});} }
function focusBid(bid){ const el=$(`.gw-doc [data-bid="${bid}"]`); if(el){el.scrollIntoView({behavior:'smooth',block:'center'}); el.animate([{background:'rgba(64,120,220,.25)'},{background:'transparent'}],{duration:1600});} }
function showEvidence(sid){
  const row = D.evidence[sid]; const pane = $('#p-evidence'); tab('evidence'); focusSid(sid);
  if(!row){ pane.innerHTML = '<div class="empty">该句没有关联证据。</div>'; return; }
  let h = `<div class="card"><h4>${esc(row.location)}</h4><div>${esc(row.text)}</div><div class="meta">${esc(row.confirmation)}</div></div>`;
  if(!row.refs.length) h += '<div class="empty">该句未引用事实、依据或措施（如为过渡语、结束语属正常）。</div>';
  for(const r of row.refs){
    const cls = r.status==='已核实事实'||r.status==='计算结果'||r.status==='已批准事项'||r.status==='适用' ? 'ok' : (r.status==='冲突'||r.status==='不适用' ? 'bad' : 'warn');
    h += `<div class="card"><h4>${esc(r.label)}<span class="st ${cls}">${esc(r.status||'')}</span></h4><dl class="kv">`;
    for(const [k,v] of Object.entries(r.detail||{})) if(v) h += `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`;
    h += '</dl></div>';
  }
  const iss = D.issues.filter(i=>i.sid===sid);
  if(iss.length){ h += '<h4>该句的审校问题</h4>' + iss.map(issueCard).join(''); }
  pane.innerHTML = h;
}
function sevClass(s){return {'阻断送审':'blocking','重要':'major','一般':'minor'}[s]||'info';}
function issueCard(i){
  return `<div class="card ${sevClass(i.severity)} clickable" data-sid="${esc(i.sid||'')}" data-bid="${esc(i.bid||'')}"><h4>${esc(i.id)} · ${esc(i.type)} <span class="st">${esc(i.severity)}</span>${i.needs_human?'<span class="st warn">需人工</span>':''}</h4>
  <div class="meta">${esc(i.location)}｜${esc(i.channel)}</div>${i.original?`<div>原文：“${esc(i.original)}”</div>`:''}${i.evidence_text?`<div class="meta">对应材料：“${esc(i.evidence_text)}”</div>`:''}<div>${esc(i.suggestion)}</div>${i.rule?`<div class="meta">依据：${esc(i.rule)}</div>`:''}</div>`;
}
function renderIssues(){
  const pane = $('#p-issues');
  pane.innerHTML = D.issues.length ? D.issues.map(issueCard).join('') : '<div class="empty">没有未处理的问题。</div>';
  $$('.card.clickable', pane).forEach(c=>c.onclick=()=>{ if(c.dataset.sid){focusSid(c.dataset.sid);} else if(c.dataset.bid){focusBid(c.dataset.bid);} });
}
function renderPending(){
  const pane = $('#p-pending'); let h = '';
  for(const cp of D.checkpoints){
    h += `<div class="card major"><h4>${esc(cp.kind)}：${esc(cp.question)}</h4>${(cp.details||[]).map(d=>`<div class="meta">· ${esc(d)}</div>`).join('')}`;
    if(D.api){ h += `<textarea placeholder="说明（可选）" data-note="${esc(cp.cp_id)}"></textarea><textarea placeholder='附加数据（JSON，可选，如 {"confirm_facts":["F-003"]}）' data-json="${esc(cp.cp_id)}"></textarea>` + cp.options.map(o=>`<button class="btn ${o.key==='accept'||o.key==='submit'?'primary':''}" title="${esc(o.effect||'')}" data-cp="${esc(cp.cp_id)}" data-opt="${esc(o.key)}">${esc(o.label)}</button>`).join(''); }
    else { h += '<div class="meta">请在命令行运行 <code>gongwen task confirm</code> 或启动 <code>gongwen serve</code> 处理。</div>'; }
    h += '</div>';
  }
  for(const p of D.pending){ h += `<div class="card ${p.blocking?'blocking':'info'}"><h4>${esc(p.kind)}</h4><div>${esc(p.description)}</div>${p.location?`<div class="meta">${esc(p.location)}</div>`:''}</div>`; }
  pane.innerHTML = h || '<div class="empty">没有待确认事项。</div>';
  $$('button[data-cp]', pane).forEach(b=>b.onclick=async()=>{
    const note = ($(`textarea[data-note="${b.dataset.cp}"]`)||{}).value||'';
    let data = {}; const raw = ($(`textarea[data-json="${b.dataset.cp}"]`)||{}).value||'';
    if(raw.trim()){ try{ data = JSON.parse(raw); }catch(e){ alert('附加数据不是有效 JSON'); return; } }
    b.disabled = true;
    const r = await fetch(D.api+'/checkpoint', {method:'POST', headers:{'Content-Type':'application/json','X-GW-Token':D.token}, body: JSON.stringify({task_id:D.task_id, cp_id:b.dataset.cp, option:b.dataset.opt, note, data})});
    const j = await r.json(); alert(j.message || (r.ok?'已处理':'处理失败')); if(r.ok) location.reload(); else b.disabled=false;
  });
}
function renderVersions(){
  const pane = $('#p-versions'); let h='';
  for(const v of D.versions){ h += `<div class="card"><h4>第 ${v.version} 版 <span class="meta">${esc(v.created_at)}｜${esc(v.author)}</span></h4><div>${esc(v.summary)}</div>${(v.semantic_changes||[]).map(s=>`<div class="meta">语义变化：${esc(s)}</div>`).join('')}</div>`; }
  for(const p of D.patches){ h += `<div class="card ${p.status==='needs_human'?'major':'info'} clickable" data-sid="${esc(p.target)}"><h4>${esc(p.id)} · ${esc(p.op)} <span class="st">${esc(p.status)}</span></h4><div class="meta">${esc(p.reason)}</div><div><del>${esc(p.before)}</del></div><div><ins>${esc(p.after)}</ins></div></div>`; }
  pane.innerHTML = h || '<div class="empty">暂无修改记录。</div>';
  $$('.card.clickable', pane).forEach(c=>c.onclick=()=>focusSid(c.dataset.sid));
}
function renderLayout(){
  const pane = $('#p-layout'); const L = D.layout; if(!L){pane.innerHTML='<div class="empty">尚未排版。</div>';return;}
  let h = `<div class="card"><h4>渲染核验</h4><div class="meta">${esc(L.render.rendered?('已实际渲染（'+L.render.renderer+'），共 '+L.render.pages+' 页'):'未进行实际渲染核验')}</div>${(L.render.substitutions||[]).map(s=>`<div class="meta">字体替代：${esc(s)}</div>`).join('')}${(L.render.notes||[]).map(s=>`<div class="meta">${esc(s)}</div>`).join('')}</div>`;
  for(const c of L.checks){ const cls = c.status==='pass'?'ok':(c.status==='fail'?'bad':'warn'); h += `<div class="card"><h4>${esc(c.item)}<span class="st ${cls}">${esc(c.status)}</span></h4><div class="meta">要求：${esc(c.expected)}｜实际：${esc(c.actual)}</div><div class="meta">${esc(c.clause)}${c.conditional?'（条件性要求）':''} ${esc(c.note||'')}</div></div>`; }
  pane.innerHTML = h;
}
$$('.gw-doc span.s').forEach(s=>s.onclick=()=>showEvidence(s.dataset.sid));
renderIssues(); renderPending(); renderVersions(); renderLayout();
$('#p-evidence').innerHTML = '<div class="empty">点击正文中的句子，查看其来源、计算公式、适用条件和确认状态。</div>';
"""


def build_page(ir: DocumentIR, data: dict[str, Any], api: str | None = None, token: str | None = None) -> str:
    flags: dict[str, str] = {}
    rank = {"blocking": 3, "major": 2, "minor": 1}
    for i in data.get("issues", []):
        sev = {"阻断送审": "blocking", "重要": "major", "一般": "minor"}.get(i.get("severity"))
        sid = i.get("sid")
        if sid and sev and rank[sev] > rank.get(flags.get(sid, ""), 0):
            flags[sid] = sev
    doc_html = render_document(ir, flags, label=f"{ir.status.value}｜本稿由公文智能体辅助起草，须经人工审核")
    data = dict(data)
    data["api"] = api
    data["token"] = token
    # 嵌入 <script> 的数据中转义 < > &：材料文字中的 "<!--<script>" 等不能改变 HTML 解析状态、吞掉页面脚本
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    status_cls = {"讨论稿": "discussion", "送审稿": "submission", "经批准的待印发版本": "approved"}.get(ir.status.value, "discussion")
    counts = data.get("counts", {})
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>公文审阅工作台</title>
<style>{PAGE_CSS}{DOC_CSS}</style></head>
<body>
<header class="top"><h1>{html.escape(ir.title)}</h1>
<span class="badge {status_cls}">{html.escape(ir.status.value)}</span>
<span class="counts"><span>阻断 <b class="blocking">{counts.get('阻断送审', 0)}</b></span><span>重要 <b class="major">{counts.get('重要', 0)}</b></span><span>一般 <b class="minor">{counts.get('一般', 0)}</b></span><span>待确认 <b>{len(data.get('checkpoints', [])) + len(data.get('pending', []))}</b></span></span>
<span class="counts"><span>第 {ir.version} 版</span><span>{html.escape(data.get('genre') or '')}</span></span></header>
<div class="notice">本页为附带可核验证据包的草稿：系统不形成审批结论；成文日期、发文字号与签发信息须来自真实办理流程。</div>
<main><section class="doc-wrap">{doc_html}</section>
<aside><nav class="tabs"><button data-t="evidence" class="on">证据</button><button data-t="issues">问题</button><button data-t="pending">待确认</button><button data-t="versions">修改记录</button><button data-t="layout">版式</button></nav>
<div class="pane on" id="p-evidence"></div><div class="pane" id="p-issues"></div><div class="pane" id="p-pending"></div><div class="pane" id="p-versions"></div><div class="pane" id="p-layout"></div></aside></main>
<script type="application/json" id="gw-data">{payload}</script>
<script>{PAGE_JS}</script>
</body></html>"""
