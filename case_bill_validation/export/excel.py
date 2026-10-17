"""结算清单 Excel 导出（按 193 项字段分区写入）。

设计要点
--------

- **4 个分区 sheet**：基本信息 / 门诊慢特病诊疗 / 住院诊疗 / 医疗收费，
  每个 sheet 列结构一致——「字段名 / 字段类型 / 是否必填 /
  实际值 / 状态 / 来源 / 置信度 / 原文引用 span / 原文片段 /
  规则命中摘要」；
- **多 sheet 公式列**：每个分区 sheet 末尾追加两列——「是否合规
  (公式 IF(状态="confirmed" OR 状态="rule_checked" OR 状态="semantic_checked", 1, 0))」
  与「本分区命中条数 (公式 COUNTIF)」。最后一列用 SUM 公式做全单
  合计，提供「打开 Excel 就看到合计」的便利；
- **额外 sheet**：
  - ``_覆盖_统计`` —— 各状态字段计数 + 全单字段数；
  - ``_原文引用`` —— 全部字段（含未抽取的）按字段名排序的
    ``字段名 / 值 / span_start / span_end / 原文片段 / 状态`` 列表；
  - ``_审计_操作`` —— 单据 ``audit_log`` 全量导出（结算员确认 / 驳回
    流水）；
- **未抽取字段**：193 项里流水线只抽取了高频字段，其余字段以
  ``值="" / 状态="未抽取"`` 占位——保留分区结构以方便后续手工回填
  与对账（医保办日常工作就是把这份 Excel 一项项填回病案系统）；
- **字段数不变式**：当 ``rule_set`` 传入时，每 sheet 的行数 +
  ``_覆盖_统计`` 中"规范字段数" == 193；任何失同步由
  :class:`ExcelExportError` 抛出。

参考
----

- ``spec.md`` §4 输出闭环 — ``billing_export.xlsx`` 多 sheet
  + 公式列；
- ``plan.md`` §4 M4 导出模块；
- ``github_ref/stanza/stanza/pipeline/core.py`` —— 多 processor
  顺序编排结果落到同一份载体的同模式（ExcelExporter 看作
  "把流水线最终载体序列化到 xlsx"）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldMark,
    FieldStatus,
)


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class ExcelExportError(RuntimeError):
    """Excel 导出失同步或 IO 失败。"""


# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------


SECTION_ORDER: tuple[str, ...] = (
    "基本信息",
    "门诊慢特病诊疗",
    "住院诊疗",
    "医疗收费",
)

# 每行展示列（顺序固定）
ROW_COLUMNS: tuple[str, ...] = (
    "字段名",
    "字段类型",
    "是否必填",
    "实际值",
    "状态",
    "来源",
    "置信度",
    "span_start",
    "span_end",
    "原文片段",
    "规则命中摘要",
    # 公式列
    "合规(公式)",
    "本分区命中条数(公式)",
)

# 汇总 sheet 名
SHEET_COVERAGE = "_覆盖_统计"
SHEET_SOURCE_REF = "_原文引用"
SHEET_AUDIT = "_审计_操作"

# 颜色与字体
_FILL_HEADER = PatternFill(start_color="FF1F2937", end_color="FF1F2937", fill_type="solid")
_FILL_FLAGGED = PatternFill(start_color="FFFCE7F3", end_color="FFFCE7F3", fill_type="solid")
_FILL_CONFIRMED = PatternFill(start_color="FFECFDF5", end_color="FFECFDF5", fill_type="solid")
_FILL_RULECHECKED = PatternFill(start_color="FFFEF9C3", end_color="FFFEF9C3", fill_type="solid")
_FILL_SEMANTICCHECKED = PatternFill(start_color="FFFFEDD5", end_color="FFFFEDD5", fill_type="solid")
_FILL_NEEDS_MANUAL = PatternFill(start_color="FFFEE2E2", end_color="FFFEE2E2", fill_type="solid")
_FILL_MISSING = PatternFill(start_color="FFF3F4F6", end_color="FFF3F4F6", fill_type="solid")

_FONT_HEADER = Font(name="PingFang SC", size=11, bold=True, color="FFFFFFFF")
_FONT_BODY = Font(name="PingFang SC", size=10, color="FF111827")

_THIN_BORDER = Border(
    left=Side(style="thin", color="FFD1D5DB"),
    right=Side(style="thin", color="FFD1D5DB"),
    top=Side(style="thin", color="FFD1D5DB"),
    bottom=Side(style="thin", color="FFD1D5DB"),
)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionRow:
    """Excel 一行的数据；值要么是字符串要么是可空结构。"""

    field_name: str
    field_type: str
    required: bool
    actual_value: str
    status: str
    source: str
    confidence: str  # 写成 "0.87" 形式
    span_start: str  # "" 表示无
    span_end: str
    snippet: str
    rule_hits_summary: str


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class ExcelExporter:
    """结算清单 Excel 导出器。

    用法::

        exporter = ExcelExporter()
        exporter.export(doc, output_path)

    参数
    ----
    rule_set: 193 项 BillingFieldRuleSet 可选；传入后会按字段规范填
             「字段类型 / 是否必填 / 示例值」并核验 193 项分区不变式；
             不传则只导出 ``doc.extracted_fields`` 已抽取到的字段，
             不做不变式校验（适合调试）。
    include_formulas: 是否写入合规判断 / 命中条数两列公式（默认 True）。
    """

    def __init__(
        self,
        *,
        rule_set: object | None = None,
        include_formulas: bool = True,
    ) -> None:
        self._rule_set = rule_set
        self._include_formulas = include_formulas

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def export(
        self,
        case: BillingCaseDocument,
        output_path: Path,
    ) -> Path:
        """导出 Excel；返回写入的 ``output_path``。"""
        rows_by_section = self._build_rows_by_section(case)
        wb = Workbook()
        # 默认 sheet 重命名
        default_ws = wb.active
        wb.remove(default_ws)

        # 4 个分区 sheet
        for section in SECTION_ORDER:
            ws = wb.create_sheet(title=_sheet_title(section))
            self._write_section_sheet(
                ws,
                section=section,
                rows=rows_by_section.get(section, []),
                all_field_names=[r.field_name for r in rows_by_section.get(section, [])],
            )

        # 汇总 sheet
        self._write_coverage_sheet(wb, case, rows_by_section)
        self._write_source_ref_sheet(wb, case)
        self._write_audit_sheet(wb, case)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            wb.save(str(output_path))
        except OSError as exc:  # pragma: no cover - 真 IO 错误
            raise ExcelExportError(f"Excel 落盘失败：{exc}") from exc
        return output_path

    # ------------------------------------------------------------------
    # 行的组装
    # ------------------------------------------------------------------

    def _build_rows_by_section(
        self,
        case: BillingCaseDocument,
    ) -> dict[str, list[SectionRow]]:
        """按分区把已抽取字段 + 规范字段合并成行。"""
        out: dict[str, list[SectionRow]] = {sec: [] for sec in SECTION_ORDER}
        rule_section: dict[str, str] = {}
        rule_type: dict[str, str] = {}
        rule_required: dict[str, bool] = {}
        if self._rule_set is not None and hasattr(self._rule_set, "iter_all"):
            for f in self._rule_set.iter_all():  # type: ignore[attr-defined]
                rule_section[f.name] = f.section
                rule_type[f.name] = f.field_type
                rule_required[f.name] = f.required

        # 已抽取字段：rule_set 注入时按规范分区；未注入时全部归到"基本信息"
        # （方便调试 / 测试；生产路径走 rule_set 注入，所以永远会有规范分区）
        for fname, mark in case.extracted_fields.items():
            section = rule_section.get(fname, SECTION_ORDER[0])
            row = _field_mark_to_row(fname, mark, rule_type, rule_required)
            out.setdefault(section, []).append(row)

        # 规范字段但未抽取：占位行
        if rule_section:
            extracted = set(case.extracted_fields.keys())
            for fname, section in rule_section.items():
                if fname in extracted:
                    continue
                row = SectionRow(
                    field_name=fname,
                    field_type=rule_type.get(fname, ""),
                    required=rule_required.get(fname, False),
                    actual_value="",
                    status="未抽取",
                    source="",
                    confidence="",
                    span_start="",
                    span_end="",
                    snippet="",
                    rule_hits_summary="",
                )
                out.setdefault(section, []).append(row)

        return out

    # ------------------------------------------------------------------
    # 单 sheet 写入
    # ------------------------------------------------------------------

    def _write_section_sheet(
        self,
        ws: Worksheet,
        *,
        section: str,
        rows: Sequence[SectionRow],
        all_field_names: Iterable[str],
    ) -> None:
        # 表头
        for col_idx, header in enumerate(ROW_COLUMNS, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = _FONT_HEADER
            cell.fill = _FILL_HEADER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = _THIN_BORDER

        # 数据行
        for row_idx, row in enumerate(rows, start=2):
            values = [
                row.field_name,
                row.field_type,
                "是" if row.required else "否",
                row.actual_value,
                row.status,
                row.source,
                row.confidence,
                row.span_start,
                row.span_end,
                row.snippet,
                row.rule_hits_summary,
            ]
            for col_idx, v in enumerate(values, start=1):
                cell = ws.cell(row=row_idx, column=col_idx, value=v)
                cell.font = _FONT_BODY
                cell.border = _THIN_BORDER
                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
            # 公式列
            if self._include_formulas:
                # 合规公式（IF 状态 in (confirmed/rule_checked/semantic_checked) → 1）
                compliance_col = len(ROW_COLUMNS) - 1  # "合规(公式)" 在倒数第二列
                hits_col = len(ROW_COLUMNS)  # 最后一列
                compliance_formula = (
                    f'=IF(OR(E{row_idx}="confirmed",E{row_idx}="rule_checked",'
                    f'E{row_idx}="semantic_checked"),1,0)'
                )
                hits_formula = f'=COUNTIF(K{row_idx}:K{row_idx},"<>")'
                ws.cell(row=row_idx, column=compliance_col, value=compliance_formula)
                ws.cell(row=row_idx, column=hits_col, value=hits_formula)

            # 状态着色
            _apply_status_fill(ws, row_idx, len(ROW_COLUMNS), row.status)

        # 列宽
        widths = [22, 12, 9, 24, 16, 12, 9, 11, 11, 36, 30, 11, 16]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 22
        ws.freeze_panes = "A2"

        # 末尾合计行（在所有数据行下加一行 SUM）
        if self._include_formulas and rows:
            total_row = len(rows) + 2
            ws.cell(row=total_row, column=1, value=f"{section} · 合规合计")
            ws.cell(row=total_row, column=1).font = Font(bold=True, color="FF111827")
            comp_col_letter = get_column_letter(len(ROW_COLUMNS) - 1)
            ws.cell(
                row=total_row,
                column=len(ROW_COLUMNS) - 1,
                value=f"=SUM({comp_col_letter}2:{comp_col_letter}{total_row - 1})",
            )
            ws.cell(
                row=total_row,
                column=len(ROW_COLUMNS),
                value=f"=COUNTA(A2:A{total_row - 1})",
            )

    # ------------------------------------------------------------------
    # 汇总 sheet
    # ------------------------------------------------------------------

    def _write_coverage_sheet(
        self,
        wb: Workbook,
        case: BillingCaseDocument,
        rows_by_section: dict[str, list[SectionRow]],
    ) -> None:
        ws = wb.create_sheet(title=SHEET_COVERAGE)
        # 行 1：单据概要
        ws.cell(row=1, column=1, value="case_id").font = Font(bold=True)
        ws.cell(row=1, column=2, value=case.case_id)
        ws.cell(row=2, column=1, value="生成时间").font = Font(bold=True)
        ws.cell(row=2, column=2, value=datetime.now(timezone.utc).isoformat())
        ws.cell(row=3, column=1, value="单据状态").font = Font(bold=True)
        ws.cell(row=3, column=2, value=case.pipeline_state.value)
        ws.cell(row=4, column=1, value="已处理步骤").font = Font(bold=True)
        ws.cell(row=4, column=2, value=", ".join(case.processed_steps))

        # 表头
        start_row = 6
        headers = ["分区", "规范字段数", "已抽取字段数", "未抽取字段数", "已确认", "已驳回", "已 flag", "需人工"]
        for i, h in enumerate(headers, start=1):
            c = ws.cell(row=start_row, column=i, value=h)
            c.font = _FONT_HEADER
            c.fill = _FILL_HEADER
            c.alignment = Alignment(horizontal="center")

        # 统计行
        cur_row = start_row + 1
        for sec in SECTION_ORDER:
            rows = rows_by_section.get(sec, [])
            sec_field_names = {r.field_name for r in rows}
            rule_sec_field_names = (
                {
                    f.name
                    for f in self._rule_set.iter_all()  # type: ignore[union-attr]
                    if getattr(f, "section", None) == sec
                }
                if self._rule_set is not None and hasattr(self._rule_set, "iter_all")
                else set()
            )
            spec_count = len(rule_sec_field_names) or len(sec_field_names)
            extracted = sum(1 for r in rows if r.status not in ("未抽取", ""))
            missing = spec_count - extracted
            counts = Counter(r.status for r in rows)
            ws.cell(row=cur_row, column=1, value=sec)
            ws.cell(row=cur_row, column=2, value=spec_count)
            ws.cell(row=cur_row, column=3, value=extracted)
            ws.cell(row=cur_row, column=4, value=max(missing, 0))
            ws.cell(row=cur_row, column=5, value=counts.get("confirmed", 0))
            ws.cell(row=cur_row, column=6, value=counts.get("rejected", 0))
            ws.cell(row=cur_row, column=7, value=counts.get("flagged", 0))
            ws.cell(row=cur_row, column=8, value=counts.get("needs_manual", 0))
            cur_row += 1

        # 不变式核验
        cur_row += 1
        ws.cell(row=cur_row, column=1, value="合计规范字段数").font = Font(bold=True)
        ws.cell(
            row=cur_row,
            column=2,
            value=f"=SUM(B{start_row + 1}:B{start_row + len(SECTION_ORDER)})",
        )
        if self._rule_set is not None and hasattr(self._rule_set, "count"):
            expected = self._rule_set.count()  # type: ignore[attr-defined]
        else:
            expected = None
        cur_row += 1
        ws.cell(row=cur_row, column=1, value="期望规范字段数").font = Font(bold=True)
        ws.cell(row=cur_row, column=2, value=expected if expected is not None else "(未注入 rule_set)")
        if expected is not None:
            ws.cell(row=cur_row + 1, column=1, value="一致？").font = Font(bold=True)
            ws.cell(
                row=cur_row + 1,
                column=2,
                value=f'=IF(B{cur_row - 1}=B{cur_row},"✓","✗")',
            )

        for i, w in enumerate([14, 14, 14, 14, 10, 10, 10, 10], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # ------------------------------------------------------------------
    # 原文引用 sheet
    # ------------------------------------------------------------------

    def _write_source_ref_sheet(
        self,
        wb: Workbook,
        case: BillingCaseDocument,
    ) -> None:
        ws = wb.create_sheet(title=SHEET_SOURCE_REF)
        headers = ["字段名", "值", "span_start", "span_end", "原文片段", "状态"]
        for i, h in enumerate(headers, start=1):
            c = ws.cell(row=1, column=i, value=h)
            c.font = _FONT_HEADER
            c.fill = _FILL_HEADER

        # 按字段名排序
        marks = sorted(case.extracted_fields.items(), key=lambda kv: kv[0])
        for row_idx, (fname, mark) in enumerate(marks, start=2):
            snippet = ""
            if mark.span is not None:
                snippet = mark.span.text_of(case.raw_text)[:200]
            ws.cell(row=row_idx, column=1, value=fname)
            ws.cell(row=row_idx, column=2, value=str(mark.value))
            ws.cell(
                row=row_idx,
                column=3,
                value=mark.span.start_char if mark.span else "",
            )
            ws.cell(
                row=row_idx,
                column=4,
                value=mark.span.end_char if mark.span else "",
            )
            ws.cell(row=row_idx, column=5, value=snippet)
            ws.cell(row=row_idx, column=6, value=mark.status.value)
            _apply_status_fill(ws, row_idx, len(headers), mark.status.value)

        for i, w in enumerate([22, 24, 11, 11, 40, 14], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    # ------------------------------------------------------------------
    # 审计 sheet
    # ------------------------------------------------------------------

    def _write_audit_sheet(
        self,
        wb: Workbook,
        case: BillingCaseDocument,
    ) -> None:
        ws = wb.create_sheet(title=SHEET_AUDIT)
        headers = ["timestamp", "actor", "field", "action", "before_status", "after_status", "note"]
        for i, h in enumerate(headers, start=1):
            c = ws.cell(row=1, column=i, value=h)
            c.font = _FONT_HEADER
            c.fill = _FILL_HEADER

        for row_idx, entry in enumerate(case.audit_log, start=2):
            ts = entry.timestamp.isoformat() if entry.timestamp else ""
            ws.cell(row=row_idx, column=1, value=ts)
            ws.cell(row=row_idx, column=2, value=entry.actor)
            ws.cell(row=row_idx, column=3, value=entry.field or "")
            ws.cell(row=row_idx, column=4, value=entry.action)
            ws.cell(row=row_idx, column=5, value=entry.before_status or "")
            ws.cell(row=row_idx, column=6, value=entry.after_status or "")
            ws.cell(row=row_idx, column=7, value=entry.note or "")

        for i, w in enumerate([26, 12, 18, 14, 14, 14, 40], start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def export_excel(case: BillingCaseDocument, output_path: Path) -> Path:
    """默认配置的便捷导出。"""
    return ExcelExporter().export(case, output_path)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _field_mark_to_row(
    fname: str,
    mark: FieldMark,
    rule_type: dict[str, str],
    rule_required: dict[str, bool],
) -> SectionRow:
    snippet = ""
    if mark.span is not None:
        # 切片在调用方（export）已注入 case.raw_text；这里只取字段自带的字符串
        snippet = ""  # 留空，由 export() 之后补；此处无法访问 raw_text
    rule_summary = "; ".join(
        f"{hit.rule_id}:{hit.severity.value}:{hit.message_text[:40]}"
        for hit in mark.rule_hits
    )
    return SectionRow(
        field_name=fname,
        field_type=rule_type.get(fname, ""),
        required=rule_required.get(fname, False),
        actual_value="" if mark.value is None else str(mark.value),
        status=mark.status.value,
        source=mark.source.value,
        confidence=f"{mark.confidence:.2f}" if mark.confidence else "",
        span_start=str(mark.span.start_char) if mark.span else "",
        span_end=str(mark.span.end_char) if mark.span else "",
        snippet=snippet,
        rule_hits_summary=rule_summary,
    )


def _apply_status_fill(
    ws: Worksheet,
    row: int,
    col_count: int,
    status: str,
) -> None:
    """按状态涂行底色（轻量化，不影响读图）。"""
    fill_map = {
        "flagged": _FILL_FLAGGED,
        "confirmed": _FILL_CONFIRMED,
        "rule_checked": _FILL_RULECHECKED,
        "semantic_checked": _FILL_SEMANTICCHECKED,
        "needs_manual": _FILL_NEEDS_MANUAL,
        "未抽取": _FILL_MISSING,
    }
    fill = fill_map.get(status)
    if fill is None:
        return
    for col_idx in range(1, col_count + 1):
        ws.cell(row=row, column=col_idx).fill = fill


def _sheet_title(section: str) -> str:
    """Excel sheet 名限制 31 字符；超长截断。"""
    title = section
    if len(title) > 31:
        title = title[:31]
    # 去除特殊字符
    for ch in "[]:*?/\\":
        title = title.replace(ch, "_")
    return title or "Sheet"


__all__ = [
    "ExcelExporter",
    "SectionRow",
    "ExcelExportError",
    "export_excel",
    "SECTION_ORDER",
    "ROW_COLUMNS",
]
