"""版式编译与检查：DocumentIR → DOCX / HTML，参数核验与实际渲染核验。"""

from .docx_checks import check_docx
from .docx_compiler import compile_docx
from .html_preview import render_document, standalone_html
from .profile import LayoutProfile, split_title, text_width_chars
from .render_check import check_rendering, tools_available

__all__ = [
    "LayoutProfile",
    "check_docx",
    "check_rendering",
    "compile_docx",
    "render_document",
    "split_title",
    "standalone_html",
    "text_width_chars",
    "tools_available",
]
