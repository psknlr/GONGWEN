"""公文字体：族名逐字核对、渲染预览的开源替代映射（真实字库优先、替代如实报告）、用户字库安装与族名核对。

需要 fontconfig；渲染相关的测试另需 LibreOffice 与 poppler，缺少时跳过。安装测试把 HOME 指向临时目录，不改动本机字体。
"""

import json
import shutil
from pathlib import Path

import pytest

from gongwen.cli.main import main as cli
from gongwen.layout import fonts
from gongwen.layout.docx_compiler import compile_docx
from gongwen.layout.profile import LayoutProfile
from gongwen.layout.render_check import check_rendering
from gongwen.layout.templates import Template, sample_ir

needs_fc = pytest.mark.skipif(not fonts.fc_available(), reason="需要 fontconfig（fc-list、fc-match）")
needs_render = pytest.mark.skipif(not (shutil.which("soffice") and shutil.which("pdftotext") and shutil.which("pdftoppm") and fonts.fc_available()), reason="需要 LibreOffice、poppler 与 fontconfig")


def _installed(name: str) -> bool:
    return fonts.norm(name) in fonts.installed_families()


def _font_file(name: str) -> Path | None:
    face = fonts.fc_match(name)
    if face is None or not face.has_family(name) or not face.file:
        return None
    return Path(face.file)


# ------------------------------------------------------------------ 映射配置（不需要渲染）
def test_fontconfig_map_appends_real_fonts_before_open_substitutes():
    xml = fonts.fontconfig_xml(LayoutProfile.load().data["fonts"], home=Path("/home/u"))
    assert '<include ignore_missing="yes">' in xml and "<dir>/home/u/.local/share/fonts</dir>" in xml
    block = xml.split("<family>方正小标宋简体</family>", 1)[1].split("</alias>", 1)[0]
    # <accept> 追加在所请求的字库名之后：已安装的真实字库（含同类备选名）优先，最后才是开源替代字体
    assert block.strip().startswith("<accept>") and block.index("方正小标宋_GBK") < block.index("Noto Serif CJK SC Black")
    fs = xml.split("<family>仿宋_GB2312</family>", 1)[1].split("</alias>", 1)[0]
    assert fs.index("<family>仿宋</family>") < fs.index("Noto Serif CJK SC Light")
    assert 'binding="strong"' in xml and "<prefer>" not in xml
    # 模板改了字库名：映射也覆盖新名称
    names = fonts.role_names(Template.from_dict({"name": "t", "fonts": {"fangsong": {"name": "方正仿宋_GBK"}}}).effective_data()["fonts"])
    assert names["fangsong"][0] == "方正仿宋_GBK"


def test_family_classification_and_role_guess():
    pf = LayoutProfile.load().data["fonts"]
    assert fonts.classify(["仿宋", "FangSong"], pf) == ([("fangsong", "仿宋", "备选"), ("fangsong", "FangSong", "备选")], None)
    assert fonts.classify(["仿宋_GB2312"], pf)[0] == [("fangsong", "仿宋_GB2312", "指定")]
    assert fonts.classify(["LXGW WenKai", "霞鹜文楷"], pf) == ([], "kaiti")
    assert fonts.classify(["DejaVu Sans"], pf) == ([], None)
    rep = fonts.FontFileReport(src=Path("simfang.ttf"), dest=None, families=["仿宋", "FangSong"], matches=[("fangsong", "仿宋", "备选")])
    assert "fonts.fangsong.name=仿宋" in rep.advice("本单位") and "备选字库名" in rep.advice("本单位")


@needs_fc
def test_check_uses_exact_family_names_not_fc_match_fallback():
    if _installed("方正小标宋简体"):
        pytest.skip("本机已安装方正小标宋简体")
    rc = next(r for r in fonts.check_roles(LayoutProfile.load().data["fonts"]) if r.role == "xiaobiaosong")
    assert not rc.exact and rc.alternate is None and not rc.satisfied  # fc-match 总会回退到某个字体，不能当作已安装
    assert rc.render is not None and not rc.render.has_family("方正小标宋简体")
    if _installed("Noto Serif CJK SC Black"):
        assert rc.substitute is not None and rc.substitute.family == "Noto Serif CJK SC Black"
        assert "开源替代字体，仅供预览" in rc.summary()


# ------------------------------------------------------------------ 渲染
@needs_render
def test_substitution_map_changes_embedded_fonts_and_is_reported_honestly(tmp_path):
    if not (_installed("Noto Serif CJK SC Black") and _installed("Noto Serif CJK SC Light")):
        pytest.skip("需要 fonts-noto-cjk 与 fonts-noto-cjk-extra")
    if _installed("方正小标宋简体") or _installed("仿宋_GB2312"):
        pytest.skip("本机已安装指定字库")
    ir = sample_ir()
    profile = LayoutProfile.load()
    res = {}
    for mode in (False, True):
        out = tmp_path / str(mode)
        path, used = compile_docx(ir, out / "a.docx", profile)
        info, checks = check_rendering(path, ir, profile, used, out, font_substitution=mode)
        res[mode] = (info, {c.rule_id: c for c in checks})
    off, on = res[False][0], res[True][0]
    assert "NotoSerifCJKsc-Black" not in off.fonts_embedded
    assert {"NotoSerifCJKsc-Black", "NotoSerifCJKsc-Light"} <= set(on.fonts_embedded)
    xbs = next(s for s in on.substitutions if s.startswith("方正小标宋简体："))
    assert "已替代：Noto Serif CJK SC Black（开源替代字体，仅供预览；定稿须安装方正小标宋简体）" in xbs
    fs = next(s for s in on.substitutions if s.startswith("仿宋_GB2312："))
    assert "Noto Serif CJK SC Light" in fs and "字形不是仿宋" in fs
    assert any("存在字体替代" in n for n in on.notes)
    # 版面按字形校准，与渲染所用字体无关：换用替代字体后标志位置不变
    m_off, m_on = (float(r[1]["LAY-MARK"].actual.split("mm")[0]) for r in (res[False], res[True]))
    assert abs(m_off - 35) <= 0.6 and abs(m_on - 35) <= 0.6 and abs(m_on - m_off) <= 0.6
    assert "字形上缘实测" in res[True][1]["LAY-MARK"].actual


@needs_render
def test_installed_real_font_wins_over_substitutes(tmp_path):
    """模板指定的字库已安装时，渲染直接使用它，不被替代映射改换，也不报告替代。"""
    if not _installed("LXGW WenKai"):
        pytest.skip("需要霞鹜文楷（fonts-lxgw-wenkai）")
    tpl = Template.from_dict({"name": "t", "fonts": {"fangsong": {"name": "LXGW WenKai"}}})
    profile = tpl.profile()
    assert "LXGW WenKai" in fonts.role_names(profile.data["fonts"])["fangsong"]  # 映射中有该名的别名条目
    ir = sample_ir()
    path, used = compile_docx(ir, tmp_path / "a.docx", profile)
    info, _ = check_rendering(path, ir, profile, used, tmp_path)
    assert "LXGWWenKai-Regular" in info.fonts_embedded and "NotoSerifCJKsc-Light" not in info.fonts_embedded
    assert not any(s.startswith("LXGW WenKai：") for s in info.substitutions)


# ------------------------------------------------------------------ 安装用户提供的字库
@needs_fc
def test_install_copied_font_reads_family_and_warns_on_mismatch(tmp_path, monkeypatch, capsys):
    src = _font_file("LXGW WenKai")
    if src is None:
        pytest.skip("需要霞鹜文楷（fonts-lxgw-wenkai）作为测试字库")
    given = tmp_path / "from"
    given.mkdir()
    shutil.copyfile(src, given / "simkai.ttf")  # 文件名像 Windows 楷体，族名却是 LXGW WenKai
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "GONGWEN_HOME"):
        monkeypatch.delenv(var, raising=False)
    ws = ["-C", str(tmp_path / "ws")]
    assert cli([*ws, "fonts", "install", "--from", str(given), "--json"]) == 0
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    dest = home / ".local" / "share" / "fonts" / "gongwen"
    assert d["dest"] == str(dest) and (dest / "simkai.ttf").is_file() and d["fc_cache"]["ok"]
    f = d["files"][0]
    assert "LXGW WenKai" in f["families"] and f["matches"] == [] and f["guessed_role"] == "kaiti"
    assert f["advice"].startswith("警告：") and "fonts.kaiti.name=LXGW WenKai" in f["advice"]
    # 安装后 fontconfig 能在用户字体目录中找到该文件（族名逐字核对：楷体_GB2312 仍未安装）
    assert any(face.file.startswith(str(dest)) for face in fonts.list_faces())
    kaiti = next(r for r in d["roles"] if r["role"] == "kaiti")
    assert not kaiti["exact"]
    # 再装一次：内容相同不重复复制；同名不同内容须 --force
    assert cli([*ws, "fonts", "install", "--from", str(given / "simkai.ttf"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["files"][0]["copied"] is False
    (given / "simkai.ttf").write_bytes((given / "simkai.ttf").read_bytes()[:-1] + b"\0")
    assert cli([*ws, "fonts", "install", "--from", str(given / "simkai.ttf")]) == 2
    assert "同名但内容不同" in capsys.readouterr().err


def test_install_rejects_non_font_files_and_missing_source(tmp_path, capsys):
    (tmp_path / "说明.txt").write_text("x", encoding="utf-8")
    ws = ["-C", str(tmp_path)]
    assert cli([*ws, "fonts", "install", "--from", str(tmp_path / "说明.txt")]) == 2
    assert "不是字库文件" in capsys.readouterr().err
    assert cli([*ws, "fonts", "install"]) == 2
    assert "--from" in capsys.readouterr().err
    assert cli([*ws, "fonts", "install", "--from", str(tmp_path / "x.ttf"), "--open"]) == 2


def test_open_install_prints_command_without_root(monkeypatch, capsys):
    monkeypatch.setattr(fonts, "open_install_plan", lambda: {"apt": True, "missing": ["fonts-lxgw-wenkai"], "root": False, "command": ["sudo", "apt-get", "install", "-y", "fonts-lxgw-wenkai"], "update_command": ["sudo", "apt-get", "update"]})
    assert cli(["fonts", "install", "--open"]) == 1
    out = capsys.readouterr().out
    assert "sudo apt-get install -y fonts-lxgw-wenkai" in out and "不是方正小标宋" in out
