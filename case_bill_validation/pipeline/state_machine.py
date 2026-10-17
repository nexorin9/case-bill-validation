"""流水线状态机：字段级 + 单据级非法迁移拒绝。

本模块是流水线状态推进的唯一事实来源，被
:mod:`case_bill_validation.document` 的 ``FieldMark.mark_*`` /
``BillingCaseDocument.advance_document`` 调用，与编排器
:mod:`case_bill_validation.pipeline.core` 的 DAG 校验解耦。

设计动机
========
- 字段从抽取到登记的 7 个状态推进路径（见 :data:`ALLOWED_FIELD_TRANSITIONS`）
  必须显式登记；任何"跳过中间态"都意味着流水线语义被偷偷绕过，结算清单的法
  律责任无法追溯（spec.md §5 closure_dimensions 中"业务状态生命周期"维度）。
- 单据从草稿到提交医保的 5 个状态（见 :data:`ALLOWED_DOCUMENT_TRANSITIONS`）
  也须显式登记；状态机非法迁移意味着"该单据未经审核即提交"，违反医保办与病
  案室会签流程。

设计参考
========
- ``spec.md`` §5 业务状态生命周期；
- ``github_ref/stanza/stanza/models/common/doc.py`` ``Document`` 字段表的同模式
  （累积 + 不允许跳步）。

对外 API
========

- :data:`ALLOWED_FIELD_TRANSITIONS` —— 字段 7 态状态迁移表；
- :data:`ALLOWED_DOCUMENT_TRANSITIONS` —— 单据 5 态状态迁移表；
- :class:`InvalidTransitionError` —— 非法迁移时抛出；
- :func:`assert_transition` —— 通用入口，按 ``from`` / ``to`` 的类型自动判定
  走字段迁移还是单据迁移；
- :func:`assert_field_transition` —— 显式字段迁移校验；
- :func:`assert_document_transition` —— 显式单据迁移校验。
"""

from __future__ import annotations

from typing import Union

from case_bill_validation.document import DocumentState, FieldStatus


# ---------------------------------------------------------------------------
# 迁移表
# ---------------------------------------------------------------------------


# 字段 7 态状态机迁移表（pending → extracting → rule_checked → semantic_checked →
# {confirmed | rejected | flagged}；extracting 与 rule_checked 失败时也可直接
# 跳 flagged；flagged 可回退到 rule_checked / semantic_checked 重新审视；confirmed
# / rejected 是终态）。
ALLOWED_FIELD_TRANSITIONS: dict[FieldStatus, set[FieldStatus]] = {
    FieldStatus.PENDING: {FieldStatus.EXTRACTING, FieldStatus.FLAGGED},
    FieldStatus.EXTRACTING: {
        FieldStatus.RULE_CHECKED,
        FieldStatus.FLAGGED,
    },
    FieldStatus.RULE_CHECKED: {
        FieldStatus.SEMANTIC_CHECKED,
        FieldStatus.FLAGGED,
    },
    FieldStatus.SEMANTIC_CHECKED: {
        FieldStatus.CONFIRMED,
        FieldStatus.REJECTED,
        FieldStatus.FLAGGED,
    },
    FieldStatus.CONFIRMED: set(),
    FieldStatus.REJECTED: set(),
    FieldStatus.FLAGGED: {FieldStatus.RULE_CHECKED, FieldStatus.SEMANTIC_CHECKED},
}


# 单据 5 态状态机迁移表（draft → in_review → confirmed → exported → submitted；
# submitted 是终态；导出后须回退确认才能再次走 in_review，但本轮简化路径下确认
# 与导出合一，由业务路由强制）。
ALLOWED_DOCUMENT_TRANSITIONS: dict[DocumentState, set[DocumentState]] = {
    DocumentState.DRAFT: {DocumentState.IN_REVIEW},
    DocumentState.IN_REVIEW: {DocumentState.CONFIRMED},
    DocumentState.CONFIRMED: {DocumentState.EXPORTED},
    DocumentState.EXPORTED: {DocumentState.SUBMITTED},
    DocumentState.SUBMITTED: set(),
}


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class InvalidTransitionError(ValueError):
    """字段/单据状态机非法迁移时抛出。

    形参 ``category`` 仅用于诊断消息，不参与断言。默认值 ``"state"`` 让消息泛用
    于字段 / 单据两种场景。
    """

    def __init__(
        self,
        message: str,
        *,
        category: str = "state",
        from_state: object | None = None,
        to_state: object | None = None,
        allowed: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.from_state = from_state
        self.to_state = to_state
        self.allowed = allowed or []


# ---------------------------------------------------------------------------
# 校验入口
# ---------------------------------------------------------------------------


# 联合类型别名，描述可被 assert_transition 自动派发的状态对象。
StateLike = Union[FieldStatus, DocumentState]


def _value(state: object) -> str:
    """安全地把状态对象转为字符串值（兼容 ``Enum.value`` / 原生字符串）。"""
    if hasattr(state, "value"):
        return str(getattr(state, "value"))
    return str(state)


def assert_transition(from_state: StateLike, to_state: StateLike) -> None:
    """通用状态迁移校验；按 ``from_state`` 的类型自动派发字段 / 单据。

    - ``from_state`` 是 :class:`FieldStatus` 走 :func:`assert_field_transition`；
    - ``from_state`` 是 :class:`DocumentState` 走 :func:`assert_document_transition`；
    - 其他类型抛 :class:`TypeError`（防御性检查，避免业务误传裸字符串）。
    """
    if isinstance(from_state, FieldStatus):
        assert_field_transition(from_state, to_state)
        return
    if isinstance(from_state, DocumentState):
        # 同步保证 to_state 也是 DocumentState；防止业务误跨类型
        if not isinstance(to_state, DocumentState):
            raise TypeError(
                f"单据状态迁移要求 from_state/to_state 同为 DocumentState，"
                f"实际 to_state 类型 {type(to_state).__name__}"
            )
        assert_document_transition(from_state, to_state)
        return
    raise TypeError(
        f"assert_transition 不支持的状态类型: {type(from_state).__name__}"
    )


def assert_field_transition(
    from_status: FieldStatus, to_status: FieldStatus
) -> None:
    """校验字段状态 ``from_status -> to_status`` 是否在合法集内。

    非合法迁移抛 :class:`InvalidTransitionError`，异常消息列出当前状态合法
    的所有下一态，便于业务侧定位。
    """
    if not isinstance(to_status, FieldStatus):
        raise TypeError(
            f"assert_field_transition 要求 to_status 为 FieldStatus，"
            f"实际类型 {type(to_status).__name__}"
        )
    allowed = ALLOWED_FIELD_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        allowed_str = sorted(_value(s) for s in allowed) or ["(终态)"]
        raise InvalidTransitionError(
            f"字段状态非法迁移: {_value(from_status)} -> {_value(to_status)}。"
            f" 合法的下一态为: {allowed_str}",
            category="field",
            from_state=from_status,
            to_state=to_status,
            allowed=allowed_str,
        )


def assert_document_transition(
    from_state: DocumentState, to_state: DocumentState
) -> None:
    """校验单据状态 ``from_state -> to_state`` 是否在合法集内。

    非合法迁移抛 :class:`InvalidTransitionError`。
    """
    if not isinstance(to_state, DocumentState):
        raise TypeError(
            f"assert_document_transition 要求 to_state 为 DocumentState，"
            f"实际类型 {type(to_state).__name__}"
        )
    allowed = ALLOWED_DOCUMENT_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        allowed_str = sorted(_value(s) for s in allowed) or ["(终态)"]
        raise InvalidTransitionError(
            f"单据状态非法迁移: {_value(from_state)} -> {_value(to_state)}。"
            f" 合法的下一态为: {allowed_str}",
            category="document",
            from_state=from_state,
            to_state=to_state,
            allowed=allowed_str,
        )


# ---------------------------------------------------------------------------
# 辅助查询（业务侧与测试侧共享）
# ---------------------------------------------------------------------------


def is_field_terminal(status: FieldStatus) -> bool:
    """字段是否处于终态（confirmed / rejected，且不再有任何合法迁移）。"""
    return len(ALLOWED_FIELD_TRANSITIONS.get(status, set())) == 0


def is_document_terminal(state: DocumentState) -> bool:
    """单据是否处于终态（submitted，且不再有任何合法迁移）。"""
    return len(ALLOWED_DOCUMENT_TRANSITIONS.get(state, set())) == 0


def allowed_field_next(status: FieldStatus) -> list[str]:
    """返回字段状态 ``status`` 的合法下一态字符串列表（按 enum 顺序稳定排序）。"""
    return [_value(s) for s in sorted(ALLOWED_FIELD_TRANSITIONS.get(status, set()), key=lambda x: x.value)]


def allowed_document_next(state: DocumentState) -> list[str]:
    """返回单据状态 ``state`` 的合法下一态字符串列表。"""
    return [_value(s) for s in sorted(ALLOWED_DOCUMENT_TRANSITIONS.get(state, set()), key=lambda x: x.value)]


__all__ = [
    "ALLOWED_FIELD_TRANSITIONS",
    "ALLOWED_DOCUMENT_TRANSITIONS",
    "InvalidTransitionError",
    "StateLike",
    "assert_transition",
    "assert_field_transition",
    "assert_document_transition",
    "is_field_terminal",
    "is_document_terminal",
    "allowed_field_next",
    "allowed_document_next",
]
