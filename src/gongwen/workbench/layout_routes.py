"""本地工作台的模板与预览页面（gongwen serve）：

* GET  /templates?name=<模板>   模板列表与常用设置的编辑表单（字体、字号、发文机关标志颜色与位置、页边距、单位信息），
  “预览”按示例公文渲染页面图像；
* POST /api/template            保存用户模板（服务端校验，不合规时拒绝并说明）；
* POST /api/template/preview    按表单内容（不保存）渲染示例公文，返回页面图像地址与核验结果；
* GET  /preview/<任务>           任务当前排版稿的页面图像与核验结果（尚未生成时由页面发起 POST /api/preview）；
* POST /api/preview             生成任务预览；
* GET  /preview-img/<编号>/page-N.png  只从数据目录 previews/ 下生成的预览目录读取图像，校验编号与文件名，拒绝路径穿越。

写操作沿用工作台的安全约束（仅本机、X-GW-Token、请求大小限制），在 server.py 中统一检查。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from ..layout.fonts import ROLE_LABELS, ROLES
from ..layout.preview import PAGE_RE, build_html, file_digest, preview_docx, preview_ir, write_manifest
from ..layout.templates import BASE_PROFILES, Template, TemplateError, TemplateStore, _get, sample_ir
from ..schemas.layout import LayoutReport

PREVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_RENDER_LOCK = threading.Lock()
MAX_TEMPLATE_PREVIEWS = 20

SIZE_FIELDS = [
    ("elements.title.size", "标题字号"),
    ("elements.body.size", "正文字号"),
    ("elements.organ_mark.max_size", "发文机关标志字号（上限）"),
    ("elements.doc_number.size", "发文字号字号"),
    ("elements.title_note.size", "题注字号"),
    ("elements.imprint.size", "版记字号"),
    ("elements.page_number.size", "页码字号"),
]
ROLE_FIELDS = [
    ("elements.title.font", "标题字体"),
    ("elements.body.font", "正文字体"),
    ("elements.levels.1", "一级标题字体"),
    ("elements.levels.2", "二级标题字体"),
    ("elements.levels.3", "三级标题字体"),
    ("elements.levels.4", "四级标题字体"),
]
NUM_FIELDS = [
    ("elements.organ_mark.top_from_type_area_mm", "发文机关标志上边缘至版心上边缘（mm）"),
    ("margins.top_mm", "天头（上白边，mm）"),
    ("margins.left_mm", "订口（左白边，mm）"),
    ("elements.title.max_chars_per_line", "标题每行最多字数（空为按字号推算）"),
]
UNIT_FIELDS = [
    ("organ_mark", "发文机关标志（如 ××市××局文件）"),
    ("letter_organ_mark", "信函格式机关名称"),
    ("doc_number_prefix", "发文字号代字（如 示卫发）"),
    ("printer", "印发机关"),
    ("cc", "默认抄送机关（逗号分隔）"),
    ("brief_name", "简报名称"),
    ("brief_issuer", "简报编印单位"),
]

PAGE_CSS = """
:root{--bg:#f4f5f7;--panel:#fff;--ink:#1f2328;--muted:#5f6b7a;--line:#d8dde3;--accent:#1f5fbf;--warn:#b26a00;--bad:#c62828;--ok:#2e7d32}
@media (prefers-color-scheme: dark){:root{--bg:#15181c;--panel:#1d2126;--ink:#e6e8eb;--muted:#9aa4af;--line:#2d333b;--accent:#6ea8ff;--warn:#ffb74d;--bad:#ef9a9a;--ok:#81c784}}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header h1{font-size:16px;margin:0} a{color:var(--accent)}
main{display:grid;grid-template-columns:minmax(0,460px) minmax(0,1fr);gap:16px;padding:16px;max-width:1500px;margin:0 auto}
@media (max-width:1000px){main{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
fieldset{border:1px solid var(--line);border-radius:6px;margin:0 0 10px;padding:6px 10px}
legend{color:var(--muted);font-size:12px}
label{display:grid;grid-template-columns:150px 1fr;gap:8px;align-items:center;margin:4px 0;font-size:13px}
input,select{font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--ink);min-width:0}
input[type=color]{padding:0;height:28px}
.btn{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;padding:5px 12px;font:inherit;cursor:pointer;margin:4px 6px 0 0}
.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.meta{color:var(--muted);font-size:12px} .err{color:var(--bad);white-space:pre-wrap} .warn{color:var(--warn)}
.list a{margin-right:10px} .pages{display:flex;flex-wrap:wrap;gap:12px} .pages img{width:340px;max-width:100%;border:1px solid var(--line);background:#fff}
ul{padding-left:18px;margin:4px 0}
"""


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _store(engine) -> TemplateStore:
    return TemplateStore(engine.rt.data_dir, engine.rt.workspace)


def previews_root(engine) -> Path:
    return Path(engine.rt.data_dir) / "previews"


# ---------------------------------------------------------------- 表单 ↔ 模板
def _differs(base: Any, value: Any) -> bool:
    if isinstance(base, list):
        vals = value if isinstance(value, list) else [x.strip() for x in re.split(r"[,，、;；]", str(value)) if x.strip()]
        return vals != base
    if isinstance(base, (int, float)) and not isinstance(base, bool):
        try:
            return abs(float(value) - float(base)) > 1e-9
        except (TypeError, ValueError):
            return True
    if isinstance(base, str) and re.fullmatch(r"[0-9A-Fa-f]{6}", base):
        return str(value).strip().lstrip("#").upper() != base.upper()
    return str(value).strip() != str(base)


def template_from_form(body: dict[str, Any]) -> Template:
    """表单内容 → 模板（只保留与基础配置档不同的设置；服务端完整校验，不合规时抛出 TemplateError）。"""
    name = str(body.get("name") or "").strip()
    settings = body.get("settings") or {}
    unit = body.get("unit") or {}
    if not isinstance(settings, dict) or not isinstance(unit, dict):
        raise ValueError("settings 与 unit 须为 JSON 对象")
    base_id = str(body.get("base") or "gbt9704-2012")
    if base_id not in BASE_PROFILES:
        raise ValueError(f"基础配置档应为 {'、'.join(BASE_PROFILES)}")
    from ..layout.profile import _raw

    base = _raw(base_id)
    data: dict[str, Any] = {"name": name, "base": base_id, "description": str(body.get("description") or "").strip()}
    for path, value in settings.items():
        if not isinstance(path, str) or value is None or (isinstance(value, str) and not value.strip()):
            continue
        b = _get(base, path)
        if b is not None and not _differs(b, value):
            continue
        cur = data
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
            if not isinstance(cur, dict):
                raise ValueError(f"未知设置项：{path}")
        cur[parts[-1]] = value
    u = {k: v for k, v in unit.items() if isinstance(k, str) and v not in (None, "", [])}
    if u:
        data["unit"] = u
    return Template.from_dict(data, name=name or None)


# ---------------------------------------------------------------- 页面
def templates_page(engine, token: str, query: str = "") -> str:
    store = _store(engine)
    q = parse_qs(query or "")
    sel = (q.get("name") or [""])[0]
    rows = store.list()
    current: Template | None = None
    load_err = ""
    if sel:
        try:
            current = store.get(sel)
        except (KeyError, ValueError) as exc:
            load_err = str(exc)
    eff = current.effective_data() if current else Template(name="new").effective_data()
    from ..layout.profile import _raw

    sizes = list(_raw("gbt9704-2012")["sizes_pt"])
    links = "".join(
        f'<a href="/templates?name={_esc(n)}">{_esc(n)}</a><span class="meta">（{_esc(t.kind) if t else "无法加载"}{"，偏离国标 " + str(len(t.deviations())) + " 项" if t else ""}）</span>'
        for n, t, _ in rows
    )

    def sel_html(path: str, options: list[tuple[str, str]], value: Any) -> str:
        return f'<select data-path="{_esc(path)}">' + "".join(f'<option value="{_esc(k)}"{" selected" if str(k) == str(value) else ""}>{_esc(lbl)}</option>' for k, lbl in options) + "</select>"

    fs_fonts = "".join(
        f'<label>{_esc(ROLE_LABELS[r])}字库名<input data-path="fonts.{r}.name" value="{_esc(eff["fonts"][r]["name"])}"></label>'
        f'<label>{_esc(ROLE_LABELS[r])}备选名<input data-path="fonts.{r}.alternates" value="{_esc("、".join(eff["fonts"][r].get("alternates") or []))}"></label>'
        for r in ROLES
    )
    fs_sizes = "".join(f"<label>{_esc(lbl)}{sel_html(p, [(s, s) for s in sizes], _get(eff, p))}</label>" for p, lbl in SIZE_FIELDS)
    fs_roles = "".join(f"<label>{_esc(lbl)}{sel_html(p, [(r, ROLE_LABELS[r]) for r in ROLES], _get(eff, p))}</label>" for p, lbl in ROLE_FIELDS)
    color = str(_get(eff, "elements.organ_mark.color") or "FF0000")
    fs_num = f'<label>发文机关标志颜色<input type="color" data-path="elements.organ_mark.color" value="#{_esc(color)}"></label>' + "".join(
        f'<label>{_esc(lbl)}<input type="number" step="0.1" data-path="{_esc(p)}" value="{_esc(_get(eff, p))}"></label>' for p, lbl in NUM_FIELDS
    )
    unit = current.unit if current else {}
    fs_unit = "".join(
        f'<label>{_esc(lbl)}<input data-unit="{_esc(k)}" value="{_esc("、".join(unit.get(k) or []) if k == "cc" else unit.get(k, ""))}"></label>' for k, lbl in UNIT_FIELDS
    )
    readonly = current is not None and current.kind != "用户"
    name_val = "" if (current is None or readonly) else current.name
    note = ""
    if readonly:
        note = f'<div class="meta">“{_esc(current.name)}”是{_esc(current.kind)}模板，不能直接修改：在下面填写新名称后保存，即另存为用户模板。</div>'
    devs = "".join(f"<li>{_esc(c.describe())}</li>" for c in (current.deviations() if current else []))
    payload = json.dumps({"token": token, "base": current.base if current else "gbt9704-2012"}, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>公文模板</title><style>{PAGE_CSS}</style></head><body>
<header><h1>公文模板</h1><a href="/">任务列表</a><span class="meta">模板只改版式与单位信息；偏离 GB/T 9704—2012 的设置会逐项列出。方正小标宋简体、仿宋_GB2312 等为授权字库，须自行安装（gongwen fonts install）。</span></header>
<main><div class="panel">
<div class="list">{links}<a href="/templates">＋新建</a></div>
{f'<div class="err">{_esc(load_err)}</div>' if load_err else ''}
<h3>{_esc(current.name if current else "新建模板")}</h3>{note}
<label>模板名称<input id="tpl-name" value="{_esc(name_val)}" placeholder="如 本单位（汉字、字母、数字、-、_）"></label>
<label>说明<input id="tpl-desc" value="{_esc(current.description if current and not readonly else "")}"></label>
<fieldset><legend>字体（字库名属实务；国标规定的是字体类别）</legend>{fs_fonts}</fieldset>
<fieldset><legend>字号</legend>{fs_sizes}</fieldset>
<fieldset><legend>字体类别</legend>{fs_roles}</fieldset>
<fieldset><legend>发文机关标志与页边距（下白边、切口随之推算，版心保持 156mm×225mm）</legend>{fs_num}</fieldset>
<fieldset><legend>单位信息（只补全文稿中空缺的要素）</legend>{fs_unit}</fieldset>
<button class="btn" id="btn-preview">预览</button><button class="btn primary" id="btn-save">保存</button>
<div id="msg" class="meta"></div>
{f'<h4>当前模板偏离国标</h4><ul>{devs}</ul>' if devs else ''}
</div>
<div class="panel"><h3>示例公文预览</h3><div class="meta">按左侧设置排版一份合成的示例通知，用 LibreOffice 渲染为页面图像（约需数秒）；未安装的字库以开源字体替代，仅供预览。</div><div id="result"></div></div></main>
<script type="application/json" id="gw-tpl">{payload}</script>
<script>
const C = JSON.parse(document.getElementById('gw-tpl').textContent);
const $ = s => document.querySelector(s);
function esc(t){{return String(t??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
function collect(){{
  const settings = {{}}; document.querySelectorAll('[data-path]').forEach(el=>{{ let v = el.value; if(el.dataset.path.endsWith('.alternates')) v = v.split(/[,，、;；]/).map(x=>x.trim()).filter(Boolean); settings[el.dataset.path] = v; }});
  const unit = {{}}; document.querySelectorAll('[data-unit]').forEach(el=>{{ let v = el.value.trim(); if(el.dataset.unit==='cc') v = v.split(/[,，、;；]/).map(x=>x.trim()).filter(Boolean); unit[el.dataset.unit] = v; }});
  return {{name: $('#tpl-name').value.trim(), description: $('#tpl-desc').value.trim(), base: C.base, settings, unit}};
}}
async function post(url, body){{ const r = await fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json','X-GW-Token':C.token}}, body: JSON.stringify(body)}}); let j = {{}}; try{{ j = await r.json(); }}catch(e){{}} return [r.ok, j]; }}
$('#btn-save').onclick = async ()=>{{ const [ok, j] = await post('/api/template', collect()); $('#msg').className = ok ? 'meta' : 'err'; $('#msg').textContent = j.message || (ok?'已保存':'保存失败'); if(ok && j.name) setTimeout(()=>location.href='/templates?name='+encodeURIComponent(j.name), 600); }};
$('#btn-preview').onclick = async ()=>{{
  $('#result').innerHTML = '<div class="meta">正在渲染……</div>';
  const [ok, j] = await post('/api/template/preview', collect());
  if(!ok){{ $('#result').innerHTML = '<div class="err">'+esc(j.message||'预览失败')+'</div>'; return; }}
  let h = '';
  if(j.deviations && j.deviations.length) h += '<h4>偏离 GB/T 9704—2012</h4><ul>' + j.deviations.map(d=>'<li>'+esc(d)+'</li>').join('') + '</ul>';
  h += j.pages.length ? '<div class="pages">' + j.pages.map((u,i)=>'<img alt="第 '+(i+1)+' 面" src="'+esc(u)+'">').join('') + '</div>' : '<div class="warn">'+esc(j.message||'未实际渲染')+'</div>' + (j.html||'');
  if(j.substitutions && j.substitutions.length) h += '<h4>字体替代（仅供预览）</h4><ul>' + j.substitutions.map(d=>'<li>'+esc(d)+'</li>').join('') + '</ul>';
  if(j.checks && j.checks.length) h += '<h4>核验（非通过项）</h4><ul>' + j.checks.map(c=>'<li><b>'+esc(c.status)+'</b> '+esc(c.item)+'：'+esc(c.actual)+'</li>').join('') + '</ul>';
  $('#result').innerHTML = h;
}};
</script></body></html>"""


def _img_url(pid: str) -> Any:
    return lambda i, p: f"/preview-img/{pid}/{p.name}"


def task_preview_page(engine, tid: str, token: str) -> tuple[int, str]:
    try:
        st = engine.load_state(tid)
    except KeyError:
        return 404, "任务不存在"
    report = engine.store.load_model(tid, "layout_report", LayoutReport)
    title = str(engine.current_ir(st).title if engine.current_ir(st) else tid)
    nav = f'<div style="padding:8px 16px;font:13px system-ui"><a href="/task/{_esc(tid)}">返回审阅工作台</a>　<a href="/">任务列表</a></div>'
    docx = next((Path(o.path) for o in (report.outputs if report else []) if o.kind == "docx"), None)
    if report is None or docx is None or not docx.is_file():
        return 200, f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>排版预览</title><style>{PAGE_CSS}</style></head><body>{nav}<div class="panel" style="margin:16px">任务尚未排版（排版检查阶段之后才有 DOCX），暂无预览。</div></body></html>'
    out = previews_root(engine) / tid
    manifest = out / "manifest.json"
    pages: list[Path] = []
    if manifest.is_file():
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            m = {}
        if m.get("docx_sha256") == file_digest(docx):
            pages = sorted((p for p in (out / "pages").glob("page-*.png") if PAGE_RE.match(p.name)), key=lambda p: int(PAGE_RE.match(p.name).group(1)))
            if pages or m.get("rendered") is False:
                page = build_html(title, report, pages, img_src=_img_url(tid), notes=list(m.get("notes") or []))
                return 200, page.replace("<body>", "<body>" + nav, 1)
    payload = json.dumps({"token": token, "task_id": tid}).replace("<", "\\u003c")
    return 200, f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>排版预览</title><style>{PAGE_CSS}</style></head><body>{nav}
<div class="panel" style="margin:16px" id="box">正在用 LibreOffice 渲染当前排版稿（约需数秒）……</div>
<script type="application/json" id="gw-pv">{payload}</script>
<script>
const P = JSON.parse(document.getElementById('gw-pv').textContent);
fetch('/api/preview', {{method:'POST', headers:{{'Content-Type':'application/json','X-GW-Token':P.token}}, body: JSON.stringify({{task_id:P.task_id}})}})
 .then(r=>r.json().then(j=>[r.ok,j])).then(([ok,j])=>{{ if(ok) location.reload(); else document.getElementById('box').textContent = j.message || '预览失败'; }});
</script></body></html>"""


def preview_image(engine, pid: str, name: str) -> bytes | None:
    """读取预览图像：编号与文件名须合规，且解析后的路径必须位于数据目录 previews/ 之下。"""
    if not PREVIEW_ID_RE.match(pid or "") or not PAGE_RE.match(name or ""):
        return None
    root = previews_root(engine).resolve()
    p = (root / pid / "pages" / name).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        return None
    return p.read_bytes()


# ---------------------------------------------------------------- 写操作
def _prune_template_previews(root: Path) -> None:
    dirs = sorted((d for d in root.glob("tpl-*") if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for d in dirs[:-MAX_TEMPLATE_PREVIEWS]:
        shutil.rmtree(d, ignore_errors=True)


def layout_post(engine, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """处理模板与预览的写请求，返回 (状态码, JSON)。调用方已校验来源、令牌与大小。"""
    try:
        if path == "/api/template":
            tpl = template_from_form(body)
            p = _store(engine).save(tpl)
            devs = [c.describe() for c in tpl.deviations()]
            return 200, {"message": f"已保存模板“{tpl.name}”（偏离国标 {len(devs)} 项）", "name": tpl.name, "path": str(p), "deviations": devs}
        if path == "/api/template/preview":
            body = {**body, "name": str(body.get("name") or "").strip() or "未命名"}
            tpl = template_from_form(body)
            key = hashlib.sha256(json.dumps(tpl.to_dict(), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
            pid = f"tpl-{key}"
            root = previews_root(engine)
            cfg = engine.rt.config.layout
            with _RENDER_LOCK:
                res = preview_ir(sample_ir(unit=tpl.unit), root / pid, tpl.profile(cfg.margin_mode), font_substitution=cfg.font_substitution, stem="sample", title=f"模板“{tpl.name}”示例")
                write_manifest(res)
                _prune_template_previews(root)
            rep = res.report
            return 200, {
                "pages": [f"/preview-img/{pid}/{p.name}" for p in res.pages],
                "rendered": res.rendered,
                "message": "" if res.rendered else "本机缺少 LibreOffice 或 poppler-utils：未生成页面图像（以下为 HTML 近似预览，不代表实际版面）",
                "deviations": [c.describe() for c in tpl.deviations()],
                "substitutions": rep.render.substitutions if rep else [],
                "checks": [{"status": c.status, "item": c.item, "actual": c.actual} for c in (rep.checks if rep else []) if c.status != "pass"],
                "html": "" if res.rendered else res.html.read_text(encoding="utf-8"),
            }
        if path == "/api/preview":
            tid = body.get("task_id")
            if not isinstance(tid, str):
                raise ValueError("task_id 须为文本")
            st = engine.load_state(tid)
            report = engine.store.load_model(tid, "layout_report", LayoutReport)
            docx = next((Path(o.path) for o in (report.outputs if report else []) if o.kind == "docx"), None)
            if report is None or docx is None or not docx.is_file():
                raise ValueError("任务尚未排版，暂无可预览的 DOCX")
            from ..layout.templates import resolve_profile

            cfg = engine.rt.config.layout
            profile = resolve_profile(st.options.get("layout_template") or cfg.template or None, data_dir=engine.rt.data_dir, workspace=engine.rt.workspace, profile_id=cfg.profile, margin_mode=cfg.margin_mode)
            with _RENDER_LOCK:
                res = preview_docx(docx, previews_root(engine) / tid, report, profile, ir=engine.current_ir(st), font_substitution=cfg.font_substitution)
                write_manifest(res, {"docx_sha256": file_digest(docx)})
            return 200, {"message": f"已生成预览（{len(res.pages)} 面）" if res.rendered else "未实际渲染：本机缺少 LibreOffice 或 poppler-utils", "pages": len(res.pages)}
    except TemplateError as exc:
        return 400, {"message": str(exc), "errors": exc.errors}
    except (KeyError, ValueError, TypeError, PermissionError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        return 400, {"message": f"处理失败：{msg}"}
    return 404, {"message": "not found"}


__all__ = ["layout_post", "preview_image", "task_preview_page", "template_from_form", "templates_page"]
