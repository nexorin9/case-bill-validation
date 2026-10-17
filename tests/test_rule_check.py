"""RuleCheckerProcessor（Task 7）+ 必填 / 类型 / 编码区间校验测试。

覆盖：
1. **必填缺失**：required=True 字段为空 → flagged + RuleHit(rule_source=required)；
2. **类型错误**：date / int / float / enum / str-非空 / icd10 / icd9_cm3；
3. **正向校验**：合规字段推进 RULE_CHECKED；
4. **状态机**：EXTRACTING -> RULE_CHECKED / EXTRACTING -> FLAGGED；
5. **审计流水**：audit_log 含 actor='rule_checker' + note。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldMark,
    FieldSource,
    FieldStatus,
    RuleSeverity,
    RuleSource,
)
from case_bill_validation.pipeline.rule_checker import (
    RuleCheckerProcessor,
    _validate_field_type,
)
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet
from case_bill_validation.rules.migrations import (
    FieldSpec,
    SECTION_BASIC,
    init_billing_db,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_minimal_rule_set():
    """手工构造的最小规则集（避免依赖 SQLite 启动期）。"""
    specs = [
        # 必填 + str 非空 + 正则约束
        FieldSpec("姓名", SECTION_BASIC, "str", True, "", "", "", "<EXAMPLE_NAME>"),
        FieldSpec("证件号码", SECTION_BASIC, "str", True, ".+", "", "", "110101199001011234"),
        # 必填 + enum
        FieldSpec("性别", SECTION_BASIC, "enum", True, "in:男,女,未说明", "", "", "男"),
        # 必填 + date
        FieldSpec("出生日期", SECTION_BASIC, "date", True, "^\\d{4}-\\d{2}-\\d{2}$", "", "", "1960-01-01"),
        # 必填 + int
        FieldSpec("年龄", SECTION_BASIC, "int", True, "range:0,150", "", "", "65"),
        # 必填 + float
        FieldSpec("新生儿出生体重", SECTION_BASIC, "float", False, "range:0.0,8.0", "", "", "3.20"),
        # 选填 + enum
        FieldSpec("职业", SECTION_BASIC, "enum", False, "in:公务员,职员,工人", "", "", "职员"),
        # icd10
        FieldSpec("主要诊断代码", SECTION_BASIC, "icd10", True, "", "", "", "I21.401"),
        # icd9_cm3
        FieldSpec("主要手术操作代码", SECTION_BASIC, "icd9_cm3", True, "", "", "", "00.66"),
    ]
    return BillingFieldRuleSet.from_specs(specs)


@pytest.fixture
def rule_set() -> BillingFieldRuleSet:
    return _make_minimal_rule_set()


def _make_doc(
    fields: dict[str, FieldMark], *, case_id: str = "test-rc-case"
) -> BillingCaseDocument:
    doc = BillingCaseDocument(case_id=case_id, raw_text="出院小结正文" * 30)
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
# 1. 必填缺失
# ---------------------------------------------------------------------------


def test_required_missing_value_flags_field(rule_set):
    doc = _make_doc(
        {
            "姓名": _mk("姓名", None),
            "证件号码": _mk("证件号码", ""),
            "性别": _mk("性别", "男"),
        }
    )
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "姓名" in out.flagged_field_names()
    assert "证件号码" in out.flagged_field_names()
    # 性别通过
    assert "性别" not in out.flagged_field_names()
    # 状态
    assert out.extracted_fields["姓名"].status == FieldStatus.FLAGGED
    assert out.extracted_fields["证件号码"].status == FieldStatus.FLAGGED
    assert out.extracted_fields["性别"].status == FieldStatus.RULE_CHECKED
    # rule_hits
    name_hits = [h for h in out.extracted_fields["姓名"].rule_hits if h.rule_source == RuleSource.REQUIRED]
    assert name_hits
    assert name_hits[0].severity == RuleSeverity.ERROR


def test_required_field_absent_in_doc(rule_set):
    """某 required 字段未抽取 → rule_checker 把缺失也写入 rule_hits。"""
    doc = _make_doc({"性别": _mk("性别", "男")})
    proc = RuleCheckerProcessor(rule_set=rule_set, known_extracted={"姓名", "证件号码", "性别"})
    out = proc.process(doc)
    # 缺"姓名" "证件号码"，应写 rule_hits 到 doc
    required_hits = [h for h in out.rule_hits if h.rule_source == RuleSource.REQUIRED]
    assert len(required_hits) >= 2
    rule_ids = {h.rule_id for h in required_hits}
    assert "required:姓名" in rule_ids
    assert "required:证件号码" in rule_ids


# ---------------------------------------------------------------------------
# 2. 类型错误
# ---------------------------------------------------------------------------


def test_type_error_date_wrong_format(rule_set):
    doc = _make_doc({"出生日期": _mk("出生日期", "2026/02/01")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "出生日期" in out.flagged_field_names()
    type_hit = next(
        h for h in out.extracted_fields["出生日期"].rule_hits
        if h.rule_source == RuleSource.TYPE_CHECK
    )
    assert "日期格式" in type_hit.message_text


def test_type_error_int_contains_chars(rule_set):
    doc = _make_doc({"年龄": _mk("年龄", "六十五")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "年龄" in out.flagged_field_names()


def test_type_error_float_out_of_range(rule_set):
    doc = _make_doc({"新生儿出生体重": _mk("新生儿出生体重", "12.5")})  # 越界 >8.0
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "新生儿出生体重" in out.flagged_field_names()


def test_type_error_enum_invalid_value(rule_set):
    doc = _make_doc({"性别": _mk("性别", "其他")})  # 不在枚举内
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "性别" in out.flagged_field_names()
    hit = next(
        h for h in out.extracted_fields["性别"].rule_hits
        if h.rule_source == RuleSource.TYPE_CHECK
    )
    assert "枚举值" in hit.message_text


def test_type_error_icd10_lowercase(rule_set):
    doc = _make_doc({"主要诊断代码": _mk("主要诊断代码", "i21.401")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要诊断代码" in out.flagged_field_names()


def test_type_error_icd10_wrong_length(rule_set):
    doc = _make_doc({"主要诊断代码": _mk("主要诊断代码", "A1")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要诊断代码" in out.flagged_field_names()


def test_type_error_icd9_cm3_prefixed_with_letter(rule_set):
    doc = _make_doc({"主要手术操作代码": _mk("主要手术操作代码", "A00.66")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要手术操作代码" in out.flagged_field_names()


# ---------------------------------------------------------------------------
# 3. 正向校验
# ---------------------------------------------------------------------------


def test_valid_inputs_advance_all_fields_to_rule_checked(rule_set):
    doc = _make_doc(
        {
            "姓名": _mk("姓名", "张三"),
            "证件号码": _mk("证件号码", "110101199001011234"),
            "性别": _mk("性别", "男"),
            "出生日期": _mk("出生日期", "1960-01-01"),
            "年龄": _mk("年龄", 65),
            "职业": _mk("职业", "职员"),
            "主要诊断代码": _mk("主要诊断代码", "I21.401"),
            "主要手术操作代码": _mk("主要手术操作代码", "00.66"),
        }
    )
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    for fmark in out.extracted_fields.values():
        assert fmark.status == FieldStatus.RULE_CHECKED
    assert out.flagged_field_names() == []


# ---------------------------------------------------------------------------
# 4. 状态机 / 边角
# ---------------------------------------------------------------------------


def test_processor_skip_when_needs_manual(rule_set):
    doc = _make_doc({})
    doc.needs_manual = True
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert out is doc
    # needs_manual 情况下跳过 record_step（让编排器识别为降级）
    assert "rule_checker" not in out.processed_steps


def test_processor_metadata(rule_set):
    proc = RuleCheckerProcessor(rule_set=rule_set)
    assert proc.step_name == "rule_checker"
    assert proc.REQUIRES_DEFAULT == {"extracted_fields"}
    assert proc.PROVIDES_DEFAULT == {"rule_checked_fields"}


def test_optional_missing_value_does_not_flag(rule_set):
    """选填字段空 → 不应 flagged；未抽取时也不会写缺失规则（除非 known_extracted）。"""
    doc = _make_doc(
        {
            "姓名": _mk("姓名", "张三"),
            "证件号码": _mk("证件号码", "110101199001011234"),
            "性别": _mk("性别", "男"),
            "出生日期": _mk("出生日期", "1960-01-01"),
            "年龄": _mk("年龄", 65),
            "职业": _mk("职业", None),  # 选填空
        }
    )
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "职业" not in out.flagged_field_names()


def test_audit_entry_appended(rule_set):
    doc = _make_doc({"姓名": _mk("姓名", "张三")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    actions = [e.action for e in out.audit_log if e.actor == "rule_checker"]
    assert "rule_check" in actions


def test_validate_field_type_helper():
    # 直接调用 helper
    assert _validate_field_type("x", "2026-01-01", "date", "") is None
    assert _validate_field_type("x", "2026/01/01", "date", "") is not None
    assert _validate_field_type("x", "3.14", "float", "") is None
    assert _validate_field_type("x", "abc", "float", "") is not None
    assert _validate_field_type("x", "65", "int", "") is None
    assert _validate_field_type("x", "65.0", "int", "") is not None
    assert _validate_field_type("x", "男", "enum", "in:男,女,未说明") is None
    assert _validate_field_type("x", "其他", "enum", "in:男,女,未说明") is not None
    assert _validate_field_type("x", "I21.401", "icd10", "") is None
    assert _validate_field_type("x", "i21.401", "icd10", "") is not None
    assert _validate_field_type("x", "00.66", "icd9_cm3", "") is None
    assert _validate_field_type("x", "A00.66", "icd9_cm3", "") is not None


def test_processed_steps_recorded(rule_set):
    doc = _make_doc({"姓名": _mk("姓名", "张三")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "rule_checker" in out.processed_steps
