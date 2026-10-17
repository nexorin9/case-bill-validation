"""Task 23 · 跨字段校验详细化测试。

覆盖 task 23 description 列出的 5 类规则，各 1-2 类反例 + 1 类正例：

1. **手术日期 ≥ 入院日期** （`>=`）：手术日期早于入院日期 → fail
2. **ICU 天数 ≤ 住院天数** （`<=`）：ICU 天数 > 住院天数 → fail
3. **主要诊断 ICD 与主要手术 ICD 相符性** （`icd_chapter_consistent`）：
   - I 循环诊断 + 35 心血管手术 → pass
   - K 消化诊断 + 35 心血管手术 → fail
4. **离院方式 2/3 时必填拟接收机构** （`not_in_set`）：
   - 离院 2 但缺接收机构 → fail
5. **实际住院天数 = 出院日期 - 入院日期** （`date_duration_equals`）：
   - 7 天 = 入院 02-01 出院 02-08 → pass
   - 填 5 天 → fail

参考：
- ``case_bill_validation/rules/migrations._CROSS_FIELD_RULES``
- ``spec.md`` §6 acceptance_story
"""

from __future__ import annotations

from pathlib import Path

import pytest

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldMark,
    FieldSource,
    FieldStatus,
)
from case_bill_validation.pipeline.cross_validator import CrossValidatorProcessor
from case_bill_validation.rules.cross_field_rules import (
    Comparator,
    CrossFieldRules,
    _parse_chapter_map,
)
from case_bill_validation.rules.migrations import init_billing_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cf_rules(tmp_path: Path) -> CrossFieldRules:
    db = tmp_path / "cf.db"
    init_billing_db(db)
    return CrossFieldRules.open(db)


def _make_doc(
    fields: dict[str, FieldMark], *, case_id: str = "test-cf-detail"
) -> BillingCaseDocument:
    doc = BillingCaseDocument(case_id=case_id, raw_text="出院小结正文" * 50)
    for name, mark in fields.items():
        doc.upsert_field(mark)
    return doc


def _mk(name: str, value, status: FieldStatus = FieldStatus.EXTRACTING) -> FieldMark:
    return FieldMark(
        field_name=name,
        value=value,
        status=status,
        source=FieldSource.REGEX,
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# 0. 元数据：种子规则总数 ≥ 10
# ---------------------------------------------------------------------------


def test_seed_rule_count_at_least_10(cf_rules):
    """task 23 要求种子规则 ≥ 5（实际 ≥ 10，含两条新增）。"""
    assert cf_rules.count() >= 10
    names = {r.name for r in cf_rules.all()}
    # 新增的两条规则必须在册
    assert "diagnosis_surgery_consistency" in names
    assert "actual_stay_days_match_dates" in names
    # 原有 8 条应保留
    for legacy in (
        "surgery_date_after_admission",
        "discharge_date_after_admission",
        "icu_days_le_stay_days",
        "bed_days_le_stay_days",
        "nursing_days_le_stay_days",
        "billing_period_within_stay",
        "billing_end_le_discharge",
        "transfer_requires_org",
    ):
        assert legacy in names, f"原种子规则 {legacy} 不应被移除"


def test_new_comparators_exported():
    """新增两个 comparator 常量在 Comparator.ALL 内。"""
    assert Comparator.DATE_DURATION_EQUALS == "date_duration_equals"
    assert Comparator.ICD_CHAPTER_CONSISTENT == "icd_chapter_consistent"
    assert Comparator.DATE_DURATION_EQUALS in Comparator.ALL
    assert Comparator.ICD_CHAPTER_CONSISTENT in Comparator.ALL


def test_parse_chapter_map_helper():
    """_parse_chapter_map 解析 'I:35,36|K:42,43' 为 dict。"""
    m = _parse_chapter_map("I:35,36|K:42,43,44")
    assert m == {"I": {"35", "36"}, "K": {"42", "43", "44"}}
    # 空字符串
    assert _parse_chapter_map("") == {}
    # 错误段被忽略
    assert "broken" not in _parse_chapter_map("broken_segment|I:01")


# ---------------------------------------------------------------------------
# 1. 手术日期 ≥ 入院日期（`>=`）
# ---------------------------------------------------------------------------


def test_detail_surgery_before_admission_flags_both(cf_rules):
    """反例：手术日期 2026-01-30 早于入院日期 2026-02-01 → 两字段 flagged。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "出院日期": _mk("出院日期", "2026-02-10"),
            "主要手术操作日期": _mk("主要手术操作日期", "2026-01-30"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "主要手术操作日期" in out.flagged_field_names()
    assert "入院日期" in out.flagged_field_names()
    surgery = out.extracted_fields["主要手术操作日期"]
    assert any(h.rule_id == "surgery_date_after_admission" for h in surgery.rule_hits)


def test_detail_surgery_after_admission_passes(cf_rules):
    """正例：手术日期 2026-02-05 晚于入院日期 2026-02-01 → 通过。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "出院日期": _mk("出院日期", "2026-02-10"),
            "主要手术操作日期": _mk("主要手术操作日期", "2026-02-05"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert out.flagged_field_names() == []
    assert out.extracted_fields["主要手术操作日期"].status == FieldStatus.RULE_CHECKED


# ---------------------------------------------------------------------------
# 2. ICU 天数 ≤ 住院天数（`<=`）
# ---------------------------------------------------------------------------


def test_detail_icu_days_exceed_stay_fail(cf_rules):
    """反例：ICU 8 天 > 住院 5 天 → flagged。"""
    doc = _make_doc(
        {
            "实际住院天数": _mk("实际住院天数", 5),
            "ICU住院天数": _mk("ICU住院天数", 8),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "ICU住院天数" in out.flagged_field_names()
    icu = out.extracted_fields["ICU住院天数"]
    assert any(h.rule_id == "icu_days_le_stay_days" for h in icu.rule_hits)
    assert any(h.severity.value == "error" for h in icu.rule_hits)


def test_detail_icu_days_equal_stay_passes(cf_rules):
    """正例：ICU 5 天 == 住院 5 天 → 通过（`<=` 含等于）。"""
    doc = _make_doc(
        {
            "实际住院天数": _mk("实际住院天数", 5),
            "ICU住院天数": _mk("ICU住院天数", 5),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert out.flagged_field_names() == []


# ---------------------------------------------------------------------------
# 3. 主要诊断 ICD 与主要手术 ICD 相符性（`icd_chapter_consistent`）
# ---------------------------------------------------------------------------


def test_detail_diagnosis_I_with_cardiac_surgery_passes(cf_rules):
    """正例：I25.101（冠状动脉粥样硬化）↔ 36.06（冠状动脉支架）→ 通过。"""
    doc = _make_doc(
        {
            "主要诊断代码": _mk("主要诊断代码", "I25.101"),
            "主要手术操作代码": _mk("主要手术操作代码", "36.06"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert out.flagged_field_names() == []


def test_detail_diagnosis_K_with_cardiac_surgery_fails(cf_rules):
    """反例：K80.801（胆结石）↔ 36.06（冠状动脉支架）→ flagged（章节不符）。"""
    doc = _make_doc(
        {
            "主要诊断代码": _mk("主要诊断代码", "K80.801"),
            "主要手术操作代码": _mk("主要手术操作代码", "36.06"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "主要诊断代码" in out.flagged_field_names()
    assert "主要手术操作代码" in out.flagged_field_names()
    diag = out.extracted_fields["主要诊断代码"]
    assert any(h.rule_id == "diagnosis_surgery_consistency" for h in diag.rule_hits)
    # 错误消息应包含诊断章节信息
    msg = next(
        h.message_text
        for h in diag.rule_hits
        if h.rule_id == "diagnosis_surgery_consistency"
    )
    assert "K" in msg or "章节" in msg or "不符" in msg


def test_detail_diagnosis_unknown_chapter_skipped(cf_rules):
    """边界：诊断 ICD 首字母不在映射内（如 'Z' 健康状态）→ 跳过（不报错）。"""
    doc = _make_doc(
        {
            "主要诊断代码": _mk("主要诊断代码", "Z00.000"),
            "主要手术操作代码": _mk("主要手术操作代码", "99.99"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    # Z 不在映射内 → 规则跳过 → 两字段标 RULE_CHECKED（不 flagged）
    assert "主要诊断代码" not in out.flagged_field_names()
    assert out.extracted_fields["主要诊断代码"].status == FieldStatus.RULE_CHECKED


# ---------------------------------------------------------------------------
# 4. 离院方式 2/3 时必填拟接收机构（`not_in_set`）
# ---------------------------------------------------------------------------


def test_detail_transfer_discharge_2_missing_org_fails(cf_rules):
    """反例：离院 2医嘱转院 但 拟接收机构名称 为空 → flagged。"""
    doc = _make_doc(
        {
            "离院方式": _mk("离院方式", "2医嘱转院"),
            "拟接收机构名称": _mk("拟接收机构名称", ""),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "拟接收机构名称" in out.flagged_field_names()
    org = out.extracted_fields["拟接收机构名称"]
    assert any(h.rule_id == "transfer_requires_org" for h in org.rule_hits)


def test_detail_transfer_discharge_1_with_empty_org_passes(cf_rules):
    """正例：离院 1医嘱离院 → 接收机构可空 → 通过。"""
    doc = _make_doc(
        {
            "离院方式": _mk("离院方式", "1医嘱离院"),
            "拟接收机构名称": _mk("拟接收机构名称", ""),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "拟接收机构名称" not in out.flagged_field_names()


def test_detail_transfer_discharge_3_with_filled_org_passes(cf_rules):
    """正例：离院 3医嘱转基层 + 接收机构有值 → 通过。"""
    doc = _make_doc(
        {
            "离院方式": _mk("离院方式", "3医嘱转基层医疗机构"),
            "拟接收机构名称": _mk("拟接收机构名称", "<EXAMPLE_RECEIVING_ORG>"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "拟接收机构名称" not in out.flagged_field_names()


# ---------------------------------------------------------------------------
# 5. 实际住院天数 = 出院日期 - 入院日期（`date_duration_equals`）
# ---------------------------------------------------------------------------


def test_detail_stay_days_7_dates_feb01_to_feb08_passes(cf_rules):
    """正例：实际住院 7 天 = 出院 02-08 - 入院 02-01 → 通过。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "出院日期": _mk("出院日期", "2026-02-08"),
            "实际住院天数": _mk("实际住院天数", 7),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert out.flagged_field_names() == []


def test_detail_stay_days_mismatch_fails(cf_rules):
    """反例：实际住院 5 天 ≠ 出院 02-08 - 入院 02-01 = 7 天 → flagged。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "出院日期": _mk("出院日期", "2026-02-08"),
            "实际住院天数": _mk("实际住院天数", 5),  # 错填
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "实际住院天数" in out.flagged_field_names()
    stay = out.extracted_fields["实际住院天数"]
    assert any(h.rule_id == "actual_stay_days_match_dates" for h in stay.rule_hits)
    # warn 级别（口径差异提醒，不阻断）
    assert any(h.severity.value == "warn" for h in stay.rule_hits)


def test_detail_stay_days_missing_discharge_date_skipped(cf_rules):
    """边界：出院日期缺失 → ref 字段解析失败 → 规则跳过（不报错）。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            # 出院日期 不存在
            "实际住院天数": _mk("实际住院天数", 7),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "实际住院天数" not in out.flagged_field_names()
    assert out.extracted_fields["实际住院天数"].status == FieldStatus.RULE_CHECKED


# ---------------------------------------------------------------------------
# 6. 综合：5 条规则同时存在，按字段名分组触发不同行为
# ---------------------------------------------------------------------------


def test_detail_five_rules_evaluated_independently(cf_rules):
    """综合：同时触发多条规则的混合字段表。"""
    doc = _make_doc(
        {
            # 规则 1：手术日期早于入院 → fail
            "入院日期": _mk("入院日期", "2026-03-10"),
            "主要手术操作日期": _mk("主要手术操作日期", "2026-03-05"),
            # 规则 2：ICU > 住院 → fail
            "实际住院天数": _mk("实际住院天数", 4),
            "ICU住院天数": _mk("ICU住院天数", 6),
            # 规则 3：诊断-手术章节不符 → fail
            "主要诊断代码": _mk("主要诊断代码", "K80.801"),
            "主要手术操作代码": _mk("主要手术操作代码", "36.06"),
            # 规则 4：离院 2 但缺接收机构 → fail
            "离院方式": _mk("离院方式", "2医嘱转院"),
            "拟接收机构名称": _mk("拟接收机构名称", ""),
            # 规则 5：实际住院天数对不上 → fail
            "出院日期": _mk("出院日期", "2026-03-12"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    flagged = out.flagged_field_names()
    # 5 条规则都触发 → 至少 8 个字段被 flagged（含每条规则的 rhs）
    for field in (
        "主要手术操作日期",
        "入院日期",
        "ICU住院天数",
        "实际住院天数",
        "主要诊断代码",
        "主要手术操作代码",
        "拟接收机构名称",
    ):
        assert field in flagged, f"{field} 应被 flagged，实际 {flagged}"


def test_detail_severity_routing(cf_rules):
    """严重度路由：error 类规则 → 字段 flagged；warn 类 → 字段也 flagged 但 severity=warn。"""
    # actual_stay_days_match_dates 是 warn
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-04-01"),
            "出院日期": _mk("出院日期", "2026-04-10"),
            "实际住院天数": _mk("实际住院天数", 3),  # 错填
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    stay = out.extracted_fields["实际住院天数"]
    assert stay.status == FieldStatus.FLAGGED
    # warn 不降为 RULE_CHECKED
    assert any(h.severity.value == "warn" for h in stay.rule_hits)


def test_detail_audit_summary_records_rule_count(cf_rules):
    """审计：跨字段校验完成后审计条目 actor=cross_validator + note 含 rules=N。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-05-01"),
            "出院日期": _mk("出院日期", "2026-05-08"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    cross_audits = [
        e for e in out.audit_log if e.action == "cross_field_validation"
    ]
    assert cross_audits
    assert "rules=" in cross_audits[0].note
    assert cross_audits[0].actor == "cross_validator"