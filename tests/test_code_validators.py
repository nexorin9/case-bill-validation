"""rules/code_validators.py（Task 24）+ 与 RuleCheckerProcessor 集成测试。

覆盖：

1. **validate_icd10 合法样本**：A01.01 / J18.901 / I50.905 / K80.801 / I25.101 /
   无小数点（A00 / Z99）；
2. **validate_icd10 非法样本**：小写字母 / 字母后只有 1 位数字 / 3 位数字无小数点 /
   3 位小数 / 超过 2 位小数 / 非字母开头 / None / 空字符串 / 非字符串 / 全空白；
3. **validate_icd9_cm3 合法样本**：00.01 / 47.0901 / 36.06 / 99.99 / 无小数点（00 / 99）；
4. **validate_icd9_cm3 非法样本**：单数字 / 3 位数字无小数点 / 字母前缀 /
   3 位小数 / 超过 2 位小数 / None / 空字符串 / 非字符串；
5. **validate_code 路由**：icd10 → validate_icd10；icd9_cm3 → validate_icd9_cm3；
   未知类型 → UnknownICDCodeType；
6. **validation_error_message**：含字段名 + 编码字面量 + 合法格式提示；
7. **RuleCheckerProcessor 集成**：icd10/icd9_cm3 字段失败 → status=flagged +
   rule_source=invalid_code + rule_id='invalid_code:{fname}' + 错误消息含合法格式提示；
   icd10/icd9_cm3 字段合法 → status=rule_checked；
8. **审计流水**：audit_log 含 icd_code_errors 统计字段。
"""

from __future__ import annotations

import pytest

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldMark,
    FieldSource,
    FieldStatus,
    RuleSeverity,
    RuleSource,
)
from case_bill_validation.pipeline.rule_checker import RuleCheckerProcessor
from case_bill_validation.rules.code_validators import (
    ICD9_CM3_PATTERN,
    ICD10_PATTERN,
    ICD_CODE_REGISTRY,
    UnknownICDCodeType,
    validate_code,
    validate_icd10,
    validate_icd9_cm3,
    validation_error_message,
)
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet
from case_bill_validation.rules.migrations import (
    FieldSpec,
    SECTION_BASIC,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_rule_set() -> BillingFieldRuleSet:
    specs = [
        FieldSpec("主要诊断代码", SECTION_BASIC, "icd10", True, "", "", "", "I25.101"),
        FieldSpec("其他诊断代码", SECTION_BASIC, "icd10", False, "", "", "", "I10"),
        FieldSpec(
            "主要手术操作代码",
            SECTION_BASIC,
            "icd9_cm3",
            True,
            "",
            "",
            "",
            "36.06",
        ),
        FieldSpec(
            "其他手术操作代码",
            SECTION_BASIC,
            "icd9_cm3",
            False,
            "",
            "",
            "",
            "47.0901",
        ),
    ]
    return BillingFieldRuleSet.from_specs(specs)


@pytest.fixture
def rule_set() -> BillingFieldRuleSet:
    return _make_rule_set()


def _mk(name: str, value, status: FieldStatus = FieldStatus.EXTRACTING) -> FieldMark:
    return FieldMark(
        field_name=name,
        value=value,
        status=status,
        source=FieldSource.REGEX,
        confidence=0.9,
    )


def _make_doc(fields: dict[str, FieldMark]) -> BillingCaseDocument:
    doc = BillingCaseDocument(case_id="test-code-validators", raw_text="raw")
    for name, mark in fields.items():
        doc.upsert_field(mark)
    return doc


# ---------------------------------------------------------------------------
# 1. validate_icd10 合法样本
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "A01.01",
        "J18.901",
        "I50.905",
        "K80.801",
        "I25.101",
        "A00",
        "Z99",
        "B15.9",
        "C50.001",
        "I10",
    ],
)
def test_validate_icd10_accepts_legal_codes(code: str):
    assert validate_icd10(code) is True, f"应接受 {code!r}"


# ---------------------------------------------------------------------------
# 2. validate_icd10 非法样本
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "a01.01",      # 小写字母开头
        "A1.01",       # 字母后只有 1 位数字
        "A001",        # 字母后 3 位数字无小数点
        "A01.0123",    # 超过 3 位小数
        "1A01",        # 非字母开头
        "",            # 空字符串
        "   ",         # 全空白
        None,          # None
        123,           # 非字符串
        ["I50.905"],   # list
        "A01.O1",      # 字母 O 不是数字
        "A.0101",      # 字母后没有数字
        ".A01",        # 起始小数点
    ],
)
def test_validate_icd10_rejects_illegal_codes(code):
    assert validate_icd10(code) is False, f"应拒绝 {code!r}"


# ---------------------------------------------------------------------------
# 3. validate_icd9_cm3 合法样本
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "00.01",
        "36.06",
        "99.99",
        "00",
        "99",
        "01.1",
        "85.5",
        "73.0",
        "00.001",
        "47.090",
    ],
)
def test_validate_icd9_cm3_accepts_legal_codes(code: str):
    assert validate_icd9_cm3(code) is True, f"应接受 {code!r}"


# ---------------------------------------------------------------------------
# 4. validate_icd9_cm3 非法样本
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "1",           # 单数字
        "100",         # 3 位数字无小数点
        "A00.66",      # 字母前缀
        "a01",         # 小写字母前缀
        "01.0123",     # 超过 3 位小数
        "",            # 空字符串
        "   ",         # 全空白
        None,          # None
        36.06,         # float
        {"code": "00"},  # dict
        "01.",         # 末尾孤立小数点
        ".01",         # 起始小数点
    ],
)
def test_validate_icd9_cm3_rejects_illegal_codes(code):
    assert validate_icd9_cm3(code) is False, f"应拒绝 {code!r}"


# ---------------------------------------------------------------------------
# 5. validate_code 路由
# ---------------------------------------------------------------------------


def test_validate_code_routes_icd10():
    assert validate_code("I50.905", "icd10") is True
    assert validate_code("a01.01", "icd10") is False


def test_validate_code_routes_icd9_cm3():
    assert validate_code("36.06", "icd9_cm3") is True
    assert validate_code("A00.66", "icd9_cm3") is False


def test_validate_code_unknown_type_raises():
    with pytest.raises(UnknownICDCodeType) as excinfo:
        validate_code("I50.905", "icd11")
    assert "icd11" in str(excinfo.value)
    assert "icd10" in str(excinfo.value)
    assert "icd9_cm3" in str(excinfo.value)


def test_icd_code_registry_has_both_validators():
    assert "icd10" in ICD_CODE_REGISTRY
    assert "icd9_cm3" in ICD_CODE_REGISTRY
    assert ICD_CODE_REGISTRY["icd10"] is validate_icd10
    assert ICD_CODE_REGISTRY["icd9_cm3"] is validate_icd9_cm3


# ---------------------------------------------------------------------------
# 6. validation_error_message
# ---------------------------------------------------------------------------


def test_validation_error_message_icd10_contains_hint():
    msg = validation_error_message("主要诊断代码", "i21.401", "icd10")
    assert "主要诊断代码" in msg
    assert "i21.401" in msg
    assert "icd10" in msg
    assert "字母" in msg  # 含合法格式提示
    assert "I50.905" in msg or "I50" in msg  # 至少含示例


def test_validation_error_message_icd9_cm3_contains_hint():
    msg = validation_error_message("主要手术操作代码", "A00.66", "icd9_cm3")
    assert "主要手术操作代码" in msg
    assert "A00.66" in msg
    assert "icd9_cm3" in msg
    assert "数字" in msg  # 含合法格式提示
    assert "36.06" in msg or "36" in msg  # 至少含示例


def test_validation_error_message_unknown_type_graceful():
    """未知类型仍返回字符串（含兜底提示），不抛异常。"""
    msg = validation_error_message("字段X", "ABC", "icd11")
    assert "字段X" in msg
    assert "ABC" in msg


# ---------------------------------------------------------------------------
# 7. RuleCheckerProcessor 集成
# ---------------------------------------------------------------------------


def test_processor_invalid_icd10_flags_with_invalid_code_source(rule_set):
    """小写字母开头的 ICD-10 应被 flag，且 rule_source=invalid_code。"""
    doc = _make_doc({"主要诊断代码": _mk("主要诊断代码", "i21.401")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要诊断代码" in out.flagged_field_names()
    # rule_hits 必须含 RuleSource.INVALID_CODE
    invalid_code_hits = [
        h for h in out.rule_hits if h.rule_source == RuleSource.INVALID_CODE
    ]
    assert any(h.rule_id == "invalid_code:主要诊断代码" for h in invalid_code_hits)
    # 错误消息含合法格式提示
    hit = next(
        h for h in invalid_code_hits
        if h.rule_id == "invalid_code:主要诊断代码"
    )
    assert "字母" in hit.message_text
    # 状态推进：EXTRACTING → FLAGGED
    assert out.extracted_fields["主要诊断代码"].status == FieldStatus.FLAGGED


def test_processor_invalid_icd10_wrong_length_flags_with_invalid_code_source(rule_set):
    """A1 单数字 ICD-10 应被 flag。"""
    doc = _make_doc({"主要诊断代码": _mk("主要诊断代码", "A1")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要诊断代码" in out.flagged_field_names()
    invalid_code_hits = [
        h for h in out.rule_hits if h.rule_source == RuleSource.INVALID_CODE
    ]
    assert any(h.rule_id == "invalid_code:主要诊断代码" for h in invalid_code_hits)


def test_processor_invalid_icd9_cm3_with_letter_prefix_flags(rule_set):
    """字母前缀 ICD-9-CM-3 应被 flag。"""
    doc = _make_doc({"主要手术操作代码": _mk("主要手术操作代码", "A00.66")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要手术操作代码" in out.flagged_field_names()
    invalid_code_hits = [
        h for h in out.rule_hits if h.rule_source == RuleSource.INVALID_CODE
    ]
    assert any(
        h.rule_id == "invalid_code:主要手术操作代码" for h in invalid_code_hits
    )


def test_processor_invalid_icd9_cm3_too_short_flags(rule_set):
    """单数字 ICD-9-CM-3 应被 flag。"""
    doc = _make_doc({"主要手术操作代码": _mk("主要手术操作代码", "1")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要手术操作代码" in out.flagged_field_names()


def test_processor_legal_icd10_advances_to_rule_checked(rule_set):
    """合法 ICD-10 字段应推进到 RULE_CHECKED，无 invalid_code hit。"""
    doc = _make_doc({"主要诊断代码": _mk("主要诊断代码", "I25.101")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要诊断代码" not in out.flagged_field_names()
    assert out.extracted_fields["主要诊断代码"].status == FieldStatus.RULE_CHECKED
    invalid_code_hits = [
        h for h in out.rule_hits if h.rule_source == RuleSource.INVALID_CODE
    ]
    assert not any(
        h.rule_id == "invalid_code:主要诊断代码" for h in invalid_code_hits
    )


def test_processor_legal_icd9_cm3_advances_to_rule_checked(rule_set):
    """合法 ICD-9-CM-3 字段应推进到 RULE_CHECKED。"""
    doc = _make_doc({"主要手术操作代码": _mk("主要手术操作代码", "36.06")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "主要手术操作代码" not in out.flagged_field_names()
    assert out.extracted_fields["主要手术操作代码"].status == FieldStatus.RULE_CHECKED


def test_processor_optional_invalid_icd10_flags(rule_set):
    """选填字段非法 ICD-10 也应被 flag（validity 与必填无关）。"""
    doc = _make_doc({"其他诊断代码": _mk("其他诊断代码", "12345")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    assert "其他诊断代码" in out.flagged_field_names()
    invalid_code_hits = [
        h for h in out.rule_hits if h.rule_source == RuleSource.INVALID_CODE
    ]
    assert any(h.rule_id == "invalid_code:其他诊断代码" for h in invalid_code_hits)


def test_processor_blank_icd10_skips_code_check(rule_set):
    """空值 ICD-10 不走 invalid_code（必填校验另负责）。"""
    doc = _make_doc({"主要诊断代码": _mk("主要诊断代码", "")})
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    # 必填缺失标 REQUIRED，不标 INVALID_CODE
    invalid_code_hits = [
        h for h in out.rule_hits if h.rule_source == RuleSource.INVALID_CODE
    ]
    assert not invalid_code_hits
    required_hits = [
        h for h in out.rule_hits if h.rule_source == RuleSource.REQUIRED
    ]
    assert any(h.rule_id == "required:主要诊断代码" for h in required_hits)


# ---------------------------------------------------------------------------
# 8. 审计流水
# ---------------------------------------------------------------------------


def test_audit_log_records_icd_code_errors_count(rule_set):
    """audit_log.note 含 icd_code_errors 计数。"""
    doc = _make_doc(
        {
            "主要诊断代码": _mk("主要诊断代码", "i21.401"),  # 非法
            "其他诊断代码": _mk("其他诊断代码", "12345"),     # 非法
            "主要手术操作代码": _mk("主要手术操作代码", "A00.66"),  # 非法
            "其他手术操作代码": _mk("其他手术操作代码", "36.06"),  # 合法
        }
    )
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    audit_entry = out.audit_log[-1]
    assert audit_entry.actor == "rule_checker"
    assert "icd_code_errors=3" in audit_entry.note


def test_audit_log_no_icd_errors_when_all_valid(rule_set):
    """全部合法时审计记 icd_code_errors=0。"""
    doc = _make_doc(
        {
            "主要诊断代码": _mk("主要诊断代码", "I25.101"),
            "其他诊断代码": _mk("其他诊断代码", "I10"),
            "主要手术操作代码": _mk("主要手术操作代码", "36.06"),
            "其他手术操作代码": _mk("其他手术操作代码", "47.090"),
        }
    )
    proc = RuleCheckerProcessor(rule_set=rule_set)
    out = proc.process(doc)
    audit_entry = out.audit_log[-1]
    assert "icd_code_errors=0" in audit_entry.note


# ---------------------------------------------------------------------------
# 9. 模式常量
# ---------------------------------------------------------------------------


def test_icd10_pattern_compiled():
    # 合法 ICD-10 应匹配
    assert ICD10_PATTERN.match("I50.905") is not None
    assert ICD10_PATTERN.match("A00") is not None
    # 非法 ICD-10 不匹配
    assert ICD10_PATTERN.match("i50.905") is None  # 小写
    assert ICD10_PATTERN.match("A1") is None         # 字母后只 1 位数字
    assert ICD10_PATTERN.match("1A01") is None       # 非字母开头


def test_icd9_cm3_pattern_compiled():
    assert ICD9_CM3_PATTERN.match("36.06") is not None
    assert ICD9_CM3_PATTERN.match("A00.66") is None
    assert ICD9_CM3_PATTERN.match("1") is None
