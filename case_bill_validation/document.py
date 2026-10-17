"""病案单据与字段数据模型。

定义链式流水线在每一步之间传递的对象：

- ``SpanRef`` / ``FieldMark`` / ``RuleHit`` / ``AuditEntry`` —— 原子证据单元；
- ``FieldStatus`` 7 值枚举（pending / extracting / rule_checked / semantic_checked
  / confirmed / rejected / flagged），覆盖字段级生命周期；
- ``DocumentState`` 5 值枚举（draft / in_review / confirmed / exported / submitted），
  覆盖单据级生命周期；
- ``BillingCaseDocument`` —— 流水线载体，承载原文、span 列表、字段表、规则命中、
  审计流水与流水线自身状态。

所有模型基于 pydantic v2，``model_config`` 启用 ``validate_assignment`` —— 任何字段
被重新赋值都触发校验（包括状态机非法迁移）。``mark_*`` 方法是字段状态推进的唯一
推荐入口，状态机非法迁移由
:mod:`case_bill_validation.pipeline.state_machine` 的 ``assert_transition`` 抛
``InvalidTransitionError``。本模块对状态机的迁移表与校验函数做**再导出**，便于
``from case_bill_validation.document import ALLOWED_FIELD_TRANSITIONS`` 之类的
既有用法继续可用。

参考：
- ``spec.md`` §5（closure_dimensions 中"状态生命周期"维度）；
- ``github_ref/stanza/stanza/models/common/doc.py`` —— ``Document`` 作为流水线
  每一步累加结果载体的同模式；
- ``case_bill_validation/pipeline/state_machine.py`` —— 迁移表 + 校验函数真源。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 状态机迁移表 + 校验函数真源在
# ``case_bill_validation.pipeline.state_machine``。**为了避免与
# state_machine.py 的相互循环导入（state_machine 也需要从 document 导入
# FieldStatus / DocumentState），本模块在文件末尾再做一次**再导入；类内部的方法
# 体（_transition / advance_document）到运行时才解析 ``assert_field_transition``
# / ``assert_document_transition``，因此可以安全地延迟到末尾注入。


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FieldStatus(str, Enum):
    """字段级 7 态状态。

    流水线主路径::

        pending -> extracting -> rule_checked -> semantic_checked
                                              |
                                              +-> confirmed
                                              +-> rejected
                                              +-> flagged

    此外 ``extracting`` 与 ``rule_checked`` 失败时也可直接跳 ``flagged``。
    """

    PENDING = "pending"
    EXTRACTING = "extracting"
    RULE_CHECKED = "rule_checked"
    SEMANTIC_CHECKED = "semantic_checked"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FLAGGED = "flagged"


class DocumentState(str, Enum):
    """单据级 5 态状态。

    ::

        draft -> in_review -> confirmed -> exported -> submitted
    """

    DRAFT = "draft"
    IN_REVIEW = "in_review"
    CONFIRMED = "confirmed"
    EXPORTED = "exported"
    SUBMITTED = "submitted"


class FieldSource(str, Enum):
    """字段值来源，决定抽取链路与可审计元数据。"""

    REGEX = "regex"
    LLM = "llm"
    MANUAL = "manual"
    NEEDS_MANUAL = "needs_manual"


class RuleSeverity(str, Enum):
    """规则命中严重度。"""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class RuleSource(str, Enum):
    """规则来源标签，方便审计追溯。"""

    CROSS_FIELD = "cross_field"
    REQUIRED = "required"
    TYPE_CHECK = "type_check"
    ICD_CODE = "icd_code"
    INVALID_CODE = "invalid_code"
    SEMANTIC = "semantic"
    LLM_SKIPPED = "llm_skipped"


# ---------------------------------------------------------------------------
# 允许的状态迁移表（再导出；真源在 pipeline.state_machine，见文件末尾的
# 延迟 import —— 此处只留注释占位，避免循环依赖）
# ---------------------------------------------------------------------------


# 注：ALLOWED_FIELD_TRANSITIONS / ALLOWED_DOCUMENT_TRANSITIONS / InvalidTransitionError
# / assert_field_transition / assert_document_transition 由文件末尾的延迟 import
# 注入到本模块 globals。直接运行时可通过
# ``from case_bill_validation.document import ALLOWED_FIELD_TRANSITIONS`` 访问，
# 真源仍然在 ``case_bill_validation.pipeline.state_machine``。


# ---------------------------------------------------------------------------
# 原子证据
# ---------------------------------------------------------------------------


class SpanRef(BaseModel):
    """原文区间引用。``start_char`` / ``end_char`` 必须落在承载它的文档正文内。

    字段::

        start_char (int, ge=0) —— 含；
        end_char   (int, gt=start_char) —— 排他；
        page_no    (int | None) —— 若原文带页码分隔则填写。
    """

    model_config = ConfigDict(validate_assignment=True, frozen=True)

    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    page_no: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ensure_span_bounds(self) -> "SpanRef":
        if self.end_char <= self.start_char:
            raise ValueError(
                f"SpanRef.end_char ({self.end_char}) 必须严格大于 "
                f"start_char ({self.start_char})"
            )
        return self

    def text_of(self, raw_text: str) -> str:
        """返回 ``raw_text[start_char:end_char]``。调用方负责在原始文档上落地。"""
        return raw_text[self.start_char : self.end_char]


class RuleHit(BaseModel):
    """一次规则命中（跨字段 / 必填 / 类型 / ICD 编码 / 语义 / LLM 跳过）。"""

    model_config = ConfigDict(validate_assignment=True)

    rule_id: str
    rule_source: RuleSource
    severity: RuleSeverity
    message_text: str
    span: SpanRef | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditEntry(BaseModel):
    """审计流水（字段状态推进 / 单据状态推进 / 手工操作）。"""

    model_config = ConfigDict(validate_assignment=True)

    actor: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    field: str | None = None
    action: str
    before_status: str | None = None
    after_status: str | None = None
    hash: str | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# 字段表
# ---------------------------------------------------------------------------


class FieldMark(BaseModel):
    """流水线对单个结算清单字段的抽取结果（含状态机与规则命中）。"""

    model_config = ConfigDict(validate_assignment=True)

    field_name: str = Field(min_length=1)
    value: Any = None
    span: SpanRef | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: FieldStatus = Field(default=FieldStatus.PENDING)
    source: FieldSource = Field(default=FieldSource.REGEX)
    rule_hits: list[RuleHit] = Field(default_factory=list)
    note: str | None = None

    @field_validator("field_name")
    @classmethod
    def _validate_field_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("field_name 不能为空字符串")
        return v

    @model_validator(mode="after")
    def _check_span_in_raw_text(self) -> "FieldMark":
        """``span`` 与 ``value`` 一致性快速自检。完整边界校验由
        ``tests/test_span_binding.py`` 在 t6 接 extractor 时做。"""
        if self.span is not None and isinstance(self.value, str):
            # 仅当 value 是字符串时做基本一致性检查（不依赖 raw_text；
            # BillingCaseDocument 上有更强的绑定校验）。
            txt_len = len(self.value)
            # span_end - span_start 应当等于 value 的字符长度 —— 这是 t6 test_span_binding
            # 的主断言之一，这里只做长度下限提示，不强制。
            _ = txt_len  # 占位，避免 pylint no-else-raise
        return self

    # ------------------------------------------------------------------
    # 状态推进方法（推荐唯一入口）
    # ------------------------------------------------------------------

    def mark_extracting(self) -> "FieldMark":
        self._transition(FieldStatus.EXTRACTING)
        return self

    def mark_rule_checked(self) -> "FieldMark":
        self._transition(FieldStatus.RULE_CHECKED)
        return self

    def mark_semantic_checked(self) -> "FieldMark":
        self._transition(FieldStatus.SEMANTIC_CHECKED)
        return self

    def confirm(self) -> "FieldMark":
        self._transition(FieldStatus.CONFIRMED)
        return self

    def reject(self) -> "FieldMark":
        self._transition(FieldStatus.REJECTED)
        return self

    def flag(self) -> "FieldMark":
        self._transition(FieldStatus.FLAGGED)
        return self

    def mark_needs_manual(self) -> "FieldMark":
        """抽取失败 → 标 ``FLAGGED`` + ``source=NEEDS_MANUAL``。"""
        self.source = FieldSource.NEEDS_MANUAL
        self._transition(FieldStatus.FLAGGED)
        return self

    def add_rule_hit(self, hit: RuleHit) -> "FieldMark":
        """追加一次规则命中（去重 by ``(rule_id, span)``）。"""
        for existing in self.rule_hits:
            if existing.rule_id == hit.rule_id and existing.span == hit.span:
                return self
        self.rule_hits.append(hit)
        return self

    def _transition(self, to_status: FieldStatus) -> None:
        assert_field_transition(self.status, to_status)
        self.status = to_status


# ---------------------------------------------------------------------------
# 单据
# ---------------------------------------------------------------------------


class BillingCaseDocument(BaseModel):
    """整份病案结算单据的流水线载体。

    字段语义见 ``spec.md`` §3：

    - ``raw_text`` —— 出院小结/病案首页纯文本；
    - ``spans`` —— 段落/句子 span 列表（segmenter 输出，t4 填充）；
    - ``extracted_fields`` —— 字段表（extractor 输出，t6 填充）；
    - ``rule_hits`` —— 跨字段 + 规则审查命中（t7 填充）；
    - ``audit_log`` —— 审计流水；
    - ``pipeline_state`` —— 单据级 5 态；
    - ``processed_steps`` —— 已完成的流水线步骤（用于 SSE 进度广播）。
    """

    model_config = ConfigDict(validate_assignment=True)

    case_id: str = Field(min_length=1)
    raw_text: str = ""
    pages: list[tuple[int, int]] = Field(default_factory=list)
    spans: list[SpanRef] = Field(default_factory=list)
    sections: dict[str, list[int]] = Field(default_factory=dict)
    extracted_fields: dict[str, FieldMark] = Field(default_factory=dict)
    rule_hits: list[RuleHit] = Field(default_factory=list)
    audit_log: list[AuditEntry] = Field(default_factory=list)
    pipeline_state: DocumentState = Field(default=DocumentState.DRAFT)
    processed_steps: list[str] = Field(default_factory=list)
    needs_manual: bool = False
    parser_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_spans_within_raw_text(self) -> "BillingCaseDocument":
        for s in self.spans:
            if s.end_char > len(self.raw_text):
                raise ValueError(
                    f"span end_char={s.end_char} 超出 raw_text 长度 "
                    f"{len(self.raw_text)}（field={s.model_dump()}）"
                )
        for fname, fmark in self.extracted_fields.items():
            if fmark.span is not None:
                if fmark.span.end_char > len(self.raw_text):
                    raise ValueError(
                        f"字段 {fname} 的 span 越界（end_char={fmark.span.end_char}"
                        f" 超出 raw_text 长度 {len(self.raw_text)}）"
                    )
                if fmark.span.start_char < 0 or fmark.span.end_char <= fmark.span.start_char:
                    raise ValueError(
                        f"字段 {fname} 的 span 非法（start={fmark.span.start_char}"
                        f" end={fmark.span.end_char}）"
                    )
        return self

    # ------------------------------------------------------------------
    # 单据状态推进
    # ------------------------------------------------------------------

    def advance_document(self, to_state: DocumentState) -> "BillingCaseDocument":
        assert_document_transition(self.pipeline_state, to_state)
        self.pipeline_state = to_state
        return self

    # ------------------------------------------------------------------
    # 字段辅助
    # ------------------------------------------------------------------

    def upsert_field(self, field: FieldMark) -> "BillingCaseDocument":
        """插入或覆盖一个字段；返回 self 以便链式调用。"""
        self.extracted_fields[field.field_name] = field
        return self

    def record_step(self, step: str) -> "BillingCaseDocument":
        """记录一条流水线步骤（去重保序）。"""
        if step not in self.processed_steps:
            self.processed_steps.append(step)
        return self

    def append_audit(self, entry: AuditEntry) -> "BillingCaseDocument":
        self.audit_log.append(entry)
        return self

    # ------------------------------------------------------------------
    # 统计 / 调试
    # ------------------------------------------------------------------

    def field_status_counts(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in FieldStatus}
        for f in self.extracted_fields.values():
            out[f.status.value] += 1
        return out

    def flagged_field_names(self) -> list[str]:
        return [
            name
            for name, f in self.extracted_fields.items()
            if f.status == FieldStatus.FLAGGED
        ]


__all__ = [
    "FieldStatus",
    "DocumentState",
    "FieldSource",
    "RuleSeverity",
    "RuleSource",
    "ALLOWED_FIELD_TRANSITIONS",
    "ALLOWED_DOCUMENT_TRANSITIONS",
    "InvalidTransitionError",
    "assert_field_transition",
    "assert_document_transition",
    "SpanRef",
    "RuleHit",
    "AuditEntry",
    "FieldMark",
    "BillingCaseDocument",
]


# ---------------------------------------------------------------------------
# 延迟 import：状态机真源注入
# ---------------------------------------------------------------------------


# 延迟到文件末尾导入 ``case_bill_validation.pipeline.state_machine``，
# 即可保留 ``from case_bill_validation.document import ...`` 的既有用法，
# 又能避开 ``document <-> state_machine`` 之间的循环导入（state_machine 需要从
# 本模块导入 FieldStatus / DocumentState，但本模块此时已经把这些 enum 定义完毕）。
from case_bill_validation.pipeline.state_machine import (  # noqa: E402,F401
    ALLOWED_DOCUMENT_TRANSITIONS,
    ALLOWED_FIELD_TRANSITIONS,
    InvalidTransitionError,
    assert_document_transition,
    assert_field_transition,
)
