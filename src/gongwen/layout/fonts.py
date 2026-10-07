"""公文字体：检查是否安装、安装用户提供的字库文件、渲染预览时的开源替代映射。

* GB/T 9704—2012 规定的是字体类别（仿宋体、楷体、黑体、小标宋体、宋体）；方正小标宋简体、仿宋_GB2312、
  楷体_GB2312 等字库名属实务，且都是授权字库（方正字库、中易），本系统不下载、不附带。用户须使用本单位合法
  取得的字库文件，用 ``gongwen fonts install --from <目录或文件>`` 安装；
* 检查按 fontconfig 的字体族名逐字核对（规范化空白与大小写），不把 fc-match 的回退结果当作“已安装”；
* 指定字库及其备选名都未安装时，只在 LibreOffice 渲染子进程中（环境变量 FONTCONFIG_FILE）把字库名映射到最接近的
  开源字体，不改变 DOCX 中的字体设置，也不改动系统字体配置。映射用 ``<accept>`` 追加在所请求的字库名之后，
  已安装的真实字库总是优先；渲染核验如实报告“已替代：……（开源替代字体，仅供预览）”。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from xml.sax.saxutils import escape

ROLES = ("xiaobiaosong", "fangsong", "kaiti", "heiti", "songti")
ROLE_LABELS = {"xiaobiaosong": "小标宋体", "fangsong": "仿宋体", "kaiti": "楷体", "heiti": "黑体", "songti": "宋体"}

# 各类字体常见的真实字库名（中易/Windows、方正、华文/macOS）。渲染映射时排在开源替代字体之前：装了哪个就用哪个
KNOWN_NAMES: dict[str, list[str]] = {
    "xiaobiaosong": ["方正小标宋简体", "方正小标宋_GBK", "方正小标宋", "FZXiaoBiaoSong-B05S", "FZXiaoBiaoSong-B05", "华文中宋", "STZhongsong"],
    "fangsong": ["仿宋_GB2312", "仿宋", "FangSong", "FangSong_GB2312", "方正仿宋_GBK", "方正仿宋简体", "FZFangSong-Z02S", "华文仿宋", "STFangsong"],
    "kaiti": ["楷体_GB2312", "楷体", "KaiTi", "KaiTi_GB2312", "方正楷体_GBK", "方正楷体简体", "FZKai-Z03S", "华文楷体", "STKaiti"],
    "heiti": ["黑体", "SimHei", "方正黑体_GBK", "方正黑体简体", "FZHei-B01S", "华文黑体", "STHeiti"],
    "songti": ["宋体", "SimSun", "新宋体", "NSimSun", "方正书宋_GBK", "方正书宋简体", "FZShuSong-Z01S", "华文宋体", "STSong"],
}


@dataclass(frozen=True)
class Substitute:
    family: str
    note: str


# 开源替代字体（按优先级）。只用于渲染预览，不写入 DOCX。
# 小标宋取 Noto Serif CJK SC Black（即思源宋体 Heavy；Noto 的字重名为 Black，与 Source Han 的 Heavy 是同一字重）：
# 小标宋是笔画粗重的标题宋体，横画明显粗于书宋，2 号标题的黑度在思源宋体各字重中以 Black 最接近；
# Bold 的黑度接近“中宋”，作为次选。
OPEN_SUBSTITUTES: dict[str, list[Substitute]] = {
    "xiaobiaosong": [
        Substitute("Noto Serif CJK SC Black", "思源宋体最粗字重，笔画粗重，标题字号下黑度最接近小标宋"),
        Substitute("Source Han Serif SC Heavy", "思源宋体 Heavy（与 Noto Serif CJK Black 同一字重）"),
        Substitute("Noto Serif CJK SC Bold", "思源宋体 Bold，黑度接近中宋"),
        Substitute("Noto Serif CJK SC", "思源宋体常规字重"),
    ],
    "songti": [
        Substitute("Noto Serif CJK SC", "思源宋体常规字重"),
        Substitute("Source Han Serif SC", "思源宋体"),
        Substitute("AR PL UMing CN", "文鼎明体"),
    ],
    "heiti": [
        Substitute("Noto Sans CJK SC", "思源黑体常规字重"),
        Substitute("Source Han Sans SC", "思源黑体"),
        Substitute("WenQuanYi Zen Hei", "文泉驿正黑"),
    ],
    "kaiti": [
        Substitute("LXGW WenKai", "霞鹜文楷"),
        Substitute("AR PL UKai CN", "文鼎楷体"),
        Substitute("Noto Serif CJK SC", "本机没有开源楷体，以宋体近似，字形不是楷体"),
    ],
    "fangsong": [
        Substitute("Zhuque Fangsong (technical preview)", "朱雀仿宋（开源仿宋体，须另行安装）"),
        Substitute("Noto Serif CJK SC Light", "本机没有开源仿宋体，以细宋体近似，字形不是仿宋"),
        Substitute("Noto Serif CJK SC", "本机没有开源仿宋体，以宋体近似，字形不是仿宋"),
    ],
}

# Debian / Ubuntu 软件源中的开源中文字体包（只作渲染预览的替代字体）
OPEN_PACKAGES = ["fonts-noto-cjk", "fonts-noto-cjk-extra", "fonts-lxgw-wenkai", "fonts-arphic-ukai"]

LICENSE_NOTE = (
    "方正小标宋简体、仿宋_GB2312、楷体_GB2312 等为授权字库（方正字库、中易），本系统不下载、不附带；"
    "请使用本单位合法取得的字库文件（如单位购买的方正字库、Windows 自带的仿宋/楷体/黑体/宋体）。"
    "开源替代字体只用于预览，定稿须在安装指定字库的环境中复核。"
)

FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc"}


def norm(name: str) -> str:
    """字体族名比较口径：去掉空白、不分大小写（与 fontconfig 的族名比较一致）。"""
    return re.sub(r"\s+", "", name or "").casefold()


# ---------------------------------------------------------------- fontconfig 查询
@dataclass
class FontFace:
    families: list[str]
    style: str = ""
    fullname: str = ""
    postscript: str = ""
    file: str = ""
    index: int = 0

    @property
    def display(self) -> str:
        """便于阅读的名称：全名（如 Noto Serif CJK SC Black），没有时取首个族名。"""
        return self.fullname or (self.families[0] if self.families else "")

    def has_family(self, name: str) -> bool:
        n = norm(name)
        return any(norm(f) == n for f in self.families) or norm(self.fullname) == n


_FMT = "%{family}\t%{style}\t%{fullname}\t%{postscriptname}\t%{file}\t%{index}"


def _split_list(value: str) -> list[str]:
    """fontconfig 多值字段以逗号分隔（值内的逗号转义为 \\,）。"""
    parts = re.split(r"(?<!\\),", value)
    return [p.replace("\\,", ",").strip() for p in parts if p.strip()]


def _face(line: str) -> FontFace | None:
    cols = line.split("\t")
    if len(cols) < 6 or not cols[0]:
        return None
    fams = _split_list(cols[0])
    full = _split_list(cols[2])
    try:
        index = int(cols[5] or 0)
    except ValueError:
        index = 0
    return FontFace(families=fams, style=_split_list(cols[1])[0] if cols[1] else "", fullname=full[0] if full else "", postscript=cols[3], file=cols[4], index=index)


def fc_available() -> bool:
    return bool(shutil.which("fc-list") and shutil.which("fc-match"))


def list_faces(env: dict[str, str] | None = None) -> list[FontFace]:
    exe = shutil.which("fc-list")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--format", _FMT + "\n"], capture_output=True, text=True, timeout=120, env=env).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [f for f in (_face(line) for line in out.splitlines()) if f]


def installed_families(env: dict[str, str] | None = None) -> dict[str, str]:
    """已安装的字体族名（含全名）：规范化名 → 原名。"""
    out: dict[str, str] = {}
    for face in list_faces(env):
        for name in [*face.families, face.fullname]:
            if name:
                out.setdefault(norm(name), name)
    return out


def _pattern(name: str) -> str:
    # fontconfig 模式语法中 \ - : , 有特殊含义，须转义
    return re.sub(r"([\\\-:,])", r"\\\1", name)


def fc_match(name: str, env: dict[str, str] | None = None, lang: str = "zh-cn") -> FontFace | None:
    exe = shutil.which("fc-match")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--format", _FMT, f"{_pattern(name)}:lang={lang}"], capture_output=True, text=True, timeout=60, env=env).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    return _face(out.strip("\n"))


# ---------------------------------------------------------------- 字库名与替代映射
def role_names(profile_fonts: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """各类字体的真实字库名清单：配置档/模板指定的字库名在前，其后是备选名与常见字库名（去重保序）。"""
    out: dict[str, list[str]] = {}
    for role in ROLES:
        spec = (profile_fonts or {}).get(role) or {}
        names = [spec.get("name", ""), *(spec.get("alternates") or []), *KNOWN_NAMES[role]]
        unique: dict[str, str] = {}
        for n in names:
            if n:
                unique.setdefault(norm(n), n)
        out[role] = list(unique.values())
    # 模板中其他（非标准五类）字体键：只取指定名
    for key, spec in (profile_fonts or {}).items():
        if key not in out and isinstance(spec, dict) and spec.get("name"):
            out[key] = [spec["name"], *(spec.get("alternates") or [])]
    return out


def role_of(name: str, profile_fonts: dict[str, Any] | None = None) -> str | None:
    n = norm(name)
    for role, names in role_names(profile_fonts).items():
        if any(norm(x) == n for x in names):
            return role
    return None


def _user_font_dirs(home: Path) -> list[Path]:
    dirs = [home / ".local" / "share" / "fonts", home / ".fonts"]
    if os.environ.get("XDG_DATA_HOME"):
        dirs.insert(0, Path(os.environ["XDG_DATA_HOME"]) / "fonts")
    return dirs


def fontconfig_xml(profile_fonts: dict[str, Any] | None = None, home: Path | None = None, base_conf: str | None = None) -> str:
    """只供 LibreOffice 渲染子进程使用的 fontconfig 配置：包含系统配置，并为每个公文字库名追加同类真实字库与开源替代字体。

    渲染子进程的 HOME 指向临时的 LibreOffice 配置目录，用户字体目录（~/.local/share/fonts 等）因此要按真实 HOME 显式列出。"""
    home = home or Path.home()
    base = base_conf or os.environ.get("FONTCONFIG_FILE") or "/etc/fonts/fonts.conf"
    lines = [
        '<?xml version="1.0"?>',
        '<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">',
        "<fontconfig>",
        "  <!-- 由 gongwen 生成：只用于 LibreOffice 渲染预览，不改变系统字体配置；公文字库未安装时以开源字体替代，已安装的真实字库优先 -->",
        f'  <include ignore_missing="yes">{escape(base)}</include>',
    ]
    for d in _user_font_dirs(home):
        lines.append(f"  <dir>{escape(str(d))}</dir>")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "fontconfig"
    lines.append(f"  <cachedir>{escape(str(cache))}</cachedir>")
    for role, names in role_names(profile_fonts).items():
        subs = [s.family for s in OPEN_SUBSTITUTES.get(role, [])]
        for n in names:
            accept = [x for x in names if x != n] + subs
            lines.append('  <alias binding="strong">')
            lines.append(f"    <family>{escape(n)}</family>")
            lines.append("    <accept>" + "".join(f"<family>{escape(x)}</family>" for x in accept) + "</accept>")
            lines.append("  </alias>")
    lines.append("</fontconfig>")
    return "\n".join(lines) + "\n"


@contextmanager
def render_env(profile_fonts: dict[str, Any] | None, enabled: bool = True) -> Iterator[dict[str, str]]:
    """渲染子进程使用的环境变量（FONTCONFIG_FILE 指向临时生成的映射配置）；关闭映射时为空。"""
    if not enabled:
        yield {}
        return
    d = Path(tempfile.mkdtemp(prefix="gw-fc-"))
    try:
        conf = d / "fonts.conf"
        conf.write_text(fontconfig_xml(profile_fonts), encoding="utf-8")
        yield {"FONTCONFIG_FILE": str(conf)}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _env(extra: dict[str, str] | None) -> dict[str, str] | None:
    return {**os.environ, **extra} if extra else None


def env_fingerprint(profile_fonts: dict[str, Any] | None, enabled: bool = True) -> str:
    """字体环境指纹：各类字库名在渲染时实际匹配到的字体文件（含替代映射）。

    已渲染的 PDF 只在指纹相同时复用：之后安装了授权字库、开关了替代映射或换了模板字库名，都须重新渲染。"""
    import hashlib

    fonts_ = profile_fonts or {}
    parts = [f"map={int(enabled)}"]
    with render_env(fonts_, enabled=enabled) as extra:
        env = _env(extra)
        for role in ROLES:
            name = (fonts_.get(role) or {}).get("name", "")
            face = fc_match(name, env) if name else None
            parts.append(f"{role}:{name}->{face.file if face else '-'}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def open_substitute(face: FontFace | None, role: str | None) -> Substitute | None:
    if face is None:
        return None
    roles = [role] if role else list(OPEN_SUBSTITUTES)
    for r in roles:
        for s in OPEN_SUBSTITUTES.get(r or "", []):
            if face.has_family(s.family):
                return s
    return None


def describe_render(requested: str, profile_fonts: dict[str, Any] | None, installed: dict[str, str], env_extra: dict[str, str] | None, embedded: list[str]) -> str | None:
    """渲染时某字库的替代情况（文字以字库名开头）；指定字库已安装时返回 None。"""
    if norm(requested) in installed:
        return None
    role = role_of(requested, profile_fonts)
    face = fc_match(requested, _env(env_extra))
    shown = face.display if face else ""
    in_pdf = bool(face and face.postscript and any(e == face.postscript for e in embedded))
    pdf_note = f"PDF 实际嵌入：{'、'.join(embedded) or '未知'}"
    if face is None:
        return f"{requested}：本机未安装，渲染时被替代（{pdf_note}）"
    if not in_pdf:
        # fontconfig 的预测与 LibreOffice 实际选用不一致（如按缺字回退）：以 PDF 实际嵌入为准
        return f"{requested}：本机未安装，渲染时被替代（预期 {shown}，但 PDF 中未见该字体；{pdf_note}）"
    real = [x for x in role_names(profile_fonts).get(role or "", []) if norm(x) != norm(requested)]
    if any(face.has_family(x) for x in real):
        return f"{requested}：本机未安装，已用同类字库“{shown}”渲染（备选字库名，与指定字库可能有字形差异；定稿须核对）"
    sub = open_substitute(face, role)
    if sub is not None:
        extra = f"；{sub.note}" if role == "fangsong" else ""
        return f"{requested}：本机未安装。已替代：{shown}（开源替代字体，仅供预览{extra}；定稿须安装{requested}）"
    return f"{requested}：本机未安装，渲染时被替代为 {shown}（系统默认回退字体，仅供预览；定稿须安装{requested}）"


# ---------------------------------------------------------------- 各类字体的安装情况
@dataclass
class RoleCheck:
    role: str
    category: str
    name: str
    alternates: list[str]
    exact: bool = False
    alternate: str | None = None
    render: FontFace | None = None
    substitute: Substitute | None = None

    @property
    def satisfied(self) -> bool:
        return self.exact or self.alternate is not None

    def summary(self) -> str:
        if self.exact:
            return f"已安装指定字库 {self.name}"
        if self.alternate:
            return f"未安装 {self.name}，已安装同类字库 {self.alternate}（渲染时使用；DOCX 中仍指定 {self.name}）"
        r = self.render.display if self.render else "未知"
        if self.substitute is not None:
            return f"未安装 {self.name} 及备选字库；渲染替代：{r}（开源替代字体，仅供预览；{self.substitute.note}）"
        return f"未安装 {self.name} 及备选字库；渲染时回退为 {r}（系统默认字体，仅供预览）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "category": self.category,
            "name": self.name,
            "alternates": self.alternates,
            "exact": self.exact,
            "alternate": self.alternate,
            "satisfied": self.satisfied,
            "render_font": self.render.display if self.render else None,
            "render_file": self.render.file if self.render else None,
            "open_substitute": self.substitute.family if self.substitute else None,
            "summary": self.summary(),
        }


def check_roles(profile_fonts: dict[str, Any], mapping: bool = True) -> list[RoleCheck]:
    """逐类核对：指定字库名、备选名是否已安装（族名逐字核对），以及渲染时实际会用哪个字体。"""
    installed = installed_families()
    out = []
    with render_env(profile_fonts, enabled=mapping) as extra:
        env = _env(extra)
        for role in ROLES:
            spec = profile_fonts.get(role) or {}
            name = spec.get("name", "")
            alts = list(spec.get("alternates") or [])
            rc = RoleCheck(role=role, category=spec.get("category", ROLE_LABELS[role]), name=name, alternates=alts)
            rc.exact = norm(name) in installed
            if not rc.exact:
                rc.alternate = next((installed[norm(a)] for a in alts if norm(a) in installed), None)
            rc.render = fc_match(name, env)
            if not rc.satisfied:
                rc.substitute = open_substitute(rc.render, role)
            out.append(rc)
    return out


# ---------------------------------------------------------------- 字库文件：族名与安装
def font_files(paths: list[str | Path]) -> list[Path]:
    """展开目录（递归）与文件，只保留字库文件（.ttf .otf .ttc .otc）。"""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() in FONT_EXTS)
        elif p.is_file():
            if p.suffix.lower() not in FONT_EXTS:
                raise ValueError(f"不是字库文件（应为 .ttf/.otf/.ttc/.otc）：{p}")
            out.append(p)
        else:
            raise FileNotFoundError(f"字库文件或目录不存在：{p}")
    seen: set[Path] = set()
    return [p for p in out if not (p.resolve() in seen or seen.add(p.resolve()))]


def file_families(path: Path) -> list[list[str]]:
    """读取字库文件中每个字形的族名（含中文名）。优先用 fontTools（如已安装），否则用 fc-scan。"""
    try:
        from fontTools.ttLib import TTCollection, TTFont  # type: ignore[import-not-found]
    except ImportError:
        TTFont = None  # noqa: N806
    if TTFont is not None:
        try:
            fonts = TTCollection(str(path)).fonts if path.suffix.lower() in (".ttc", ".otc") else [TTFont(str(path), lazy=True)]
            out = []
            for f in fonts:
                names: list[str] = []
                for rec in f["name"].names:
                    if rec.nameID in (1, 16):
                        try:
                            s = rec.toUnicode().strip()
                        except UnicodeDecodeError:
                            continue
                        if s and s not in names:
                            names.append(s)
                out.append(names)
            return out
        except Exception:  # 文件损坏或格式不支持：改用 fc-scan
            pass
    exe = shutil.which("fc-scan")
    if not exe:
        raise ValueError("无法读取字库族名：未安装 fontTools，也没有 fc-scan（fontconfig）")
    try:
        out_text = subprocess.run([exe, "--format", "%{family}\n", str(path)], capture_output=True, text=True, timeout=120).stdout
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ValueError(f"读取字库族名失败：{path}（{exc}）") from exc
    faces = [_split_list(line) for line in out_text.splitlines() if line.strip()]
    if not faces:
        raise ValueError(f"无法从 {path.name} 读出字体族名：文件可能已损坏或不是字库文件")
    return faces


_ROLE_HINTS = (
    ("xiaobiaosong", ("小标宋", "xiaobiaosong", "标宋", "中宋", "zhongsong")),
    ("fangsong", ("仿宋", "fangsong")),
    ("kaiti", ("楷", "kai")),
    ("heiti", ("黑", "hei")),
    ("songti", ("宋", "song", "simsun", "ming")),
)


def guess_role(families: list[str]) -> str | None:
    """按族名关键字猜测字体类别（如“LXGW WenKai”→楷体）；猜不出时返回 None。"""
    text = " ".join(families).casefold()
    for role, keys in _ROLE_HINTS:
        if any(k in text for k in keys):
            return role
    return None


@dataclass
class FontFileReport:
    src: Path
    dest: Path | None
    families: list[str]
    copied: bool = False
    matches: list[tuple[str, str, str]] = field(default_factory=list)  # (类别, 字库名, 指定/备选)
    guessed: str | None = None

    def advice(self, template: str = "<模板名>") -> str:
        """与模板字库名的对应关系与建议（中文）。"""
        fams = " / ".join(self.families) or "（未读出族名）"
        if any(kind == "指定" for _, _, kind in self.matches):
            role, name, _ = next(m for m in self.matches if m[2] == "指定")
            return f"族名“{fams}”与模板中{ROLE_LABELS.get(role, role)}的指定字库名 {name} 一致"
        if self.matches:
            role, name, _ = self.matches[0]
            return (
                f"族名“{fams}”是{ROLE_LABELS.get(role, role)}的备选字库名：渲染预览会用它代替指定字库；"
                f"若定稿环境（如 Word）也只有该字库，建议在模板中直接指定：gongwen template set {template} fonts.{role}.name={name}"
            )
        if self.guessed:
            fam = self.families[0] if self.families else ""
            return (
                f"警告：族名“{fams}”与模板中任何字库名、备选名都不一致，渲染与 Word 都不会自动使用它；"
                f"如该文件就是本单位使用的{ROLE_LABELS[self.guessed]}，请执行：gongwen template set {template} fonts.{self.guessed}.name={fam}"
            )
        return f"警告：族名“{fams}”与模板中任何字库名都不一致，也未识别为公文字体类别（仿宋、楷体、黑体、小标宋、宋体）"


def classify(families: list[str], profile_fonts: dict[str, Any]) -> tuple[list[tuple[str, str, str]], str | None]:
    hits: list[tuple[str, str, str]] = []
    fams = {norm(f): f for f in families}
    for role in ROLES:
        spec = profile_fonts.get(role) or {}
        if norm(spec.get("name", "")) in fams:
            hits.append((role, fams[norm(spec["name"])], "指定"))
        for alt in spec.get("alternates") or []:
            if norm(alt) in fams:
                hits.append((role, fams[norm(alt)], "备选"))
    return hits, (None if hits else guess_role(families))


def user_font_dir() -> Path:
    base = Path(os.environ["XDG_DATA_HOME"]) if os.environ.get("XDG_DATA_HOME") else Path.home() / ".local" / "share"
    return base / "fonts" / "gongwen"


SYSTEM_FONT_DIR = Path("/usr/local/share/fonts/gongwen")


def install_files(files: list[Path], dest: Path, profile_fonts: dict[str, Any], force: bool = False) -> list[FontFileReport]:
    """把用户提供的字库文件复制到 dest（不改名、不覆盖内容不同的同名文件，除非 force），并核对族名。

    先逐个读出族名并检查冲突，全部通过后才复制：有文件损坏或冲突时一个也不安装。"""
    reports = []
    for src in files:
        fams = list(dict.fromkeys(n for face in file_families(src) for n in face))
        target = dest / Path(src.name).name
        rep = FontFileReport(src=src, dest=target, families=fams)
        rep.matches, rep.guessed = classify(fams, profile_fonts)
        if target.exists() and target.read_bytes() != src.read_bytes() and not force:
            raise ValueError(f"目标已有同名但内容不同的文件：{target}（确认替换请加 --force）")
        reports.append(rep)
    dest.mkdir(parents=True, exist_ok=True)
    for rep in reports:
        if rep.dest.exists() and rep.dest.read_bytes() == rep.src.read_bytes():
            continue  # 已安装
        shutil.copyfile(rep.src, rep.dest)
        os.chmod(rep.dest, 0o644)
        rep.copied = True
    return reports


def refresh_cache(dest: Path) -> tuple[bool, str]:
    exe = shutil.which("fc-cache")
    if not exe:
        return False, "未找到 fc-cache：请安装 fontconfig 后执行 fc-cache -f"
    try:
        r = subprocess.run([exe, "-f", str(dest)], capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"fc-cache 执行失败：{exc}"
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def open_install_plan() -> dict[str, Any]:
    """开源替代字体的安装计划（Debian/Ubuntu）：缺哪些包、能否直接安装、应执行的命令。"""
    apt = shutil.which("apt-get")
    dpkg = shutil.which("dpkg-query")
    missing = []
    for pkg in OPEN_PACKAGES:
        ok = False
        if dpkg:
            try:
                r = subprocess.run([dpkg, "-W", "-f=${Status}", pkg], capture_output=True, text=True, timeout=30)
                ok = r.returncode == 0 and "install ok installed" in r.stdout
            except (subprocess.TimeoutExpired, OSError):
                ok = False
        if not ok:
            missing.append(pkg)
    root = hasattr(os, "geteuid") and os.geteuid() == 0
    prefix = [] if root else ["sudo"]
    return {
        "apt": bool(apt),
        "missing": missing,
        "root": root,
        "command": [*prefix, "apt-get", "install", "-y", *missing] if missing else [],
        "update_command": [*prefix, "apt-get", "update"],
    }


__all__ = [
    "KNOWN_NAMES",
    "LICENSE_NOTE",
    "OPEN_PACKAGES",
    "OPEN_SUBSTITUTES",
    "ROLES",
    "ROLE_LABELS",
    "FontFace",
    "FontFileReport",
    "RoleCheck",
    "check_roles",
    "classify",
    "describe_render",
    "file_families",
    "font_files",
    "fontconfig_xml",
    "guess_role",
    "install_files",
    "installed_families",
    "open_install_plan",
    "refresh_cache",
    "render_env",
    "role_names",
    "user_font_dir",
]
