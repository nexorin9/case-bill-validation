"""SemanticChecker processor（Task 8 实装）。

设计要点
--------

- 输入：``BillingCaseDocument``（由 ``RuleCheckerProcessor`` 完成
  ``extracted_fields`` 中字段状态推进至 ``RULE_CHECKED`` / ``FLAGGED``）；
- 输出：对每个 ``status='flagged'`` 字段调 :class:`ModelGateway` 做语义核：
    - 成功 → 字段推进到 ``SEMANTIC_CHECKED`` + 追加 ``RuleHit(rule_source=semantic)``；
    - 失败 → 字段保持 ``RULE_CHECKED`` + 追加 ``RuleHit(rule_source=llm_skipped,
      severity=info, message='semantic_skipped')``，**不静默写数据**；
- 状态机：``RULE_CHECKED → SEMANTIC_CHECKED``；失败时 ``RULE_CHECKED`` 不变。
- 审计：每次成功调用与失败调用都通过 :func:`append_llm_call` 落
  ``logs/audit/llm_calls.jsonl``。

参考
----
- ``spec.md`` §5（"安全审计" / "失败降级"维度）；§6（OpenAI-compatible
  ModelGateway + JSON Schema）；
- ``plan.md`` §6（ModelGateway 计划：401/timeout/结构化校验 → 重试 1 次 →
  降级到 rule_checked + semantic_skipped + audit llm_unavailable_at）；
- ``github_ref/stanza/stanza/pipeline/core.py:176-513`` —— pipeline 错误降级；
- ``stanza/pipeline/constituency_processor.py:108-115`` —— PROVIDES / REQUIRES 模式。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

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
from case_bill_validation.llm.gateway import (
    GatewayError,
    LLMCallMetadata,
    ModelGateway,
    StubModelGateway,
)
from case_bill_validation.llm.prompts import semantic_check_prompt
from case_bill_validation.persistence.audit import append_llm_call
from case_bill_validation.pipeline.core import Processor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# processor
# ---------------------------------------------------------------------------


class SemanticCheckerProcessor(Processor):
    """LLM 语义核 processor（Task 8 实装）。

    REQUIRES / PROVIDES
    -------------------
    - requires: ``{'rule_checked_fields'}``；
    - provides: ``{'semantic_checked_fields'}`` —— 字段表已 LLM 语义核。

    注入
    ----
    - ``llm_gateway``: :class:`ModelGateway` 或 :class:`StubModelGateway`；
      不传则按 settings 懒构造（生产路径）；
    - ``rule_set_provider``: 字段定义查找器（``get_field(name)``）；可空
      （语义核不依赖 193 项字段定义，仍可工作）；
    - ``max_chars``: 原文截断字符数（默认 6000）；
    """

    step_name = "semantic_checker"
    REQUIRES_DEFAULT = {"rule_checked_fields"}
    PROVIDES_DEFAULT = {"semantic_checked_fields"}

    def __init__(
        self,
        *,
        llm_gateway: Any | None = None,
        rule_set_provider: Any | None = None,
        max_chars: int = 6000,
        audit_dir: Any | None = None,
    ) -> None:
        super().__init__()
        self._llm_gateway = llm_gateway
        self._rule_set_provider = rule_set_provider
        self._max_chars = max_chars
        self._audit_dir = audit_dir

    # ------------------------------------------------------------------
    # lazy gateway
    # ------------------------------------------------------------------

    def _resolve_gateway(self) -> Any:
        if self._llm_gateway is not None:
            return self._llm_gateway
        return ModelGateway.from_settings()

    # ------------------------------------------------------------------
    # 主路径
    # ------------------------------------------------------------------

    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        if doc.needs_manual:
            logger.debug(
                "semantic_checker 跳过：case_id=%s 已标 needs_manual", doc.case_id
            )
            doc.record_step(self.step_name)
            return doc

        gateway = self._resolve_gateway()
        flagged = [
            (name, mark)
            for name, mark in doc.extracted_fields.items()
            if mark.status == FieldStatus.FLAGGED
        ]

        if not flagged:
            logger.info(
                "semantic_checker：case_id=%s 无 flagged 字段，跳过",
                doc.case_id,
            )
            doc.append_audit(
                AuditEntry(
                    actor="semantic_checker",
                    action="noop_no_flagged",
                    note="无 flagged 字段",
                )
            )
            doc.record_step(self.step_name)
            return doc

        success_count = 0
        skipped_count = 0

        # 串行 await：保证 SSE 顺序与日志追加顺序一致
        # 检测是否在已有 event loop 内（fastapi / pytest-asyncio 上下文）；
        # 若有 loop → 把调用放进外层 run_coroutine_threadsafe；
        # 否则直接 asyncio.run 同步等待。
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        for fname, mark in flagged:
            field_def = self._resolve_field_def(fname)
            messages, json_schema = semantic_check_prompt(
                field_def={"name": fname, "description": ""},
                value=mark.value,
                raw_text=doc.raw_text,
                max_chars=self._max_chars,
            )

            coro_factory = lambda: gateway.chat_structured(  # noqa: E731
                case_id=doc.case_id,
                field_name=fname,
                messages=messages,
                schema_name="semantic_check",
                json_schema=json_schema,
            )

            try:
                if running_loop is not None:
                    fut = asyncio.run_coroutine_threadsafe(coro_factory(), running_loop)
                    result = fut.result()
                else:
                    result = asyncio.run(coro_factory())
            except GatewayError as exc:
                self._handle_gateway_error(doc, fname, mark, exc)
                skipped_count += 1
                continue
            except Exception as exc:  # noqa: BLE001
                # 兜底：把任何非 GatewayError 异常包成 GatewayError 走降级
                gw_err = GatewayError(
                    f"semantic_checker 非预期异常：{exc}",
                    audit_metadata={
                        "case_id": doc.case_id,
                        "field_name": fname,
                        "model": "unknown",
                        "prompt_hash": "",
                        "response_hash": "",
                        "latency_ms": 0.0,
                        "http_code": None,
                        "downstream_status": "unavailable",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "schema_name": "semantic_check",
                        "attempts": 1,
                        "base_url": "",
                    },
                    cause=exc,
                )
                self._handle_gateway_error(doc, fname, mark, gw_err)
                skipped_count += 1
                continue

            # 成功路径：写语义证据 + 推进状态机 + 审计
            self._apply_semantic_result(doc, fname, mark, result)
            success_count += 1

        doc.append_audit(
            AuditEntry(
                actor="semantic_checker",
                action="semantic_check",
                note=(
                    f"flagged={len(flagged)} "
                    f"success={success_count} skipped={skipped_count}"
                ),
            )
        )
        doc.record_step(self.step_name)
        logger.info(
            "semantic_checker 完成：case_id=%s flagged=%d success=%d skipped=%d",
            doc.case_id,
            len(flagged),
            success_count,
            skipped_count,
        )
        return doc

    # ------------------------------------------------------------------
    # 内部：成功 / 失败处理
    # ------------------------------------------------------------------

    def _apply_semantic_result(
        self,
        doc: BillingCaseDocument,
        field_name: str,
        mark: FieldMark,
        result: dict[str, Any],
    ) -> None:
        """解析语义核响应，写入 rule_hits + 推进状态机 + 审计。"""
        raw = result.get("_raw", {}) or {}
        meta = result.get("_metadata", {}) or {}
        reason_text = str(raw.get("reason_text", "")).strip()
        new_status_raw = str(raw.get("status", "")).strip().lower()
        evidence = raw.get("evidence_span", {}) or {}

        # 落审计（success metadata）
        try:
            append_llm_call(meta, audit_dir=self._audit_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("append_llm_call 失败：%s", exc)

        # 写语义证据 RuleHit
        span = _build_span_ref(evidence, doc.raw_text)
        hit = RuleHit(
            rule_id=f"semantic:{field_name}",
            rule_source=RuleSource.SEMANTIC,
            severity=RuleSeverity.INFO,
            message_text=(
                reason_text or "（语义核未给出 reason_text）"
            ),
            span=span,
        )
        mark.add_rule_hit(hit)
        # 也写到 doc.rule_hits（便于全局规则命中面板）
        if not any(
            h.rule_id == hit.rule_id and h.span == hit.span
            for h in doc.rule_hits
        ):
            doc.rule_hits.append(hit)

        # 状态机推进：RULE_CHECKED → SEMANTIC_CHECKED
        # 注：FieldStatus.SEMANTIC_CHECKED 在 ALLOWED_FIELD_TRANSITIONS
        # 中允许由 RULE_CHECKED 推进。
        try:
            mark.mark_semantic_checked()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "semantic_checker 推进 %s 状态失败: %s", field_name, exc
            )

        # 把 reason_text 写进 mark.note 供 UI 展示
        if reason_text:
            mark = mark.model_copy(update={"note": reason_text})
            doc.extracted_fields[field_name] = mark

        doc.append_audit(
            AuditEntry(
                actor="semantic_checker",
                field=field_name,
                action="semantic_check_success",
                before_status="flagged",
                after_status="semantic_checked",
                note=reason_text[:120] if reason_text else None,
            )
        )

    def _handle_gateway_error(
        self,
        doc: BillingCaseDocument,
        field_name: str,
        mark: FieldMark,
        exc: GatewayError,
    ) -> None:
        """LLM 不可达 / 校验失败：降级到 rule_checked + semantic_skipped 标记。"""
        # 写审计（unavailable metadata）
        try:
            append_llm_call(exc.to_audit_dict(), audit_dir=self._audit_dir)
        except Exception as audit_exc:  # noqa: BLE001
            logger.warning("append_llm_call 失败：%s", audit_exc)

        # 写 RuleHit(rule_source=llm_skipped, severity=info, message='semantic_skipped')
        hit = RuleHit(
            rule_id=f"llm_skipped:{field_name}",
            rule_source=RuleSource.LLM_SKIPPED,
            severity=RuleSeverity.INFO,
            message_text="semantic_skipped",
            span=mark.span,
        )
        mark.add_rule_hit(hit)
        if not any(
            h.rule_id == hit.rule_id and h.span == hit.span
            for h in doc.rule_hits
        ):
            doc.rule_hits.append(hit)

        # 状态机降级：FLAGGED → RULE_CHECKED（per plan.md §6）
        try:
            mark.mark_rule_checked()
        except Exception as state_exc:  # noqa: BLE001
            logger.warning(
                "semantic_checker 降级字段 %s 状态失败: %s",
                field_name,
                state_exc,
            )

        doc.append_audit(
            AuditEntry(
                actor="semantic_checker",
                field=field_name,
                action="semantic_check_unavailable",
                before_status="flagged",
                after_status="rule_checked",
                note=str(exc)[:200],
            )
        )

        logger.info(
            "semantic_checker LLM 不可达 → 降级：case_id=%s field=%s err=%s",
            doc.case_id,
            field_name,
            exc,
        )

    def _resolve_field_def(self, field_name: str) -> dict[str, Any]:
        if self._rule_set_provider is None:
            return {"name": field_name, "description": ""}
        try:
            f = self._rule_set_provider.get_field(field_name)
            if f is None:
                return {"name": field_name, "description": ""}
            # 转 dict（pydantic v2 model_dump；普通对象退化为 __dict__）
            if hasattr(f, "model_dump"):
                return f.model_dump()
            return dict(getattr(f, "__dict__", {"name": field_name}))
        except Exception:  # noqa: BLE001
            return {"name": field_name, "description": ""}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_span_ref(evidence: dict[str, Any], raw_text: str) -> SpanRef | None:
    """从 LLM 返回的 evidence_span 构造 SpanRef；越界时返回 None。"""
    if not isinstance(evidence, dict):
        return None
    start = evidence.get("start_char")
    end = evidence.get("end_char")
    page_no = evidence.get("page_no")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if end <= start or start < 0 or end > len(raw_text):
        return None
    kwargs: dict[str, Any] = {"start_char": start, "end_char": end}
    if isinstance(page_no, int):
        kwargs["page_no"] = page_no
    try:
        return SpanRef(**kwargs)
    except Exception:  # noqa: BLE001
        return None


def make_semantic_checker(
    *,
    llm_gateway: Any | None = None,
    rule_set_provider: Any | None = None,
    audit_dir: Any | None = None,
) -> SemanticCheckerProcessor:
    """工厂：方便测试注入。"""
    return SemanticCheckerProcessor(
        llm_gateway=llm_gateway,
        rule_set_provider=rule_set_provider,
        audit_dir=audit_dir,
    )


__all__ = [
    "SemanticCheckerProcessor",
    "make_semantic_checker",
    # 暴露依赖便于测试
    "StubModelGateway",
    "GatewayError",
    "LLMCallMetadata",
]
