"""渲染预览（gongwen preview）与本地工作台的模板、预览页面。

* preview：排版 → LibreOffice 渲染 → 页面图像 → 自包含的 preview.html；缺少 LibreOffice 时退回 HTML 近似预览并写明；
* 工作台：/templates 编辑表单、POST /api/template 服务端校验（无令牌 403、不合规 400）、示例公文预览、
  任务预览页；预览图像只从数据目录 previews/ 下读取，拒绝路径穿越。

渲染相关的测试需要 LibreOffice 与 poppler，缺少时跳过。
"""

import json
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest
from test_engine_e2e import make_engine, run_to_review, start

from gongwen.cli.main import main as cli
from gongwen.layout.templates import TemplateStore
from gongwen.orchestrator import default_user
from gongwen.workbench.server import serve

needs_render = pytest.mark.skipif(not (shutil.which("soffice") and shutil.which("pdftotext") and shutil.which("pdftoppm")), reason="需要 LibreOffice 与 poppler 做实际渲染")
DRAFT = "关于加强基层医疗示范点建设的通知\n各区卫生健康局：\n为提升基层医疗卫生服务能力，现就有关事项通知如下。\n一、工作目标\n全市新建示范点12个。\n示例市卫生健康委员会\n2026年3月1日\n"
PNG = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------ 命令行预览
@needs_render
def test_preview_file_writes_pages_and_self_contained_html(tmp_path, capsys):
    src = tmp_path / "稿.txt"
    src.write_text(DRAFT, encoding="utf-8")
    assert cli(["-C", str(tmp_path), "preview", str(src), "-o", str(tmp_path / "pv"), "--template", "windows-fonts", "--json"]) == 0
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert d["rendered"] and d["pages"] and d["template"] == "windows-fonts"
    page1 = tmp_path / "pv" / "pages" / "page-1.png"
    assert page1.read_bytes().startswith(PNG)
    html = (tmp_path / "pv" / "preview.html").read_text(encoding="utf-8")
    assert html.count("data:image/png;base64,") == len(d["pages"])  # 图像内嵌，单个文件即可打开
    assert "核验结果" in html and "字体替代" in html and "第 1 面" in html
    assert (tmp_path / "pv" / "manifest.json").is_file() and (tmp_path / "pv" / "稿.预览.docx").is_file()
    assert src.read_text(encoding="utf-8") == DRAFT


def test_preview_falls_back_to_html_without_libreoffice(tmp_path, monkeypatch, capsys):
    import gongwen.layout.preview as pv
    import gongwen.layout.render_check as rc

    none = {k: None for k in ("soffice", "pdfinfo", "pdffonts", "pdftotext", "pdftoppm", "fc-list")}
    monkeypatch.setattr(rc, "tools_available", lambda: none)
    monkeypatch.setattr(pv, "tools_available", lambda: none)
    src = tmp_path / "稿.txt"
    src.write_text(DRAFT, encoding="utf-8")
    assert cli(["-C", str(tmp_path), "preview", str(src), "-o", str(tmp_path / "pv")]) == 0
    out = capsys.readouterr().out
    assert "未实际渲染" in out and "HTML 近似预览" in out
    html = (tmp_path / "pv" / "preview.html").read_text(encoding="utf-8")
    assert "未实际渲染" in html and "gw-doc" in html and "<img" not in html
    assert not (tmp_path / "pv" / "pages").exists() or not any((tmp_path / "pv" / "pages").iterdir())


@needs_render
def test_preview_task_renders_current_docx(tmp_path, capsys):
    eng = make_engine(tmp_path)
    user = default_user()
    st = run_to_review(eng, user, start(eng, user).task_id)
    assert cli(["-C", str(tmp_path), "preview", st.task_id, "--json"]) == 0
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert d["rendered"] and d["pages"][0].endswith("page-1.png")
    assert str(eng.rt.data_dir / "previews" / st.task_id) in d["html"]
    assert cli(["-C", str(tmp_path), "preview", "T-不存在"]) == 2


# ------------------------------------------------------------------ 工作台
@pytest.fixture()
def server(tmp_path):
    eng = make_engine(tmp_path)
    user = default_user()
    httpd, token = serve(eng, user, port=0)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    yield eng, user, f"http://127.0.0.1:{httpd.server_address[1]}", token
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _post(url, body, token=None):
    headers = {"Content-Type": "application/json", **({"X-GW-Token": token} if token else {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


FORM = {"name": "本单位", "description": "示例", "settings": {"elements.title.size": "小二", "elements.organ_mark.color": "#C00000", "fonts.fangsong.name": "仿宋", "margins.top_mm": "37"}, "unit": {"organ_mark": "示例市卫生健康委员会文件", "cc": ["示例市财政局"]}}


def test_template_routes_validate_on_server_and_require_token(server):
    eng, _, base, token = server
    code, ctype, page = _get(base + "/templates")
    page = page.decode("utf-8")
    assert code == 200 and "公文模板" in page and "windows-fonts" in page and token in page
    assert _post(base + "/api/template", FORM)[0] == 403  # 无令牌
    assert _post(base + "/api/template", FORM, "guess")[0] == 403
    code, res = _post(base + "/api/template", {**FORM, "settings": {"elements.title.size": "小五"}}, token)
    assert code == 400 and "应为字号名之一" in res["message"]
    code, res = _post(base + "/api/template", {**FORM, "name": "../x"}, token)
    assert code == 400 and "模板名" in res["message"]
    code, res = _post(base + "/api/template", {**FORM, "name": "windows-fonts"}, token)
    assert code == 400 and "内置模板" in res["message"]
    code, res = _post(base + "/api/template", {**FORM, "settings": {"elements.titel.size": "小二"}}, token)
    assert code == 400 and "未知设置项" in res["message"]
    code, res = _post(base + "/api/template", FORM, token)
    assert code == 200, res
    tpl = TemplateStore(eng.rt.data_dir, eng.rt.workspace).get("本单位")
    # 只保存与国标默认不同的设置（天头 37mm 与默认相同，不写入）
    assert tpl.overrides == {"fonts": {"fangsong": {"name": "仿宋"}}, "elements": {"title": {"size": "小二"}, "organ_mark": {"color": "C00000"}}}
    assert tpl.unit == {"organ_mark": "示例市卫生健康委员会文件", "cc": ["示例市财政局"]}
    assert len(res["deviations"]) == 2
    code, _, page = _get(base + "/templates?name=" + urllib.parse.quote("本单位"))
    assert code == 200 and "标题字号：二号 → 小二" in page.decode("utf-8")


def test_preview_images_are_served_only_from_preview_dirs(server):
    eng, _, base, _ = server
    secret = eng.rt.data_dir / "secret.png"
    secret.write_bytes(PNG + b"secret")
    (eng.rt.data_dir / "previews" / "tpl-abc" / "pages").mkdir(parents=True)
    (eng.rt.data_dir / "previews" / "tpl-abc" / "pages" / "page-1.png").write_bytes(PNG + b"ok")
    code, ctype, data = _get(base + "/preview-img/tpl-abc/page-1.png")
    assert code == 200 and ctype == "image/png" and data == PNG + b"ok"
    for path in ("/preview-img/..%2F..%2Fsecret.png/page-1.png", "/preview-img/tpl-abc/..%2Fpage-1.png", "/preview-img/tpl-abc/secret.png", "/preview-img/../page-1.png", "/preview-img/tpl-abc/page-1.png%00", "/preview-img/tpl-x/page-2.png"):
        assert _get(base + path)[0] == 404, path
    assert _get(base + "/preview/T-missing")[0] == 404 and _get(base + "/preview/..")[0] == 404


@needs_render
def test_template_preview_and_task_preview_pages(server):
    eng, user, base, token = server
    code, res = _post(base + "/api/template/preview", {**FORM, "name": ""}, token)
    assert code == 200 and res["rendered"] and res["pages"], res
    assert any("标题字号" in d for d in res["deviations"])
    code, ctype, data = _get(base + res["pages"][0])
    assert code == 200 and data.startswith(PNG)
    assert _post(base + "/api/template/preview", FORM)[0] == 403
    st = run_to_review(eng, user, start(eng, user).task_id)
    code, _, page = _get(f"{base}/preview/{st.task_id}")
    assert code == 200 and "/api/preview" in page.decode("utf-8")  # 尚未生成：页面发起生成请求
    assert _post(base + "/api/preview", {"task_id": st.task_id})[0] == 403
    code, res = _post(base + "/api/preview", {"task_id": st.task_id}, token)
    assert code == 200 and res["pages"] >= 1, res
    code, _, page = _get(f"{base}/preview/{st.task_id}")
    page = page.decode("utf-8")
    assert code == 200 and f'src="/preview-img/{st.task_id}/page-1.png"' in page and "核验结果" in page
    assert _post(base + "/api/preview", {"task_id": "../x"}, token)[0] == 400
