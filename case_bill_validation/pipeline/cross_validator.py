"""跨字段时序 / 语义校验 processor（Task 7 实装）。

设计要点：

- 输入：``BillingCaseDocument``（已由 ``ExtractorProcessor`` 写入
  ``extracted_fields``，字段状态均处于 ``EXTRACTING``）；
- 输出：将 ``doc.extracted_fields`` 中涉及的 lhs/rhs 字段状态推进
  至 ``RULE_CHECKED``（全部命中即合规）或 ``FLAGGED``（任一规则不通过）；
- 调用 ``rules.cross_field_rules.CrossFieldRules.evaluate(fields)`` 拿到
  ``RuleHitRecord`` 列表，再把失败的命中映射成 ``doc.rule_hits`` 追加 +
  对应字段标 ``FLAGGED``；通过的命中只追加到 ``doc.rule_hits``（severity=info）
  便于审计追溯；
- 状态机：``EXTRACTING -> RULE_CHECKED`` 或 ``EXTRACTING -> FLAGGED``；
- 失败字段合并多个 RuleHit（去重 ``(rule_id, span)``）。
"""

from __future__ import annotations

import logging
from typing import Optional

from case_bill_validation.document import (
    AuditEntry,
    BillingCaseDocument,
    RuleHit,
    RuleSeverity,
    RuleSource,
)
from case_bill_validation.pipeline.core import Processor
from case_bill_validation.rules.cross_field_rules import CrossFieldRules

logger = logging.getLogger(__name__)


def _value_of(field_mark) -> object:
    """安全取出字段 ``value``（既支持 ``FieldMark`` 也支持裸值）。"""
    if field_mark is None:
        return None
    if hasattr(field_mark, "value"):
        return field_mark.value
    return field_mark


class CrossValidatorProcessor(Processor):
    """跨字段校验 processor（Task 7 实装）。

    REQUIRES / PROVIDES
    -------------------
    - requires: ``{'extracted_fields'}``；
    - provides: ``{'cross_validated_fields'}`` —— 字段表已跨字段校验。

    行为：
        1. 若 ``doc.extracted_fields`` 为空 → 直接落 processed_steps（占位
           跳过，不抛错）；
        2. 调 ``CrossFieldRules.evaluate({name: field.value})`` 拿 hits 列表；
        3. 把 ``hits`` 转写为 ``doc.rule_hits``，把失败涉及字段标
           ``FLAGGED``；
        4. 未被任何规则涉及的字段直接 ``mark_rule_checked()``。
    """

    step_name = "cross_validator"
    REQUIRES_DEFAULT = {"extracted_fields"}
    PROVIDES_DEFAULT = {"cross_validated_fields"}

    def __init__(self, rules: CrossFieldRules | None = None) -> None:
        super().__init__()
        self._rules = rules

    def _resolve_rules(self) -> CrossFieldRules:
        if self._rules is None:
            from case_bill_validation.config import get_settings
            from pathlib import Path

            settings = get_settings()
            self._rules = CrossFieldRules.open(Path(settings.billing_db_path))
        return self._rules

    # ------------------------------------------------------------------
    # 主路径
    # ------------------------------------------------------------------

    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        if doc.needs_manual:
            logger.debug(
                "cross_validator 跳过：case_id=%s 已标 needs_manual",
                doc.case_id,
            )
            return doc

        fields = doc.extracted_fields
        if not fields:
            logger.info(
                "cross_validator：case_id=%s 无字段可校验，跳过",
                doc.case_id,
            )
            doc.record_step(self.step_name)
            return doc

        rules = self._resolve_rules()
        # 把 FieldMark 转成纯 value dict
        flat = {name: _value_of(mark) for name, mark in fields.items()}
        hits = rules.evaluate(flat)

        # 集合：被任何规则 *引用过* 的字段名
        involved: set[str] = set()

        for hit in hits:
            involved.add(hit.lhs_field)
            involved.add(hit.rhs_field)

            if hit.severity == "info":
                # 通过的规则 → 仍写一条 info 规则命中便于审计追溯，
                # 同时把对应字段推进 RULE_CHECKED（前提是 EXTRACTING 状态）
                doc.rule_hits.append(
                    RuleHit(
                        rule_id=hit.rule_id,
                        rule_source=RuleSource.CROSS_FIELD,
                        severity=RuleSeverity.INFO,
                        message_text=hit.message_text,
                        span=self._span_of(fields, hit.lhs_field)
                        or self._span_of(fields, hit.rhs_field),
                    )
                )
                for fname in (hit.lhs_field, hit.rhs_field):
                    mark = fields.get(fname)
                    if mark is None:
                        continue
                    try:
                        if mark.status.value == "extracting":
                            mark.mark_rule_checked()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "cross_validator 字段 %s 推进 rule_checked 失败: %s",
                            fname,
                            exc,
                        )
                continue

            severity = RuleSeverity(hit.severity)
            # 失败字段标 flagged + 写 rule_hits
            for fname in (hit.lhs_field, hit.rhs_field):
                mark = fields.get(fname)
                if mark is None:
                    continue
                span = mark.span
                rule_hit = RuleHit(
                    rule_id=hit.rule_id,
                    rule_source=RuleSource.CROSS_FIELD,
                    severity=severity,
                    message_text=hit.message_text,
                    span=span,
                )
                mark.add_rule_hit(rule_hit)
                try:
                    if mark.status.value == "extracting":
                        mark.flag()
                    # 已在 flagged 时不再推进（避免状态机非法迁移）
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "cross_validator 字段 %s 状态推进失败: %s", fname, exc
                    )
                # 同时把 rule_hit 推到 doc.rule_hits（去重交给 Pydantic 由 added
                # RuleHit 自身做不了；这里用 dict 做一次稳妥去重）
                if not any(
                    h.rule_id == rule_hit.rule_id
                    and h.span == rule_hit.span
                    and h.rule_source == rule_hit.rule_source
                    for h in doc.rule_hits
                ):
                    doc.rule_hits.append(rule_hit)

        # 未被任何规则引用的字段推进 EXTRACTING -> RULE_CHECKED
        for name, mark in fields.items():
            if name in involved:
                continue
            try:
                if mark.status.value == "extracting":
                    mark.mark_rule_checked()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "cross_validator 字段 %s 推进 rule_checked 失败: %s",
                    name,
                    exc,
                )

        # 审计流水
        doc.append_audit(
            AuditEntry(
                actor="cross_validator",
                action="cross_field_validation",
                note=(
                    f"rules={rules.count()} "
                    f"involved_fields={len(involved)} "
                    f"hits={len(hits)}"
                ),
            )
        )
        doc.record_step(self.step_name)
        logger.info(
            "cross_validator 完成：case_id=%s rules=%d hits=%d flagged=%s",
            doc.case_id,
            rules.count(),
            len(hits),
            doc.flagged_field_names(),
        )
        return doc

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _span_of(fields: dict, name: str):
        m = fields.get(name)
        if m is None:
            return None
        return getattr(m, "span", None)


def make_cross_validator(rules: Optional[CrossFieldRules] = None) -> CrossValidatorProcessor:
    """工厂：方便测试时注入。"""
    return CrossValidatorProcessor(rules=rules)


__all__ = ["CrossValidatorProcessor", "make_cross_validator"]
