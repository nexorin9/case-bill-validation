"""ExcelExporter 多 sheet 结构 + 公式列 + 原文引用 sheet 测试（Task 30）。

覆盖：
- 多 sheet 结构（基本信息 / 门诊慢特病诊疗 / 住院诊疗 / 医疗收费）4 个
  分区 sheet 全部存在；
- 公式列存在：合规(公式) IF(OR(E=confirmed, ...)) 列 + 本分区命中
  条数 COUNTIF 公式 + 末尾 SUM 合计公式；
- 原文引用 sheet（_原文引用）含 字段名 / 值 / span_start / span_end /
  原文片段 / 状态 列。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldMark,
    FieldStatus,
    SpanRef,
)
from case_bill_validation.export.excel import (
    ExcelExporter,
    ROW_COLUMNS,
    SECTION_ORDER,
)
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet
from case_bill_validation.rules.migrations import (
    SECTION_BASIC,
    SECTION_BILLING,
    SECTION_INPATIENT,
    SECTION_OP_SPECIAL,
    EXPECTED_TOTAL_FIELDS,
)


# ---------------------------------------------------------------------------
# 辅助：构造最小可用单据
# ---------------------------------------------------------------------------


def _make_doc_multisheet(*, raw_text: str = None) -> BillingCaseDocument:
    """构造一份含已抽取字段（覆盖 3 个分区）的单据。

    - 基本信息：入院日期 / 出院日期 / 性别（confirmed）
    - 住院诊疗：主要手术操作名称（semantic_checked）
    - 医疗收费：金额合计（rule_checked）
    """
    if raw_text is None:
        raw_text = (
            "入院日期:2026-01-12。出院日期:2026-01-26。"
            "性别:男。主要手术操作:冠状动脉支架植入术。"
            "金额合计:28945.67元。"
        )
    doc = BillingCaseDocument(case_id="case-multisheet-001", raw_text=raw_text)
    doc.upsert_field(
        FieldMark(
            field_name="入院日期",
            value="2026-01-12",
            span=SpanRef(start_char=5, end_char=15),
            confidence=0.95,
            status=FieldStatus.RULE_CHECKED,
        )
    )
    doc.upsert_field(
        FieldMark(
            field_name="出院日期",
            value="2026-01-26",
            span=SpanRef(start_char=20, end_char=30),
            confidence=0.93,
            status=FieldStatus.SEMANTIC_CHECKED,
        )
    )
    doc.upsert_field(
        FieldMark(
            field_name="性别",
            value="男",
            span=SpanRef(start_char=34, end_char=35),
            confidence=0.99,
            status=FieldStatus.CONFIRMED,
        )
    )
    doc.upsert_field(
        FieldMark(
            field_name="主要手术操作名称",
            value="冠状动脉支架植入术",
            span=SpanRef(start_char=42, end_char=52),
            confidence=0.85,
            status=FieldStatus.SEMANTIC_CHECKED,
        )
    )
    doc.upsert_field(
        FieldMark(
            field_name="金额合计",
            value="28945.67",
            span=SpanRef(start_char=54, end_char=62),
            confidence=0.92,
            status=FieldStatus.RULE_CHECKED,
        )
    )
    return doc


def _load_rule_set(tmp_path: Path) -> BillingFieldRuleSet:
    db_path = tmp_path / "billing_fields.db"
    return BillingFieldRuleSet.open(db_path)


# ---------------------------------------------------------------------------
# Tests · 多 sheet 结构
# ---------------------------------------------------------------------------


class TestExcelMultisheetStructure:
    """Excel 至少含 3 个分区 sheet（按 SECTION_ORDER 排列）。"""

    def test_three_section_sheets_exist(self, tmp_path: Path) -> None:
        """SECTION_ORDER 含 4 个分区；至少 3 个分区 sheet 必须存在。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "multisheet_3.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path), read_only=True)
        sheet_names = wb.sheetnames
        # 4 个分区 sheet：基本信息 / 门诊慢特病诊疗 / 住院诊疗 / 医疗收费
        for sec in SECTION_ORDER:
            assert sec in sheet_names, (
                f"缺少分区 sheet '{sec}'，现有 sheet={sheet_names}"
            )
        wb.close()

    def test_section_sheets_follow_section_order(self, tmp_path: Path) -> None:
        """分区 sheet 按 SECTION_ORDER 顺序排列。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "multisheet_order.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path), read_only=True)
        sheet_names = wb.sheetnames
        # 前 4 个 sheet 应该与 SECTION_ORDER 完全一致
        assert sheet_names[: len(SECTION_ORDER)] == list(SECTION_ORDER), (
            f"分区 sheet 顺序错位：{sheet_names[:4]} != {SECTION_ORDER}"
        )
        wb.close()

    def test_inpatient_and_billing_sheets_contain_extracted_fields(
        self, tmp_path: Path
    ) -> None:
        """分区 sheet 写入对应字段（住院诊疗 / 医疗收费）。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "multisheet_extract.xlsx"
        ExcelExporter(rule_set=_load_rule_set(tmp_path)).export(doc, out_path)

        wb = load_workbook(str(out_path))
        inpatient = wb[SECTION_INPATIENT]
        inpatient_fields = [
            c.value for c in inpatient["A"] if c.value is not None
        ]
        billing = wb[SECTION_BILLING]
        billing_fields = [
            c.value for c in billing["A"] if c.value is not None
        ]

        assert "主要手术操作名称" in inpatient_fields, (
            f"住院诊疗 sheet 应含主要手术操作名称，actual={inpatient_fields[:5]}"
        )
        assert "金额合计" in billing_fields, (
            f"医疗收费 sheet 应含金额合计，actual={billing_fields[:5]}"
        )

    def test_outpatient_special_disease_sheet_exists(self, tmp_path: Path) -> None:
        """门诊慢特病诊疗 sheet 存在（即便无字段）。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "multisheet_op.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path), read_only=True)
        sheet_names = wb.sheetnames
        assert SECTION_OP_SPECIAL in sheet_names, (
            f"缺 {SECTION_OP_SPECIAL} sheet，实际={sheet_names}"
        )
        wb.close()

    def test_total_field_count_matches_spec_when_rule_set_injected(
        self, tmp_path: Path
    ) -> None:
        """rule_set 注入时合计规范字段数 == 193（4 个分区相加）。"""
        rule_set = _load_rule_set(tmp_path)
        doc = _make_doc_multisheet()
        out_path = tmp_path / "multisheet_193.xlsx"
        ExcelExporter(rule_set=rule_set).export(doc, out_path)

        wb = load_workbook(str(out_path))
        cov = wb["_覆盖_统计"]
        # 找"合计规范字段数"行与"期望规范字段数"
        rows = list(cov.iter_rows(values_only=True))
        sum_row = None
        expected_row = None
        for r in rows:
            if r and r[0] == "合计规范字段数":
                sum_row = r
            if r and r[0] == "期望规范字段数":
                expected_row = r
        assert sum_row is not None, "_覆盖_统计 缺'合计规范字段数'行"
        assert expected_row is not None, "_覆盖_统计 缺'期望规范字段数'行"
        # 期望规范字段数 == 193
        assert expected_row[1] == EXPECTED_TOTAL_FIELDS == 193


# ---------------------------------------------------------------------------
# Tests · 公式列
# ---------------------------------------------------------------------------


class TestExcelFormulaColumns:
    """公式列：合规(公式) / 命中条数(公式) / 末尾 SUM 合计。"""

    def test_compliance_formula_column_present(self, tmp_path: Path) -> None:
        """ROW_COLUMNS 含 '合规(公式)' 列。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "formula_compliance.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        first_sheet = wb[SECTION_BASIC]
        headers = [c.value for c in first_sheet[1]]
        assert "合规(公式)" in headers, (
            f"缺'合规(公式)'列，实际表头={headers}"
        )

    def test_compliance_formula_uses_if_function(self, tmp_path: Path) -> None:
        """'合规(公式)' 列写的是 IF(OR(E=confirmed, E=rule_checked, ...), 1, 0)。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "formula_if.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        first_sheet = wb[SECTION_BASIC]
        # 取第一行数据
        data_row_2 = list(first_sheet.iter_rows(min_row=2, max_row=2, values_only=False))[0]
        # 找到"合规(公式)"列
        compliance_col = None
        for col_idx, cell in enumerate(data_row_2, start=1):
            if cell.value and "IF" in str(cell.value) and "1,0" in str(cell.value):
                compliance_col = cell
                break
        assert compliance_col is not None, (
            f"未找到 IF 公式列，第一行数据={[(c.coordinate, c.value) for c in data_row_2 if c.value]}"
        )

    def test_hits_count_formula_uses_countif(self, tmp_path: Path) -> None:
        """'本分区命中条数(公式)' 用 COUNTIF(..., '<>')。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "formula_countif.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        first_sheet = wb[SECTION_BASIC]
        data_row_2 = list(first_sheet.iter_rows(min_row=2, max_row=2, values_only=False))[0]
        # 找到含 COUNTIF 的单元格
        countif_cell = None
        for cell in data_row_2:
            if cell.value and "COUNTIF" in str(cell.value):
                countif_cell = cell
                break
        assert countif_cell is not None, (
            f"未找到 COUNTIF 公式，第一行数据={[(c.coordinate, c.value) for c in data_row_2 if c.value]}"
        )

    def test_summary_sum_formula_present_at_section_bottom(
        self, tmp_path: Path
    ) -> None:
        """分区 sheet 末尾有 SUM/COUNTA 合计行。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "formula_sum.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        first_sheet = wb[SECTION_BASIC]
        # 找含 SUM( 公式的单元格
        sum_cell = None
        for row in first_sheet.iter_rows():
            for cell in row:
                if cell.value and "SUM(" in str(cell.value) and "COUNTA" not in str(cell.value):
                    sum_cell = cell
                    break
            if sum_cell is not None:
                break
        assert sum_cell is not None, "未找到 SUM 合计公式"


# ---------------------------------------------------------------------------
# Tests · 原文引用 sheet
# ---------------------------------------------------------------------------


class TestExcelSourceRefSheet:
    """_原文引用 sheet 含字段名 / 值 / span_start / span_end / 原文片段 / 状态。"""

    def test_source_ref_sheet_exists(self, tmp_path: Path) -> None:
        """_原文引用 sheet 存在。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "src_ref_exists.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path), read_only=True)
        assert "_原文引用" in wb.sheetnames, (
            f"缺 _原文引用 sheet，实际={wb.sheetnames}"
        )
        wb.close()

    def test_source_ref_columns_include_span_start_and_end(
        self, tmp_path: Path
    ) -> None:
        """_原文引用 列含 span_start / span_end。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "src_ref_span.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        ref = wb["_原文引用"]
        headers = [c.value for c in ref[1]]
        assert "span_start" in headers, (
            f"_原文引用 缺 span_start 列，实际={headers}"
        )
        assert "span_end" in headers, (
            f"_原文引用 缺 span_end 列，实际={headers}"
        )

    def test_source_ref_contains_field_name_value_status(
        self, tmp_path: Path
    ) -> None:
        """_原文引用 列含 字段名 / 值 / 状态 + 全部分抽取字段都有行。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "src_ref_fields.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        ref = wb["_原文引用"]
        headers = [c.value for c in ref[1]]
        for must_have in ("字段名", "值", "原文片段", "状态", "span_start", "span_end"):
            assert must_have in headers, (
                f"_原文引用 缺 {must_have} 列，实际={headers}"
            )

        # 抽到的 5 个字段都应该写入
        names_in_ref = [
            r[0] for r in ref.iter_rows(min_row=2, values_only=True) if r and r[0]
        ]
        for expected_name in (
            "入院日期",
            "出院日期",
            "性别",
            "主要手术操作名称",
            "金额合计",
        ):
            assert expected_name in names_in_ref, (
                f"_原文引用 缺字段行 {expected_name}，现有={names_in_ref}"
            )

    def test_source_ref_span_positions_match_input(self, tmp_path: Path) -> None:
        """_原文引用的 span_start/end 与 FieldMark 输入一致。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "src_ref_pos.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        ref = wb["_原文引用"]
        # 找"入院日期"行
        target_row = None
        for r in ref.iter_rows(min_row=2, values_only=True):
            if r and r[0] == "入院日期":
                target_row = r
                break
        assert target_row is not None
        # 列序：字段名(0) 值(1) span_start(2) span_end(3) 原文片段(4) 状态(5)
        assert target_row[2] == 5, f"span_start 应=5，实际={target_row[2]}"
        assert target_row[3] == 15, f"span_end 应=15，实际={target_row[3]}"
        assert target_row[1] == "2026-01-12", f"值错，实际={target_row[1]}"

    def test_source_ref_sorted_by_field_name(self, tmp_path: Path) -> None:
        """_原文引用 按字段名排序。"""
        doc = _make_doc_multisheet()
        out_path = tmp_path / "src_ref_sort.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        ref = wb["_原文引用"]
        names_in_ref = [
            r[0] for r in ref.iter_rows(min_row=2, values_only=True) if r and r[0]
        ]
        assert names_in_ref == sorted(names_in_ref), (
            f"_原文引用 未按字段名排序，实际={names_in_ref}"
        )


# ---------------------------------------------------------------------------
# Tests · 集成（多 sheet + 公式 + 原文引用 协同验证）
# ---------------------------------------------------------------------------


class TestExcelMultisheetIntegration:
    """多 sheet + 公式 + 原文引用 协同验证。"""

    def test_excel_openpyxl_round_trip_with_rule_set(self, tmp_path: Path) -> None:
        """rule_set 注入时，生成的 xlsx 能被 openpyxl 重新加载，无 IO 错误。"""
        rule_set = _load_rule_set(tmp_path)
        doc = _make_doc_multisheet()
        out_path = tmp_path / "round_trip.xlsx"
        ExcelExporter(rule_set=rule_set).export(doc, out_path)

        # 重新打开
        wb = load_workbook(str(out_path))
        sheet_names = wb.sheetnames
        assert len(sheet_names) >= 7, (
            f"应至少 7 sheet（4 分区 + 3 汇总），实际={len(sheet_names)}"
        )
        # 关掉释放
        wb.close()
        # 再以 read_only 模式打开
        wb2 = load_workbook(str(out_path), read_only=True)
        wb2.close()

    def test_excel_multipart_summary_sheet_lists_all_four_sections(
        self, tmp_path: Path
    ) -> None:
        """_覆盖_统计 sheet 列出全部 4 个分区名。"""
        rule_set = _load_rule_set(tmp_path)
        doc = _make_doc_multisheet()
        out_path = tmp_path / "summary.xlsx"
        ExcelExporter(rule_set=rule_set).export(doc, out_path)

        wb = load_workbook(str(out_path))
        cov = wb["_覆盖_统计"]
        # 第 A 列文本
        all_a_text = " ".join(
            str(c.value) for c in cov["A"] if c.value is not None
        )
        for sec in SECTION_ORDER:
            assert sec in all_a_text, (
                f"_覆盖_统计 缺分区行 {sec}，实际 A 列={all_a_text}"
            )
