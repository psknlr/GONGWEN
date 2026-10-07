"""命令行：公文字体（gongwen fonts）、公文模板（gongwen template）与渲染预览（gongwen preview）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


# ---------------------------------------------------------------- 公共
def _ctx(args):
    """（配置, 数据目录, 工作区）：只加载配置，不装配运行时。"""
    from ..kernel.config import load_config
    from .main import _merge, _parse_override, workspace_of

    overrides: dict[str, Any] = {}
    for item in getattr(args, "config", None) or []:
        overrides = _merge(overrides, _parse_override(item))
    ws = workspace_of(args)
    cfg = load_config(ws, profile=getattr(args, "profile", None), overrides=overrides or None)
    return cfg, Path(cfg.environment.data_dir), ws


def _profile(cfg, data_dir: Path, ws: Path, template: str | None):
    from ..layout.templates import resolve_profile

    name = template if template else cfg.layout.template
    return resolve_profile(name or None, data_dir=data_dir, workspace=ws, profile_id=cfg.layout.profile, margin_mode=cfg.layout.margin_mode)


def _emit(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _basis(profile) -> str:
    return f"模板“{profile.template}”" if profile.template else f"国标默认配置 {profile.id}"


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt}[y/N] ").strip().lower() in ("y", "yes", "是")
    except EOFError:
        return False


# ---------------------------------------------------------------- fonts
def cmd_fonts_check(args) -> int:
    from ..layout import fonts

    cfg, dd, ws = _ctx(args)
    profile = _profile(cfg, dd, ws, args.template)
    if not fonts.fc_available():
        msg = "未找到 fontconfig（fc-list、fc-match）：无法检查字体。Debian/Ubuntu 可执行 sudo apt-get install fontconfig"
        _emit({"ok": False, "error": msg}) if args.json else print(msg)
        return EXIT_FAIL
    rows = fonts.check_roles(profile.data["fonts"], mapping=cfg.layout.font_substitution)
    ok = all(r.satisfied for r in rows)
    if args.json:
        _emit({"ok": ok, "basis": _basis(profile), "roles": [r.to_dict() for r in rows], "font_substitution": cfg.layout.font_substitution})
        return EXIT_OK if ok else EXIT_FAIL
    print(f"公文字体检查（{_basis(profile)}）：")
    for r in rows:
        mark = "ok" if r.exact else ("备选" if r.alternate else "--")
        alts = f"；备选名：{'、'.join(r.alternates)}" if r.alternates else ""
        print(f"  [{mark}] {r.category}（{r.role}）指定字库 {r.name}{alts}")
        print(f"        {r.summary()}")
    if not cfg.layout.font_substitution:
        print("  渲染预览的开源替代映射已关闭（layout.font_substitution = false）：缺字库时由 LibreOffice 自行回退")
    print(fonts.LICENSE_NOTE)
    if not ok:
        print("安装本单位合法取得的字库文件：gongwen fonts install --from <目录或文件>（默认装到 ~/.local/share/fonts/gongwen）")
        print("Windows 自带的是“仿宋”“楷体”（不是 *_GB2312）：可在模板中改用这些字库名，如 gongwen template new 本单位 --from windows-fonts")
        if any(r.substitute is None and not r.satisfied for r in rows):
            print("缺少开源替代字体（仅供预览）：gongwen fonts install --open")
    return EXIT_OK if ok else EXIT_FAIL


def _install_open(args) -> int:
    from ..layout import fonts

    plan = fonts.open_install_plan()
    if not plan["apt"]:
        print("本机没有 apt-get（非 Debian/Ubuntu）：请用系统的包管理器安装 Noto Serif/Sans CJK（思源宋体、思源黑体）与霞鹜文楷等开源字体。")
        return EXIT_FAIL
    if not plan["missing"]:
        print(f"开源替代字体已安装：{'、'.join(fonts.OPEN_PACKAGES)}")
        return EXIT_OK
    cmd = " ".join(plan["command"])
    print(f"将安装开源替代字体（仅供渲染预览，不是方正小标宋、仿宋_GB2312 等授权字库）：{'、'.join(plan['missing'])}")
    if not plan["root"]:
        print(f"需要管理员权限，请执行：\n  {' '.join(plan['update_command'])}\n  {cmd}")
        return EXIT_FAIL
    if not args.yes and not _confirm("确认安装？"):
        print(f"未安装。确认后可加 --yes 重试，或自行执行：{cmd}")
        return EXIT_USAGE
    for c in (plan["update_command"], plan["command"]):
        r = subprocess.run(c, check=False)
        if r.returncode != 0:
            print(f"执行失败（退出码 {r.returncode}）：{' '.join(c)}")
            return EXIT_FAIL
    print("已安装。可执行 gongwen fonts check 查看渲染时的替代情况。")
    return EXIT_OK


def cmd_fonts_install(args) -> int:
    from ..layout import fonts

    if args.open:
        if args.src:
            raise ValueError("--open 与 --from 不能同时使用")
        return _install_open(args)
    if not args.src:
        raise ValueError("请用 --from 指定本单位合法取得的字库文件或目录（如从 Windows 的 C:\\Windows\\Fonts 复制的 simfang.ttf），或用 --open 安装开源替代字体")
    cfg, dd, ws = _ctx(args)
    profile = _profile(cfg, dd, ws, args.template)
    files = fonts.font_files(args.src)
    if not files:
        raise FileNotFoundError(f"未找到字库文件（.ttf/.otf/.ttc/.otc）：{'、'.join(args.src)}")
    dest = fonts.SYSTEM_FONT_DIR if args.system else fonts.user_font_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
        probe = dest / ".gongwen-write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise PermissionError(f"无权写入 {dest}（{exc.strerror or exc}）：系统级安装请用管理员权限运行（如 sudo gongwen fonts install --from … --system），或改用 --user") from exc
    reports = fonts.install_files(files, dest, profile.data["fonts"], force=args.force)
    cache_ok, cache_msg = fonts.refresh_cache(dest)
    rows = fonts.check_roles(profile.data["fonts"], mapping=cfg.layout.font_substitution) if fonts.fc_available() else []
    tname = profile.template or "<模板名>"
    if args.json:
        _emit({
            "dest": str(dest),
            "files": [{"src": str(r.src), "dest": str(r.dest), "copied": r.copied, "families": r.families, "matches": r.matches, "guessed_role": r.guessed, "advice": r.advice(tname)} for r in reports],
            "fc_cache": {"ok": cache_ok, "message": cache_msg},
            "roles": [r.to_dict() for r in rows],
        })
        return EXIT_OK if cache_ok else EXIT_FAIL
    print(f"安装目录：{dest}（{_basis(profile)}）")
    for r in reports:
        print(f"  {'已复制' if r.copied else '已存在'} {r.src.name}｜族名：{' / '.join(r.families) or '（未读出）'}")
        print(f"    {r.advice(tname)}")
    if not profile.template and any(not any(k == '指定' for _, _, k in r.matches) for r in reports):
        print("  （当前未使用模板：先执行 gongwen template new <模板名>，修改字库名后在排版时加 --template <模板名>，或在配置中设 layout.template）")
    print(f"字体缓存：{'已刷新（fc-cache）' if cache_ok else '刷新失败：' + cache_msg}")
    if rows:
        print("安装后核对：")
        for r in rows:
            mark = "ok" if r.exact else ("备选" if r.alternate else "--")
            print(f"  [{mark}] {r.category}：{r.summary()}")
    print(fonts.LICENSE_NOTE)
    return EXIT_OK if cache_ok else EXIT_FAIL


def cmd_fonts_map(args) -> int:
    from ..layout import fonts

    cfg, dd, ws = _ctx(args)
    profile = _profile(cfg, dd, ws, args.template)
    if args.conf:
        sys.stdout.write(fonts.fontconfig_xml(profile.data["fonts"]))
        return EXIT_OK
    installed = fonts.installed_families()
    print(f"渲染预览的替代字体映射（{_basis(profile)}；只用于 LibreOffice 渲染子进程，不改变系统字体配置与 DOCX）：")
    for role, names in fonts.role_names(profile.data["fonts"]).items():
        if role not in fonts.ROLES:
            continue
        real = [f"{n}{'（已安装）' if fonts.norm(n) in installed else ''}" for n in names]
        subs = [f"{s.family}{'（已安装）' if fonts.norm(s.family) in installed else '（未安装）'}" for s in fonts.OPEN_SUBSTITUTES[role]]
        print(f"  {fonts.ROLE_LABELS[role]}：{' → '.join(real[:3])}{' …' if len(real) > 3 else ''}")
        print(f"      依次尝试：同类真实字库（{len(names)} 个字库名）→ {' → '.join(subs)}")
    print(f"映射开关：layout.font_substitution = {'true' if cfg.layout.font_substitution else 'false'}；查看完整 fontconfig 配置：gongwen fonts map --conf")
    return EXIT_OK


# ---------------------------------------------------------------- template
def _store(args):
    from ..layout.templates import TemplateStore

    cfg, dd, ws = _ctx(args)
    return TemplateStore(dd, ws), cfg


def _print_changes(tpl) -> None:
    changes = tpl.changes()
    devs = [c for c in changes if c.deviates]
    others = [c for c in changes if not c.deviates]
    print(f"偏离 GB/T 9704—2012：{len(devs)} 项" + ("" if devs else "（无）"))
    for c in devs:
        print(f"  ! {c.describe()}")
    if others:
        print(f"其他调整（字库名等国标未规定的数值、推算值或允许误差内）：{len(others)} 项")
        for c in others:
            print(f"  · {c.describe()}［{c.kind}］")


def cmd_template_list(args) -> int:
    store, cfg = _store(args)
    rows = []
    for name, tpl, err in store.list():
        if tpl is None:
            rows.append({"name": name, "kind": "?", "error": err})
        else:
            rows.append({"name": name, "kind": tpl.kind, "base": tpl.base, "deviations": len(tpl.deviations()), "description": tpl.description, "source": tpl.source, "default": name == (cfg.layout.template or cfg.layout.profile)})
    if args.json:
        _emit(rows)
        return EXIT_OK
    for r in rows:
        if "error" in r:
            print(f"  {r['name']}　（无法加载）{r['error'].splitlines()[0]}")
            continue
        star = "＊" if r["default"] else "　"
        print(f"{star}{r['name']}　{r['kind']}　基于 {r['base']}　偏离国标 {r['deviations']} 项　{r['description']}")
    print(f"＊为默认（配置 layout.template；未设置时为 {cfg.layout.profile}）。用户模板目录：{store.user_dir}")
    return EXIT_OK


def cmd_template_show(args) -> int:
    store, cfg = _store(args)
    tpl = store.get(args.name)
    if args.json:
        _emit({**tpl.to_dict(), "kind": tpl.kind, "source": tpl.source, "changes": [c.to_dict() for c in tpl.changes()], "effective": tpl.effective_data() if args.effective else None})
        return EXIT_OK
    print(f"模板：{tpl.name}（{tpl.kind}，{tpl.source}）")
    print(f"基于：{tpl.base}　说明：{tpl.description or '（无）'}")
    if tpl.unit:
        from ..layout.templates import UNIT_LABELS, fmt_value

        print("单位信息：" + "；".join(f"{UNIT_LABELS.get(k, k)} {fmt_value(v)}" for k, v in tpl.unit.items()))
    _print_changes(tpl)
    data = tpl.effective_data() if args.effective else tpl.to_dict()
    print("—— 生效参数 ——" if args.effective else "—— 模板内容 ——")
    print(yaml.safe_dump(data, allow_unicode=True, sort_keys=False).rstrip())
    return EXIT_OK


def cmd_template_new(args) -> int:
    store, _ = _store(args)
    tpl = store.new(args.name, from_=args.from_, description=args.description or "")
    print(f"已创建模板“{tpl.name}”：{tpl.source}" + (f"（复制自“{args.from_}”）" if args.from_ else ""))
    print(f"修改示例：gongwen template set {tpl.name} elements.title.size=小二 fonts.fangsong.name=仿宋 unit.organ_mark=示例市卫生健康委员会文件")
    return EXIT_OK


def cmd_template_set(args) -> int:
    store, _ = _store(args)
    if not (args.assignments or args.unset):
        raise ValueError("请给出 key.path=value（可多个），或 --unset key.path")
    tpl = store.update(args.name, args.assignments, args.unset)
    print(f"已保存模板“{tpl.name}”：{tpl.source}")
    _print_changes(tpl)
    return EXIT_OK


def cmd_template_validate(args) -> int:
    from ..layout.templates import TemplateError

    store, _ = _store(args)
    try:
        tpl = store.get(args.name)
    except TemplateError as exc:
        if args.json:
            _emit({"valid": False, "errors": exc.errors})
        else:
            print(f"模板“{args.name}”校验不通过：")
            for e in exc.errors:
                print(f"  × {e}")
        return EXIT_USAGE
    if args.json:
        _emit({"valid": True, "changes": [c.to_dict() for c in tpl.changes()]})
        return EXIT_OK
    print(f"模板“{tpl.name}”校验通过（{tpl.kind}，基于 {tpl.base}）。")
    _print_changes(tpl)
    return EXIT_OK


def cmd_template_delete(args) -> int:
    store, _ = _store(args)
    found = store.path(store.check_name(args.name))
    if found is None:
        raise KeyError(f"未找到模板：{args.name}")
    if found[0] == "用户" and not args.yes and not _confirm(f"删除模板“{args.name}”（{found[1]}）？"):
        print("未删除（非交互环境请加 --yes 确认）")
        return EXIT_USAGE
    p = store.delete(args.name)
    print(f"已删除模板“{args.name}”：{p}")
    return EXIT_OK


def cmd_template_export_dotx(args) -> int:
    from ..layout.dotx import export_dotx

    store, cfg = _store(args)
    out = Path(args.out)
    if out.suffix.lower() != ".dotx":
        raise ValueError(f"输出文件应以 .dotx 结尾：{out}")
    tpl = store.get(args.name)
    p = export_dotx(tpl.profile(cfg.layout.margin_mode), out, name=tpl.name)
    print(f"已导出 Word 模板：{p}")
    print("样式：公文标题、主送机关、正文、一级至四级标题、附件说明、署名、成文日期、附注、版记、发文机关标志、发文字号；页码按 7.5 奇偶页分设。")
    devs = tpl.deviations()
    if devs:
        print(f"注意：该模板偏离 GB/T 9704—2012 {len(devs)} 项（gongwen template show {tpl.name} 查看）。")
    print("版记在模板中是普通段落，定稿时须置于最后一面版心底部；方正小标宋简体、仿宋_GB2312 等授权字库须在使用 Word 的电脑上自行安装。")
    return EXIT_OK


# ---------------------------------------------------------------- preview
def cmd_preview(args) -> int:
    from ..importer import ir_from_file
    from ..layout.preview import can_render, preview_docx, preview_ir, write_manifest
    from ..schemas.layout import LayoutReport

    cfg, dd, ws = _ctx(args)
    src = Path(args.source)
    if src.is_file():
        ir = ir_from_file(src.name, src.read_bytes(), genre=args.genre)
        if ir.meta.get("import_warnings"):
            print(f"解析提示：{ir.meta['import_warnings']}", file=sys.stderr)
        profile = _profile(cfg, dd, ws, args.template)
        out = Path(args.out) if args.out else src.parent / "gongwen-preview" / src.stem
        stem = f"{src.stem}.预览"
        if any((out / f"{stem}{ext}").resolve() == src.resolve() for ext in (".docx", ".html", ".md", ".pdf")):
            stem += "1"
        res = preview_ir(ir, out, profile, font_substitution=cfg.layout.font_substitution, stem=stem, title=ir.title or src.stem)
    else:
        from .main import make_engine

        eng = make_engine(args, interactive=False)
        tid = args.source
        if not eng.store.exists(tid):
            raise FileNotFoundError(f"文稿文件或任务不存在：{tid}")
        st = eng.load_state(tid)
        ir = eng.current_ir(st)
        if ir is None:
            raise ValueError(f"任务 {tid} 尚未形成文稿，无法预览")
        out = Path(args.out) if args.out else Path(eng.rt.data_dir) / "previews" / tid
        report = eng.store.load_model(tid, "layout_report", LayoutReport)
        docx = next((Path(o.path) for o in (report.outputs if report else []) if o.kind == "docx"), None)
        if args.template or docx is None or not docx.is_file():
            # 指定了其他模板，或任务尚未排版：按当前文稿重新排版到预览目录（不改变任务本身）
            profile = _profile(cfg, dd, ws, args.template or st.options.get("layout_template"))
            res = preview_ir(ir, out, profile, font_substitution=cfg.layout.font_substitution, stem=f"{tid}.预览", title=ir.title)
        else:
            profile = _profile(cfg, dd, ws, st.options.get("layout_template"))
            res = preview_docx(docx, out, report, profile, ir=ir, font_substitution=cfg.layout.font_substitution, title=ir.title)
    write_manifest(res)
    if args.json:
        _emit(res.to_dict())
    else:
        print(f"预览：{res.html}")
        if res.pages:
            print(f"页面图像 {len(res.pages)} 面：{res.pages[0].parent}")
        else:
            print("未实际渲染" + ("（LibreOffice 转换失败）" if can_render() else "（本机缺少 LibreOffice 或 poppler-utils）") + "：preview.html 为 HTML 近似预览，不代表实际版面")
        rep = res.report
        if rep:
            if rep.template:
                print(f"模板：{rep.template}（偏离国标 {len(rep.deviations)} 项）")
            for c in rep.checks:
                if c.status != "pass":
                    print(f"  [{c.status}] {c.item}：要求 {c.expected}；实际 {c.actual}")
    if args.open:
        import webbrowser

        webbrowser.open(res.html.resolve().as_uri())
    return EXIT_OK


# ---------------------------------------------------------------- 解析器
def add_layout_commands(sub) -> None:
    f = sub.add_parser("fonts", help="公文字体：检查、安装本单位字库文件、开源替代字体").add_subparsers(dest="fonts_cmd", metavar="子命令")
    p = f.add_parser("check", help="逐类核对指定字库（或备选名）是否已安装，以及渲染预览时实际使用的字体")
    p.add_argument("--template", help="按该模板的字库名核对（默认 layout.template）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fonts_check)
    p = f.add_parser("install", help="安装本单位合法取得的字库文件（--from），或用 apt 安装开源替代字体（--open）")
    p.add_argument("--from", dest="src", nargs="+", metavar="目录或文件", help="字库文件或目录（.ttf/.otf/.ttc/.otc），如单位购买的方正字库、Windows 自带的仿宋/楷体")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--user", action="store_true", help="安装到 ~/.local/share/fonts/gongwen（默认）")
    g.add_argument("--system", action="store_true", help="安装到 /usr/local/share/fonts/gongwen（需要管理员权限）")
    p.add_argument("--open", action="store_true", help="安装开源替代字体（Debian/Ubuntu 软件源，仅供预览）")
    p.add_argument("--yes", action="store_true", help="--open 时不再询问")
    p.add_argument("--force", action="store_true", help="覆盖目标目录中内容不同的同名文件")
    p.add_argument("--template", help="按该模板的字库名核对族名（默认 layout.template）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_fonts_install)
    p = f.add_parser("map", help="显示渲染预览时的替代字体映射")
    p.add_argument("--template")
    p.add_argument("--conf", action="store_true", help="输出生成的 fontconfig 配置")
    p.set_defaults(func=cmd_fonts_map)

    t = sub.add_parser("template", help="公文模板（可自行调整）：查看、新建、修改、校验、导出 Word 模板").add_subparsers(dest="template_cmd", metavar="子命令")
    p = t.add_parser("list", help="全部模板（内置、工作区、用户）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_template_list)
    p = t.add_parser("show", help="模板内容、单位信息与偏离国标的各项")
    p.add_argument("name")
    p.add_argument("--effective", action="store_true", help="显示合并后的生效参数")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_template_show)
    p = t.add_parser("new", help="新建用户模板（可复制已有模板）")
    p.add_argument("name")
    p.add_argument("--from", dest="from_", metavar="模板", help="复制该模板的设置（如 windows-fonts）")
    p.add_argument("--description")
    p.set_defaults(func=cmd_template_new)
    p = t.add_parser("set", help="修改用户模板：key.path=value（可多个），如 elements.title.size=小二")
    p.add_argument("name")
    p.add_argument("assignments", nargs="*", metavar="key.path=value")
    p.add_argument("--unset", action="append", metavar="key.path", help="恢复为基础配置档的值（可重复）")
    p.set_defaults(func=cmd_template_set)
    p = t.add_parser("validate", help="校验模板并列出偏离国标的各项")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_template_validate)
    p = t.add_parser("delete", help="删除用户模板")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_template_delete)
    p = t.add_parser("export-dotx", help="导出 Word 模板（.dotx）：页面、版心、页码与公文段落样式")
    p.add_argument("name")
    p.add_argument("-o", "--out", required=True, help="输出文件（.dotx）")
    p.set_defaults(func=cmd_template_export_dotx)

    p = sub.add_parser("preview", help="排版并渲染预览：页面图像与核验结果（输出 preview.html）")
    p.add_argument("source", help="文稿文件（txt/md/docx/pdf）或任务编号")
    p.add_argument("--template", help="按该模板排版（任务预览时另行排版到预览目录，不改变任务）")
    p.add_argument("-o", "--out", help="输出目录（文件默认为同目录下 gongwen-preview/<文件名>；任务默认为数据目录 previews/<任务>）")
    p.add_argument("--genre")
    p.add_argument("--open", action="store_true", help="用浏览器打开 preview.html")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_preview)


__all__ = ["add_layout_commands"]
