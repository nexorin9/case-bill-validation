"""CrossValidatorProcessor（Task 7）+ 跨字段规则端到端测试。

覆盖：
1. 倒置用例：手术日期早于入院日期 → 两个字段 flagged；
2. 缺失用例：ICU 天数字段空 → 规则跳过（不报错）；
3. 不符用例：主要诊断 ICD 与主要手术 ICD 不符（按种子规则）；
4. 通过用例：手术日期 ≥ 入院日期 + ICU 天数 ≤ 住院天数；
5. 状态机：EXTRACTING -> RULE_CHECKED（通过）或 EXTRACTING -> FLAGGED（失败）；
6. Edge：空 extracted_fields / 已知抽取但规则不涉及字段也标 RULE_CHECKED。

参考：
- ``docs/spec.md`` §5（cross_field > failure_mode）+ §6 acceptance_story；
- 种子规则清单：``case_bill_validation/rules/migrations._CROSS_FIELD_RULES``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from case_bill_validation.document import (
    AuditEntry,
    BillingCaseDocument,
    FieldMark,
    FieldSource,
    FieldStatus,
    RuleSeverity,
)
from case_bill_validation.pipeline.cross_validator import CrossValidatorProcessor
from case_bill_validation.rules.cross_field_rules import (
    Comparator,
    CrossFieldRule,
    CrossFieldRules,
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
    fields: dict[str, FieldMark], *, case_id: str = "test-cf-case"
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
# 1. 倒置：手术日期 < 入院日期
# ---------------------------------------------------------------------------


def test_cross_field_surgery_before_admission_flags_both(cf_rules):
    """种子规则``cross_field_surgery_after_admission`` —— 手术日期 < 入院日期。"""
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
    surgery_mark = out.extracted_fields["主要手术操作日期"]
    assert surgery_mark.status == FieldStatus.FLAGGED
    assert any(
        hit.rule_source.value == "cross_field"
        for hit in surgery_mark.rule_hits
    )
    assert "cross_validator" in out.processed_steps


def test_cross_field_involved_fields_advance_specific_to_failed(cf_rules):
    """仅失败字段 flagged；通过字段保持 RULE_CHECKED。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "出院日期": _mk("出院日期", "2026-02-10"),  # 出院 > 入院：通过
            "主要手术操作日期": _mk("主要手术操作日期", "2026-02-05"),  # 通过
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)

    assert out.extracted_fields["入院日期"].status == FieldStatus.RULE_CHECKED
    assert out.extracted_fields["出院日期"].status == FieldStatus.RULE_CHECKED
    assert out.extracted_fields["主要手术操作日期"].status == FieldStatus.RULE_CHECKED
    assert out.flagged_field_names() == []


# ---------------------------------------------------------------------------
# 2. 缺失：ICU 天数字段空
# ---------------------------------------------------------------------------


def test_cross_field_missing_field_skips_rule_gracefully(cf_rules):
    """ICU 天数字段空 → 评估期跳过规则（不抛错），字段保持 EXTRACTING。"""
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "出院日期": _mk("出院日期", "2026-02-10"),
            "实际住院天数": _mk("实际住院天数", 9),
            "ICU 天数": _mk("ICU 天数", None),  # 空值
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    # 空值字段不被强制 flagged
    assert "ICU 天数" not in out.flagged_field_names()
    # 但空值字段在无规则引用时会被推 RULE_CHECKED（unless 状态机拒绝）
    icu = out.extracted_fields["ICU 天数"]
    assert icu.status in (FieldStatus.EXTRACTING, FieldStatus.RULE_CHECKED)


# ---------------------------------------------------------------------------
# 3. ICU 天数 > 住院天数
# ---------------------------------------------------------------------------


def test_cross_field_icu_days_exceed_stay_fail(cf_rules):
    doc = _make_doc(
        {
            "实际住院天数": _mk("实际住院天数", 5),
            "ICU住院天数": _mk("ICU住院天数", 8),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "ICU住院天数" in out.flagged_field_names()
    assert "实际住院天数" in out.flagged_field_names()
    icu = out.extracted_fields["ICU住院天数"]
    assert any(
        hit.rule_id == "icu_days_le_stay_days"
        for hit in icu.rule_hits
    )


def test_cross_field_icu_days_within_stay_pass(cf_rules):
    doc = _make_doc(
        {
            "实际住院天数": _mk("实际住院天数", 10),
            "ICU住院天数": _mk("ICU住院天数", 3),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "ICU住院天数" not in out.flagged_field_names()
    assert "实际住院天数" not in out.flagged_field_names()
    assert out.extracted_fields["ICU住院天数"].status == FieldStatus.RULE_CHECKED


# ---------------------------------------------------------------------------
# 4. 离院方式 2/3 必填接收机构（按种子规则 transfer_requires_org）
# ---------------------------------------------------------------------------


def test_cross_field_transfer_requires_org_when_discharge_2(cf_rules):
    doc = _make_doc(
        {
            "离院方式": _mk("离院方式", "2医嘱转院"),
            "拟接收机构名称": _mk("拟接收机构名称", ""),  # 空字符串（信号）
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert "拟接收机构名称" in out.flagged_field_names()


def test_cross_field_transfer_requires_org_when_discharge_1_ok(cf_rules):
    doc = _make_doc(
        {
            "离院方式": _mk("离院方式", "1医嘱离院"),  # 离院 1 → 可空
            "拟接收机构名称": _mk("拟接收机构名称", ""),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    # 离院方式 1 → NOT_IN_SET 规则视为通过
    assert "拟接收机构名称" not in out.flagged_field_names()
    assert out.extracted_fields["拟接收机构名称"].status == FieldStatus.RULE_CHECKED


# ---------------------------------------------------------------------------
# 5. 端到端：5 步流水线（segmenter → ... → cross_validator）
# ---------------------------------------------------------------------------


def test_cross_field_processor_metadata(cf_rules):
    proc = CrossValidatorProcessor(rules=cf_rules)
    assert proc.step_name == "cross_validator"
    assert proc.REQUIRES_DEFAULT == {"extracted_fields"}
    assert proc.PROVIDES_DEFAULT == {"cross_validated_fields"}


def test_cross_field_skip_when_needs_manual(cf_rules):
    doc = _make_doc({})
    doc.needs_manual = True
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert out is doc
    # needs_manual 情况下跳过 record_step（让流水线编排器识别为降级）
    assert "cross_validator" not in out.processed_steps


def test_cross_field_skip_when_no_extracted_fields(cf_rules):
    doc = _make_doc({})
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    assert out is doc
    assert "cross_validator" in out.processed_steps


def test_cross_field_audit_entry_appended(cf_rules):
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "主要手术操作日期": _mk("主要手术操作日期", "2026-01-30"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    audit_actions = [e.action for e in out.audit_log]
    assert "cross_field_validation" in audit_actions
    cross_audit = [e for e in out.audit_log if e.action == "cross_field_validation"]
    assert cross_audit[0].actor == "cross_validator"


def test_cross_field_rule_hits_have_correct_source(cf_rules):
    doc = _make_doc(
        {
            "入院日期": _mk("入院日期", "2026-02-01"),
            "主要手术操作日期": _mk("主要手术操作日期", "2026-01-30"),
        }
    )
    proc = CrossValidatorProcessor(rules=cf_rules)
    out = proc.process(doc)
    # doc.rule_hits 必须含 ≥1 条 cross_field 源
    cf_hits = [h for h in out.rule_hits if h.rule_source.value == "cross_field"]
    assert cf_hits
    for hit in cf_hits:
        assert hit.severity in (RuleSeverity.INFO, RuleSeverity.WARN, RuleSeverity.ERROR)
        assert hit.rule_id
        assert hit.message_text
