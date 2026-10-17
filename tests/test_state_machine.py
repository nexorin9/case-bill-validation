"""流水线状态机非法迁移拒绝（字段 7 态 + 单据 5 态）。

覆盖：

1. 字段合法迁移路径（pending→extracting→rule_checked→semantic_checked→confirmed/rejected；
   +extracting/rule_checked→flagged；flagged→rule_checked/semantic_checked 回退）；
2. 字段非法迁移失败（≥5 类：pending→confirmed 跳步、extracting→semantic_checked 跳步、
   semantic_checked→rule_checked 回退、confirmed→任意非空拒绝、rejected→任意拒绝等）；
3. 单据 5 态合法迁移 + 非法迁移（≥5 类）；
4. ``assert_transition`` 通用入口按 ``from_state`` 类型自动派发字段/单据；
5. ``assert_field_transition`` / ``assert_document_transition`` 类型校验；
6. ``InvalidTransitionError`` 的 ``from_state`` / ``to_state`` / ``allowed``
   属性与异常 ``category``；
7. ``FieldMark.mark_*`` / ``BillingCaseDocument.advance_document`` 在状态机层
   拒绝时的传播；
8. ``is_field_terminal`` / ``is_document_terminal`` / ``allowed_field_next``
   / ``allowed_document_next`` 辅助 API。
"""

from __future__ import annotations

import pytest

from case_bill_validation.document import (
    BillingCaseDocument,
    DocumentState,
    FieldMark,
    FieldStatus,
    InvalidTransitionError,
)
from case_bill_validation.pipeline import state_machine as sm


# ---------------------------------------------------------------------------
# 字段 7 态合法迁移（基本通路 + 跳过分支 + 回退路径）
# ---------------------------------------------------------------------------


class TestFieldLegalTransitions:
    """字段 7 态全部合法迁移路径。"""

    def test_pending_to_extracting(self):
        sm.assert_field_transition(FieldStatus.PENDING, FieldStatus.EXTRACTING)

    def test_pending_to_flagged(self):
        """``pending`` 字段未抽取即标 flagged（异常 capture 路径）。"""
        sm.assert_field_transition(FieldStatus.PENDING, FieldStatus.FLAGGED)

    def test_extracting_to_rule_checked(self):
        sm.assert_field_transition(FieldStatus.EXTRACTING, FieldStatus.RULE_CHECKED)

    def test_extracting_to_flagged(self):
        """抽取失败直接 flagged，跳过规则审查。"""
        sm.assert_field_transition(FieldStatus.EXTRACTING, FieldStatus.FLAGGED)

    def test_rule_checked_to_semantic_checked(self):
        sm.assert_field_transition(
            FieldStatus.RULE_CHECKED, FieldStatus.SEMANTIC_CHECKED
        )

    def test_rule_checked_to_flagged(self):
        """规则审查失败直接 flagged。"""
        sm.assert_field_transition(FieldStatus.RULE_CHECKED, FieldStatus.FLAGGED)

    def test_semantic_checked_to_confirmed(self):
        sm.assert_field_transition(
            FieldStatus.SEMANTIC_CHECKED, FieldStatus.CONFIRMED
        )

    def test_semantic_checked_to_rejected(self):
        sm.assert_field_transition(
            FieldStatus.SEMANTIC_CHECKED, FieldStatus.REJECTED
        )

    def test_semantic_checked_to_flagged(self):
        sm.assert_field_transition(
            FieldStatus.SEMANTIC_CHECKED, FieldStatus.FLAGGED
        )

    def test_flagged_back_to_rule_checked(self):
        """flagged 可以回退到 rule_checked 重新审视（手动恢复）。"""
        sm.assert_field_transition(FieldStatus.FLAGGED, FieldStatus.RULE_CHECKED)

    def test_flagged_back_to_semantic_checked(self):
        sm.assert_field_transition(
            FieldStatus.FLAGGED, FieldStatus.SEMANTIC_CHECKED
        )

    def test_terminal_states_have_no_allowed_transitions(self):
        """confirmed/rejected 是终态，无任何合法迁移。"""
        allowed_confirmed = sm.ALLOWED_FIELD_TRANSITIONS[FieldStatus.CONFIRMED]
        assert allowed_confirmed == set()
        assert sm.ALLOWED_FIELD_TRANSITIONS[FieldStatus.REJECTED] == set()
        assert sm.is_field_terminal(FieldStatus.CONFIRMED)
        assert sm.is_field_terminal(FieldStatus.REJECTED)

    def test_field_mark_main_path_through_all_seven_states(self):
        """FieldMark 实装走完 pending → ... → confirmed 一条主路径。"""
        m = FieldMark(field_name="姓名")
        assert m.status == FieldStatus.PENDING
        m.mark_extracting()
        assert m.status == FieldStatus.EXTRACTING
        m.mark_rule_checked()
        assert m.status == FieldStatus.RULE_CHECKED
        m.mark_semantic_checked()
        assert m.status == FieldStatus.SEMANTIC_CHECKED
        m.confirm()
        assert m.status == FieldStatus.CONFIRMED


# ---------------------------------------------------------------------------
# 字段非法迁移失败
# ---------------------------------------------------------------------------


class TestFieldIllegalTransitions:
    """字段 7 态的非法迁移必须抛 InvalidTransitionError。"""

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            # 跳过中间态
            (FieldStatus.PENDING, FieldStatus.CONFIRMED),
            (FieldStatus.PENDING, FieldStatus.REJECTED),
            (FieldStatus.PENDING, FieldStatus.SEMANTIC_CHECKED),
            (FieldStatus.PENDING, FieldStatus.RULE_CHECKED),
            (FieldStatus.EXTRACTING, FieldStatus.SEMANTIC_CHECKED),
            (FieldStatus.EXTRACTING, FieldStatus.CONFIRMED),
            (FieldStatus.RULE_CHECKED, FieldStatus.CONFIRMED),
            (FieldStatus.RULE_CHECKED, FieldStatus.REJECTED),
            # 同态迁移（已 confirmed 再 confirm）
            (FieldStatus.CONFIRMED, FieldStatus.CONFIRMED),
            (FieldStatus.REJECTED, FieldStatus.REJECTED),
            # 终态 -> 任意
            (FieldStatus.CONFIRMED, FieldStatus.REJECTED),
            (FieldStatus.CONFIRMED, FieldStatus.FLAGGED),
            (FieldStatus.REJECTED, FieldStatus.FLAGGED),
            (FieldStatus.REJECTED, FieldStatus.CONFIRMED),
            # 反向（不允许从 confirmed 回到 pending/extracting）
            (FieldStatus.CONFIRMED, FieldStatus.PENDING),
            (FieldStatus.SEMANTIC_CHECKED, FieldStatus.RULE_CHECKED),
            (FieldStatus.SEMANTIC_CHECKED, FieldStatus.EXTRACTING),
            # flagged 回退到 PENDING/EXTRACTING/CONFIRMED/REJECTED 不允许
            (FieldStatus.FLAGGED, FieldStatus.PENDING),
            (FieldStatus.FLAGGED, FieldStatus.EXTRACTING),
            (FieldStatus.FLAGGED, FieldStatus.CONFIRMED),
            (FieldStatus.FLAGGED, FieldStatus.REJECTED),
        ],
    )
    def test_illegal_field_transition_raises(self, from_status, to_status):
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.assert_field_transition(from_status, to_status)
        # 异常携带诊断信息
        err = exc_info.value
        assert err.from_state == from_status
        assert err.to_state == to_status
        assert err.category == "field"
        assert isinstance(err.allowed, list)

    def test_field_mark_confirm_twice_raises(self):
        """已 confirmed 字段再 confirm 必须抛 InvalidTransitionError。"""
        m = FieldMark(field_name="X")
        m.mark_extracting().mark_rule_checked().mark_semantic_checked().confirm()
        with pytest.raises(InvalidTransitionError):
            m.confirm()

    def test_field_mark_skip_extracting_to_confirm_raises(self):
        """跳过 extracting 直接 confirm（异常路径）必须抛错。"""
        m = FieldMark(field_name="X")
        with pytest.raises(InvalidTransitionError):
            m.confirm()  # 从 PENDING 跳到 CONFIRMED

    def test_field_mark_reject_from_pending_raises(self):
        """pending → rejected 跳步必须抛错。"""
        m = FieldMark(field_name="X")
        with pytest.raises(InvalidTransitionError):
            m.reject()


# ---------------------------------------------------------------------------
# 单据 5 态合法迁移
# ---------------------------------------------------------------------------


class TestDocumentLegalTransitions:
    """单据 5 态合法迁移路径。"""

    def test_draft_to_in_review(self):
        sm.assert_document_transition(DocumentState.DRAFT, DocumentState.IN_REVIEW)

    def test_in_review_to_confirmed(self):
        sm.assert_document_transition(
            DocumentState.IN_REVIEW, DocumentState.CONFIRMED
        )

    def test_confirmed_to_exported(self):
        sm.assert_document_transition(
            DocumentState.CONFIRMED, DocumentState.EXPORTED
        )

    def test_exported_to_submitted(self):
        sm.assert_document_transition(
            DocumentState.EXPORTED, DocumentState.SUBMITTED
        )

    def test_submitted_is_terminal(self):
        assert sm.is_document_terminal(DocumentState.SUBMITTED)
        assert sm.ALLOWED_DOCUMENT_TRANSITIONS[DocumentState.SUBMITTED] == set()

    def test_billing_case_document_advance_full_path(self):
        """BillingCaseDocument.advance_document 走完整 5 态路径。"""
        d = BillingCaseDocument(case_id="c", raw_text="x")
        d.advance_document(DocumentState.IN_REVIEW)
        assert d.pipeline_state == DocumentState.IN_REVIEW
        d.advance_document(DocumentState.CONFIRMED)
        assert d.pipeline_state == DocumentState.CONFIRMED
        d.advance_document(DocumentState.EXPORTED)
        assert d.pipeline_state == DocumentState.EXPORTED
        d.advance_document(DocumentState.SUBMITTED)
        assert d.pipeline_state == DocumentState.SUBMITTED


# ---------------------------------------------------------------------------
# 单据非法迁移失败
# ---------------------------------------------------------------------------


class TestDocumentIllegalTransitions:
    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            (DocumentState.DRAFT, DocumentState.CONFIRMED),
            (DocumentState.DRAFT, DocumentState.EXPORTED),
            (DocumentState.DRAFT, DocumentState.SUBMITTED),
            (DocumentState.IN_REVIEW, DocumentState.DRAFT),
            (DocumentState.IN_REVIEW, DocumentState.EXPORTED),
            (DocumentState.IN_REVIEW, DocumentState.SUBMITTED),
            (DocumentState.CONFIRMED, DocumentState.IN_REVIEW),
            (DocumentState.CONFIRMED, DocumentState.SUBMITTED),
            (DocumentState.EXPORTED, DocumentState.DRAFT),
            (DocumentState.EXPORTED, DocumentState.IN_REVIEW),
            (DocumentState.EXPORTED, DocumentState.CONFIRMED),
            (DocumentState.SUBMITTED, DocumentState.DRAFT),
            (DocumentState.SUBMITTED, DocumentState.IN_REVIEW),
            (DocumentState.SUBMITTED, DocumentState.EXPORTED),
            # 同态
            (DocumentState.DRAFT, DocumentState.DRAFT),
            (DocumentState.IN_REVIEW, DocumentState.IN_REVIEW),
        ],
    )
    def test_illegal_document_transition_raises(self, from_state, to_state):
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.assert_document_transition(from_state, to_state)
        err = exc_info.value
        assert err.from_state == from_state
        assert err.to_state == to_state
        assert err.category == "document"

    def test_billing_case_advance_skip_draft_to_confirmed_raises(self):
        d = BillingCaseDocument(case_id="c", raw_text="x")
        with pytest.raises(InvalidTransitionError):
            d.advance_document(DocumentState.CONFIRMED)

    def test_billing_case_advance_after_submitted_raises(self):
        """submitted 是终态，再 advance 必须抛错。"""
        d = BillingCaseDocument(case_id="c", raw_text="x")
        d.advance_document(DocumentState.IN_REVIEW).advance_document(
            DocumentState.CONFIRMED
        ).advance_document(DocumentState.EXPORTED).advance_document(
            DocumentState.SUBMITTED
        )
        with pytest.raises(InvalidTransitionError):
            d.advance_document(DocumentState.DRAFT)


# ---------------------------------------------------------------------------
# assert_transition 通用入口（按 from_state 类型派发）
# ---------------------------------------------------------------------------


class TestAssertTransitionDispatch:
    def test_dispatches_to_field_for_field_status(self):
        sm.assert_transition(FieldStatus.PENDING, FieldStatus.EXTRACTING)

    def test_dispatches_to_document_for_document_state(self):
        sm.assert_transition(DocumentState.DRAFT, DocumentState.IN_REVIEW)

    def test_auto_dispatch_blocked_when_field_illegal(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.assert_transition(FieldStatus.PENDING, FieldStatus.CONFIRMED)
        assert exc_info.value.category == "field"

    def test_auto_dispatch_blocked_when_document_illegal(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.assert_transition(DocumentState.DRAFT, DocumentState.SUBMITTED)
        assert exc_info.value.category == "document"

    def test_typeerror_for_unknown_type(self):
        with pytest.raises(TypeError):
            sm.assert_transition("pending", "extracting")  # plain strings not allowed


# ---------------------------------------------------------------------------
# 类型校验
# ---------------------------------------------------------------------------


class TestTypeGuards:
    def test_assert_field_transition_rejects_non_field_target(self):
        """assert_field_transition 要求 to_status 为 FieldStatus。"""
        with pytest.raises(TypeError):
            sm.assert_field_transition(FieldStatus.PENDING, "extracting")

    def test_assert_document_transition_rejects_non_document_target(self):
        with pytest.raises(TypeError):
            sm.assert_document_transition(DocumentState.DRAFT, "in_review")

    def test_assert_transition_rejects_cross_type_target(self):
        """assert_transition 要求 from_state 与 to_state 同类（都是 FieldStatus
        或都是 DocumentState）。"""
        with pytest.raises(TypeError):
            sm.assert_transition(DocumentState.DRAFT, FieldStatus.EXTRACTING)


# ---------------------------------------------------------------------------
# 异常属性
# ---------------------------------------------------------------------------


class TestInvalidTransitionErrorAttributes:
    def test_error_carries_category_field(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.assert_field_transition(FieldStatus.PENDING, FieldStatus.CONFIRMED)
        err = exc_info.value
        assert err.category == "field"
        assert err.from_state == FieldStatus.PENDING
        assert err.to_state == FieldStatus.CONFIRMED
        assert "extracting" in err.allowed or "flagged" in err.allowed

    def test_error_carries_category_document(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.assert_document_transition(
                DocumentState.IN_REVIEW, DocumentState.EXPORTED
            )
        err = exc_info.value
        assert err.category == "document"
        assert err.from_state == DocumentState.IN_REVIEW
        assert err.to_state == DocumentState.EXPORTED

    def test_terminal_state_error_message_says_terminal(self):
        """终态迁移错误信息应含「终态」字样。"""
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.assert_field_transition(FieldStatus.CONFIRMED, FieldStatus.REJECTED)
        assert "终态" in str(exc_info.value)

    def test_isinstance_value_error(self):
        """InvalidTransitionError 必须是 ValueError 的子类，以便业务侧统一捕获。"""
        assert issubclass(InvalidTransitionError, ValueError)
        with pytest.raises(ValueError):
            sm.assert_field_transition(FieldStatus.PENDING, FieldStatus.CONFIRMED)


# ---------------------------------------------------------------------------
# 辅助查询 API
# ---------------------------------------------------------------------------


class TestHelperQueries:
    def test_is_field_terminal_only_for_confirmed_and_rejected(self):
        assert sm.is_field_terminal(FieldStatus.CONFIRMED)
        assert sm.is_field_terminal(FieldStatus.REJECTED)
        assert not sm.is_field_terminal(FieldStatus.PENDING)
        assert not sm.is_field_terminal(FieldStatus.EXTRACTING)
        assert not sm.is_field_terminal(FieldStatus.RULE_CHECKED)
        assert not sm.is_field_terminal(FieldStatus.SEMANTIC_CHECKED)
        assert not sm.is_field_terminal(FieldStatus.FLAGGED)

    def test_is_document_terminal_only_for_submitted(self):
        assert sm.is_document_terminal(DocumentState.SUBMITTED)
        for s in [
            DocumentState.DRAFT,
            DocumentState.IN_REVIEW,
            DocumentState.CONFIRMED,
            DocumentState.EXPORTED,
        ]:
            assert not sm.is_document_terminal(s)

    def test_allowed_field_next_returns_strings(self):
        nexts = sm.allowed_field_next(FieldStatus.PENDING)
        assert set(nexts) == {"extracting", "flagged"}
        assert sm.allowed_field_next(FieldStatus.CONFIRMED) == []
        assert sm.allowed_field_next(FieldStatus.REJECTED) == []

    def test_allowed_document_next_returns_strings(self):
        nexts = sm.allowed_document_next(DocumentState.IN_REVIEW)
        assert nexts == ["confirmed"]
        assert sm.allowed_document_next(DocumentState.SUBMITTED) == []


# ---------------------------------------------------------------------------
# FieldMark.mark_* 与 BillingCaseDocument.advance_document 内部走 state_machine
# ---------------------------------------------------------------------------


class TestDocumentIntegrationWithStateMachine:
    def test_field_mark_reset_to_pending_via_flag_back_path(self):
        """flagged 字段回退路径走实装方法。"""
        m = FieldMark(field_name="X")
        m.mark_extracting()
        m.flag()  # EXTRACTING → FLAGGED
        assert m.status == FieldStatus.FLAGGED
        m.mark_rule_checked()  # FLAGGED → RULE_CHECKED（回退）
        assert m.status == FieldStatus.RULE_CHECKED

    def test_field_mark_mark_needs_manual_routes_through_flagged(self):
        """mark_needs_manual 把状态推到 FLAGGED + source=NEEDS_MANUAL。"""
        m = FieldMark(field_name="X")
        m.mark_needs_manual()
        assert m.status == FieldStatus.FLAGGED
        from case_bill_validation.document import FieldSource
        assert m.source == FieldSource.NEEDS_MANUAL

    def test_advance_document_is_stateless_object_returned(self):
        """advance_document 返回 self 便于链式调用（不破坏既有用法）。"""
        d = BillingCaseDocument(case_id="c", raw_text="x")
        result = d.advance_document(DocumentState.IN_REVIEW)
        assert result is d

    def test_field_mark_mark_methods_return_self(self):
        """所有 mark_* 必须返回 self（链式调用契约）。"""
        m = FieldMark(field_name="X")
        assert m.mark_extracting() is m
        assert m.mark_rule_checked() is m
        assert m.mark_semantic_checked() is m
        assert m.confirm() is m
        # reject 是另一条终态路径
        m2 = FieldMark(field_name="Y")
        assert m2.mark_extracting().mark_rule_checked().mark_semantic_checked().reject() is m2
        # flag 是失败路径
        m3 = FieldMark(field_name="Z")
        assert m3.flag() is m3
