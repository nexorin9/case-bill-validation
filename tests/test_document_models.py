"""``document.py`` 的模型与状态机测试。

覆盖：

1. ``SpanRef`` 边界 + 唯一 + 落到原文区间内取文本一致；
2. ``FieldMark`` 7 态状态的合法迁移路径；
3. ``BillingCaseDocument`` ``model_validate`` 加载脱敏样例；
4. ``validate_assignment=True`` 重新赋值仍触发校验；
5. 非法状态迁移抛 ``InvalidTransitionError``；
6. 字段 → 单据 → 审计流水 追加顺序；
7. ``field_status_counts`` / ``flagged_field_names`` 辅助 API。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from case_bill_validation.document import (
    ALLOWED_DOCUMENT_TRANSITIONS,
    ALLOWED_FIELD_TRANSITIONS,
    AuditEntry,
    BillingCaseDocument,
    DocumentState,
    FieldMark,
    FieldSource,
    FieldStatus,
    InvalidTransitionError,
    RuleHit,
    RuleSeverity,
    RuleSource,
    SpanRef,
    assert_document_transition,
    assert_field_transition,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TEXT = (
    "患者张三，男，58 岁，因「胸痛 3 天」于 2025-09-01 入院，"
    "出院日期 2025-09-10。入院诊断：急性前壁心肌梗死（ICD-10: I21.0）。"
    "出院诊断：冠心病。出院医嘱：长期口服阿司匹林，门诊随诊。"
)


@pytest.fixture()
def sample_doc() -> BillingCaseDocument:
    """一份带 3 个不同状态字段的单据。"""
    doc = BillingCaseDocument(case_id="case-2025-09-001", raw_text=SAMPLE_TEXT)
    doc.upsert_field(
        FieldMark(
            field_name="姓名",
            value="张三",
            span=SpanRef(start_char=2, end_char=4),
            confidence=1.0,
            status=FieldStatus.CONFIRMED,
            source=FieldSource.REGEX,
        )
    )
    doc.upsert_field(
        FieldMark(
            field_name="入院日期",
            value="2025-09-01",
            span=SpanRef(start_char=24, end_char=34),
            confidence=1.0,
            status=FieldStatus.PENDING,
            source=FieldSource.REGEX,
        )
    )
    doc.upsert_field(
        FieldMark(
            field_name="主要诊断ICD",
            value="I21.0",
            span=SpanRef(start_char=48, end_char=53),
            confidence=0.95,
            status=FieldStatus.FLAGGED,
            source=FieldSource.REGEX,
        )
    )
    return doc


# ---------------------------------------------------------------------------
# SpanRef
# ---------------------------------------------------------------------------


def test_span_ref_text_of_returns_substring():
    span = SpanRef(start_char=2, end_char=4)
    assert span.text_of(SAMPLE_TEXT) == "张三"


def test_span_ref_must_have_end_gt_start():
    with pytest.raises(ValidationError):
        SpanRef(start_char=5, end_char=5)


def test_span_ref_must_be_in_raw_text_on_document(sample_doc: BillingCaseDocument):
    doc = sample_doc
    # 改大 bad_span 让其越界，重新 model_validate 应抛错。
    doc.spans.append(SpanRef(start_char=10_000, end_char=10_001))
    with pytest.raises(ValidationError):
        # 由于 validate_assignment=True，重新赋值也走校验。
        BillingCaseDocument.model_validate(doc.model_dump())


def test_span_ref_is_frozen():
    """SpanRef 用 frozen=True 防止被悄悄篡改。"""
    span = SpanRef(start_char=0, end_char=4)
    with pytest.raises(ValidationError):
        span.start_char = 100  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FieldMark 状态机
# ---------------------------------------------------------------------------


def test_field_status_allowed_transitions_match_spec():
    assert ALLOWED_FIELD_TRANSITIONS[FieldStatus.PENDING] == {
        FieldStatus.EXTRACTING,
        FieldStatus.FLAGGED,
    }
    assert ALLOWED_FIELD_TRANSITIONS[FieldStatus.SEMANTIC_CHECKED] == {
        FieldStatus.CONFIRMED,
        FieldStatus.REJECTED,
        FieldStatus.FLAGGED,
    }
    # 终态不可再迁移。
    assert ALLOWED_FIELD_TRANSITIONS[FieldStatus.CONFIRMED] == set()
    assert ALLOWED_FIELD_TRANSITIONS[FieldStatus.REJECTED] == set()


def test_document_state_allowed_transitions_match_spec():
    assert ALLOWED_DOCUMENT_TRANSITIONS[DocumentState.DRAFT] == {
        DocumentState.IN_REVIEW,
    }
    assert ALLOWED_DOCUMENT_TRANSITIONS[DocumentState.CONFIRMED] == {
        DocumentState.EXPORTED,
    }
    assert ALLOWED_DOCUMENT_TRANSITIONS[DocumentState.SUBMITTED] == set()


def test_field_mark_happy_path_pending_to_extracting_to_rule_to_semantic_to_confirmed():
    f = FieldMark(field_name="诊断日期", value="2025-09-01")
    assert f.status == FieldStatus.PENDING
    f.mark_extracting().mark_rule_checked().mark_semantic_checked().confirm()
    assert f.status == FieldStatus.CONFIRMED


def test_field_mark_happy_path_can_be_rejected_at_semantic_stage():
    f = FieldMark(field_name="诊断日期", value="2025-09-01")
    f.mark_extracting().mark_rule_checked().mark_semantic_checked().reject()
    assert f.status == FieldStatus.REJECTED


def test_field_mark_illegal_transition_raises():
    f = FieldMark(field_name="诊断日期", value="x")
    # pending 直接跳 confirmed 是非法的。
    with pytest.raises(InvalidTransitionError):
        f.confirm()
    # pending 直跳 semantic_checked 也不合法（缺少 extracting & rule_checked）。
    with pytest.raises(InvalidTransitionError):
        f.mark_semantic_checked()


def test_assert_field_transition_helper_rejects_same_state():
    with pytest.raises(InvalidTransitionError):
        # 同态迁移不在 ALLOWED 集内（要显式推进，不允许"原地踏步"）。
        assert_field_transition(FieldStatus.PENDING, FieldStatus.PENDING)


def test_assert_field_transition_helper_accepts_known_path():
    # 不抛错即通过。
    assert_field_transition(FieldStatus.SEMANTIC_CHECKED, FieldStatus.CONFIRMED)
    assert_field_transition(FieldStatus.RULE_CHECKED, FieldStatus.FLAGGED)


# ---------------------------------------------------------------------------
# BillingCaseDocument
# ---------------------------------------------------------------------------


def test_billing_doc_loads_minimal_payload():
    doc = BillingCaseDocument.model_validate(
        {
            "case_id": "c-001",
            "raw_text": "hello world",
        }
    )
    assert doc.case_id == "c-001"
    assert doc.raw_text == "hello world"
    assert doc.pipeline_state == DocumentState.DRAFT
    assert doc.extracted_fields == {}
    assert doc.processed_steps == []


def test_billing_doc_with_extracts_roundtrip(sample_doc: BillingCaseDocument):
    dumped = sample_doc.model_dump()
    doc2 = BillingCaseDocument.model_validate(dumped)
    assert doc2.case_id == sample_doc.case_id
    assert doc2.extracted_fields["姓名"].value == "张三"
    assert doc2.extracted_fields["主要诊断ICD"].status == FieldStatus.FLAGGED


def test_billing_doc_validate_assignment_runs_field_validators():
    """``validate_assignment=True`` 让 pydantic 字段赋值重新走类型校验。

    直接把 ``pipeline_state`` 改到非法目标值，pydantic 允许同类型枚举的赋值
    （DocumentState 是合法枚举值），但**业务级状态机**由 ``advance_document``
    显式把关——这是有意为之：状态机不能被悄悄绕过。
    """
    doc = BillingCaseDocument(case_id="c-002", raw_text="短文本")
    # 同类型枚举赋值：pydantic 接受，业务上不鼓励。
    doc.pipeline_state = DocumentState.IN_REVIEW
    assert doc.pipeline_state == DocumentState.IN_REVIEW

    # 业务级状态机非法迁移仍由 advance_document 把关。
    with pytest.raises(InvalidTransitionError):
        doc.advance_document(DocumentState.SUBMITTED)


def test_billing_doc_field_span_out_of_bounds_rejected():
    # 字段 span 越界 - 端到端校验
    bad = {
        "case_id": "c-bad",
        "raw_text": "abc",
        "extracted_fields": {
            "姓名": {
                "field_name": "姓名",
                "value": "超长",
                "span": {"start_char": 0, "end_char": 1000},
                "status": "pending",
                "source": "regex",
            }
        },
    }
    with pytest.raises(ValidationError):
        BillingCaseDocument.model_validate(bad)


def test_billing_doc_advance_document_requires_valid_transition(sample_doc):
    sample_doc.advance_document(DocumentState.IN_REVIEW)
    assert sample_doc.pipeline_state == DocumentState.IN_REVIEW
    # draft → exported 跳过中间是非法。
    fresh = BillingCaseDocument(case_id="c-003", raw_text="x")
    with pytest.raises(InvalidTransitionError):
        fresh.advance_document(DocumentState.EXPORTED)


def test_billing_doc_append_audit_and_step_order(sample_doc):
    sample_doc.record_step("segmenting").record_step("extracting").record_step("extracting")
    assert sample_doc.processed_steps == ["segmenting", "extracting"]
    sample_doc.append_audit(
        AuditEntry(actor="auditor.li", action="advance", after_status="in_review")
    )
    assert len(sample_doc.audit_log) == 1
    assert sample_doc.audit_log[0].actor == "auditor.li"


def test_billing_doc_field_status_counts_and_flagged(sample_doc):
    counts = sample_doc.field_status_counts()
    assert counts["confirmed"] == 1
    assert counts["pending"] == 1
    assert counts["flagged"] == 1
    assert sample_doc.flagged_field_names() == ["主要诊断ICD"]


def test_rule_hit_inherits_timestamp_and_serializes():
    hit = RuleHit(
        rule_id="require_diagnosis_icd",
        rule_source=RuleSource.REQUIRED,
        severity=RuleSeverity.ERROR,
        message_text="主要诊断 ICD 必填",
    )
    assert hit.created_at is not None
    dumped = hit.model_dump()
    assert dumped["rule_source"] == "required"
    assert dumped["severity"] == "error"
