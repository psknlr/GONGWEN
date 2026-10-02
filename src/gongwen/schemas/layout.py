"""阶段9：版式编译与检查报告 LayoutReport（技能11 的输出）。"""

from __future__ import annotations

from pydantic import Field

from .common import GWModel


class LayoutCheck(GWModel):
    rule_id: str
    item: str
    expected: str
    actual: str
    status: str = Field(description="pass/fail/warn/na/unverified")
    clause: str = Field(default="", description="依据条款，如 GB/T 9704—2012 5.2.1")
    level: str = Field(default="国标", description="国标/条例/实务/单位制度")
    conditional: bool = Field(default=False, description="条件性要求（如“一般”“特定情况可以作适当调整”）")
    note: str = ""


class RenderInfo(GWModel):
    renderer: str = Field(default="none", description="如 LibreOffice 24.2；none 表示未实际渲染")
    rendered: bool = False
    pdf_path: str | None = None
    pages: int | None = None
    page_size_mm: tuple[float, float] | None = None
    fonts_requested: list[str] = Field(default_factory=list)
    fonts_embedded: list[str] = Field(default_factory=list)
    substitutions: list[str] = Field(default_factory=list, description="字体缺失或被替代的情况")
    first_page_has_body: bool | None = None
    notes: list[str] = Field(default_factory=list)


class OutputFile(GWModel):
    kind: str = Field(description="docx/pdf/html/json/md")
    path: str
    sha256: str


class LayoutReport(GWModel):
    profile: str
    profile_version: str
    checks: list[LayoutCheck] = Field(default_factory=list)
    render: RenderInfo = Field(default_factory=RenderInfo)
    outputs: list[OutputFile] = Field(default_factory=list)

    def failures(self) -> list[LayoutCheck]:
        return [c for c in self.checks if c.status == "fail"]

    def fully_verified(self) -> bool:
        """只有实际渲染且无字体替代时，才可声称满足指定版式。"""
        return self.render.rendered and not self.render.substitutions and not self.failures()
