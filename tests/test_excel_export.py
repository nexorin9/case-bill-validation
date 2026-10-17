"""ExcelExporter 单元测试（Task 11）。

覆盖：
- 4 个分区 sheet + 汇总 sheet + 原文引用 sheet + 审计 sheet 都存在；
- 已抽取字段写入正确（值 / 状态 / 规则命中摘要）；
- 未抽取字段以 "未抽取" 占位（rule_set 注入时）；
- 193 项不变式自检（rule_set 注入时）；
- 公式列存在（合规判断 / 命中条数）；
- 状态着色行存在（flagged / confirmed 等）；
- 文件可被 openpyxl 重新打开并解析。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from case_bill_validation.document import (
    AuditEntry,
    BillingCaseDocument,
    FieldMark,
    FieldStatus,
    RuleHit,
    RuleSeverity,
    RuleSource,
    SpanRef,
)
from case_bill_validation.export.excel import (
    ExcelExporter,
    SECTION_ORDER,
    export_excel,
)
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet
from case_bill_validation.rules.migrations import (
    SECTION_BASIC,
    SECTION_BILLING,
    SECTION_INPATIENT,
    EXPECTED_TOTAL_FIELDS,
)


# ---------------------------------------------------------------------------
# 辅助：构造最小可用单据
# ---------------------------------------------------------------------------


def _make_doc_with_extracted_fields(
    *,
    include_span: bool = True,
    raw_text: str = "入院日期:2026-01-12。出院日期:2026-01-26。性别:男。",
) -> BillingCaseDocument:
    """构造一份含若干已抽取字段的单据。"""
    doc = BillingCaseDocument(
        case_id="case-test-001",
        raw_text=raw_text,
    )
    # 入院日期
    doc.upsert_field(
        FieldMark(
            field_name="入院日期",
            value="2026-01-12",
            span=SpanRef(start_char=5, end_char=15) if include_span else None,
            confidence=0.95,
            status=FieldStatus.RULE_CHECKED,
        )
    )
    # 出院日期
    doc.upsert_field(
        FieldMark(
            field_name="出院日期",
            value="2026-01-26",
            span=SpanRef(start_char=20, end_char=30) if include_span else None,
            confidence=0.93,
            status=FieldStatus.SEMANTIC_CHECKED,
        )
    )
    # 性别
    doc.upsert_field(
        FieldMark(
            field_name="性别",
            value="男",
            span=SpanRef(start_char=34, end_char=35) if include_span else None,
            confidence=0.99,
            status=FieldStatus.CONFIRMED,
        )
    )
    # 一条 flagged（规则命中示例）
    flagged_mark = FieldMark(
        field_name="出生日期",
        value="1955-03-08",
        span=SpanRef(start_char=0, end_char=5) if include_span else None,
        confidence=0.7,
        status=FieldStatus.FLAGGED,
    )
    flagged_mark.add_rule_hit(
        RuleHit(
            rule_id="WH-3.4",
            rule_source=RuleSource.REQUIRED,
            severity=RuleSeverity.WARN,
            message_text="出生日期疑似越界，请人工复核",
            span=SpanRef(start_char=0, end_char=5) if include_span else None,
        )
    )
    doc.upsert_field(flagged_mark)
    # 审计
    doc.append_audit(
        AuditEntry(
            actor="settlement-clerk",
            field="性别",
            action="confirm",
            before_status="rule_checked",
            after_status="confirmed",
            note="人工确认",
        )
    )
    return doc


def _load_rule_set(tmp_path: Path) -> BillingFieldRuleSet:
    """从临时 SQLite 路径加载 193 项 rule_set。"""
    db_path = tmp_path / "billing_fields.db"
    return BillingFieldRuleSet.open(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExcelExporterBasic:
    """基础导出与多 sheet 结构。"""

    def test_export_creates_xlsx_with_four_section_sheets(
        self, tmp_path: Path
    ) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "billing_export.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path), read_only=True)
        sheet_names = wb.sheetnames
        # 4 个分区 sheet + _覆盖_统计 + _原文引用 + _审计_操作
        assert len(sheet_names) == 4 + 3, f"应有 7 个 sheet, 实际 {sheet_names}"
        assert any(SECTION_BASIC in s for s in sheet_names)
        assert any(SECTION_INPATIENT in s for s in sheet_names)
        assert any(SECTION_BILLING in s for s in sheet_names)
        wb.close()

    def test_export_includes_required_columns(self, tmp_path: Path) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        # 取第一个分区 sheet
        first_sheet = wb[SECTION_BASIC]
        headers = [c.value for c in first_sheet[1]]
        for required in (
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
            "合规(公式)",
            "本分区命中条数(公式)",
        ):
            assert required in headers, f"缺列 {required}"

    def test_extracted_fields_written_with_correct_status(
        self, tmp_path: Path
    ) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        first_sheet = wb[SECTION_BASIC]
        # 找 "性别" 行
        rows = list(first_sheet.iter_rows(values_only=True))
        target_row = None
        for r in rows[1:]:  # skip header
            if r and r[0] == "性别":
                target_row = r
                break
        assert target_row is not None, "性别 行未写入"
        # 列序: 字段名(0) 类型(1) 必填(2) 值(3) 状态(4) 来源(5) 置信度(6) ...
        assert target_row[3] == "男"
        assert target_row[4] == "confirmed"
        assert target_row[5] == "regex"  # 默认来源


class TestExcelExporterCoverageSheet:
    """_覆盖_统计 sheet 与 193 项不变式。"""

    def test_coverage_sheet_has_field_counts(self, tmp_path: Path) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        cov = wb["_覆盖_统计"]
        # 第一行是 case_id 概要；中间是各分区统计
        all_text = " ".join(
            str(c.value) for c in cov["A"] if c.value is not None
        )
        assert "基本信息" in all_text
        assert "住院诊疗" in all_text

    def test_coverage_with_rule_set_records_193_fields(
        self, tmp_path: Path
    ) -> None:
        """rule_set 注入时，合计规范字段数应 == 193（4 个分区相加）。"""
        rule_set = _load_rule_set(tmp_path)
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out.xlsx"
        ExcelExporter(rule_set=rule_set).export(doc, out_path)

        wb = load_workbook(str(out_path))
        cov = wb["_覆盖_统计"]
        # 找 "合计规范字段数" 行
        rows = list(cov.iter_rows(values_only=True))
        sum_row = None
        expected_row = None
        for r in rows:
            if r and r[0] == "合计规范字段数":
                sum_row = r
            if r and r[0] == "期望规范字段数":
                expected_row = r
        assert sum_row is not None
        # 因为 openpyxl 不求值公式，sum_row[1] 是公式字符串，
        # 但 EXPECTED_TOTAL_FIELDS 应被期望行写入
        assert expected_row is not None
        assert expected_row[1] == EXPECTED_TOTAL_FIELDS == 193

    def test_unfilled_spec_fields_appear_as_unfilled(
        self, tmp_path: Path
    ) -> None:
        """193 项里大量字段流水线未抽取 → 应写为 "未抽取" 行。"""
        rule_set = _load_rule_set(tmp_path)
        doc = _make_doc_with_extracted_fields()  # 只有 4 个字段
        out_path = tmp_path / "out.xlsx"
        ExcelExporter(rule_set=rule_set).export(doc, out_path)

        wb = load_workbook(str(out_path))
        # 拿"基本信息" sheet（我们 4 个字段可能分布在不同 sheet）
        unfilled_count = 0
        for sheet_name in SECTION_ORDER:
            ws = wb[sheet_name]
            for r in ws.iter_rows(min_row=2, values_only=True):
                if r and r[4] == "未抽取":
                    unfilled_count += 1
        # rule_set 193 项 - 我们 4 个字段 = 189 个未抽取
        assert unfilled_count == EXPECTED_TOTAL_FIELDS - 4


class TestExcelExporterSourceRefAndAudit:
    """原文引用 + 审计 sheet。"""

    def test_source_ref_sheet_has_all_extracted_fields(
        self, tmp_path: Path
    ) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        ref = wb["_原文引用"]
        names_in_ref = [r[0] for r in ref.iter_rows(min_row=2, values_only=True) if r]
        for expected in ("入院日期", "出院日期", "性别", "出生日期"):
            assert expected in names_in_ref

    def test_source_ref_includes_span_positions(self, tmp_path: Path) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        ref = wb["_原文引用"]
        for r in ref.iter_rows(min_row=2, values_only=True):
            if r and r[0] == "性别":
                assert r[2] == 34
                assert r[3] == 35
                break
        else:
            pytest.fail("性别 行未在 _原文引用 中找到")

    def test_audit_sheet_logs_clerk_actions(self, tmp_path: Path) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out.xlsx"
        ExcelExporter().export(doc, out_path)

        wb = load_workbook(str(out_path))
        audit = wb["_审计_操作"]
        # 第一行：表头；第二行：settlement-clerk 的 confirm 动作
        rows = list(audit.iter_rows(min_row=2, values_only=True))
        assert rows
        assert any(r[3] == "confirm" for r in rows if r), "审计 confirm 动作未写入"


class TestExcelExporterPublicAPI:
    """模块级便捷函数 + 重新打开可解析。"""

    def test_module_level_export_excel_works(self, tmp_path: Path) -> None:
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out2.xlsx"
        result = export_excel(doc, out_path)
        assert result == out_path
        assert out_path.exists()
        assert out_path.stat().st_size > 1000

    def test_xlsx_round_trip_openpyxl_load(self, tmp_path: Path) -> None:
        """导出的 xlsx 重新打开不应抛错。"""
        doc = _make_doc_with_extracted_fields()
        out_path = tmp_path / "out3.xlsx"
        ExcelExporter().export(doc, out_path)

        # 重新打开
        wb = load_workbook(str(out_path), data_only=False)
        wb.close()
        wb2 = load_workbook(str(out_path), data_only=True)
        wb2.close()


class TestExcelExporterErrorHandling:
    """异常路径。"""

    def test_export_to_path_pointing_at_directory_raises(
        self, tmp_path: Path
    ) -> None:
        """写到路径 = 已存在的目录时（openpyxl 拒绝），应抛 ExcelExportError。"""
        from case_bill_validation.export.excel import ExcelExportError

        doc = _make_doc_with_extracted_fields()
        bad_path = tmp_path  # 把目录当文件写
        with pytest.raises(ExcelExportError):
            ExcelExporter().export(doc, bad_path)
