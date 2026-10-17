"""规则审查 processor（Task 7 实装）。

设计要点：

- 输入：``BillingCaseDocument``（已由 ``ExtractorProcessor`` 写入
  ``extracted_fields``，字段状态均处于 ``EXTRACTING``）；
- 输出：将 ``doc.extracted_fields`` 按 ``BillingFieldRuleSet`` 必填 + 类型 +
  编码区间做审查，失败字段标 ``FLAGGED`` + ``rule_hits`` 追加 ``RuleHit``；
- 状态机：``EXTRACTING -> RULE_CHECKED`` 或 ``EXTRACTING -> FLAGGED``；
- 本任务实装 **必填校验 + 类型校验**（日期 / 枚举 / int / float /
  str-非空）；ICD-10 / ICD-9-CM-3 编码区间校验由 **Task 24** 实装，本任务
  仅做最基本的存在性 + 字母前缀校验；
- 必填缺失的字段若 ``status`` 已是 ``FLAGGED``（来源 extractor），保持不
  变；其余走 ``flag()`` 推进。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from case_bill_validation.document import (
    AuditEntry,
    BillingCaseDocument,
    RuleHit,
    RuleSeverity,
    RuleSource,
)
from case_bill_validation.pipeline.core import Processor
from case_bill_validation.rules.code_validators import (
    validation_error_message,
    validate_code,
)
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 类型校验子句（与 BillingField.field_type / value_constraint 配套）
# ---------------------------------------------------------------------------


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _validate_field_type(name: str, value, field_type: str, constraint: str) -> str | None:
    """返回错误消息或 ``None``。"""
    if _is_blank(value):
        return None  # 必填校验负责
    if field_type == "str":
        if not isinstance(value, str):
            return f"字段 {name} 类型错误：期望 str 实际 {type(value).__name__}"
        return None
    if field_type == "date":
        if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
            return f"字段 {name} 日期格式错误：期望 YYYY-MM-DD 实际 {value!r}"
        return None
    if field_type == "int":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return f"字段 {name} 类型错误：期望 int 实际 {type(value).__name__}"
        if isinstance(value, str) and not _INT_RE.match(value.strip()):
            return f"字段 {name} 整型格式错误：{value!r}"
        # int 也按 range 约束做
        if constraint.startswith("range:"):
            try:
                lo, hi = constraint[6:].split(",")
                lo_f, hi_f = float(lo), float(hi)
            except Exception:  # noqa: BLE001
                return None
            try:
                v = float(value)
            except Exception:  # noqa: BLE001
                return None
            if v < lo_f or v > hi_f:
                return f"字段 {name} 数值越界：{v} ∉ [{lo_f}, {hi_f}]"
        return None
    if field_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return f"字段 {name} 类型错误：期望 float 实际 {type(value).__name__}"
        if isinstance(value, str) and not _FLOAT_RE.match(value.strip()):
            return f"字段 {name} 浮点格式错误：{value!r}"
        # 浮点类型也按 range 约束做
        if constraint.startswith("range:"):
            try:
                lo, hi = constraint[6:].split(",")
                lo_f, hi_f = float(lo), float(hi)
            except Exception:  # noqa: BLE001
                return None
            try:
                v = float(value)
            except Exception:  # noqa: BLE001
                return None
            if v < lo_f or v > hi_f:
                return f"字段 {name} 数值越界：{v} ∉ [{lo_f}, {hi_f}]"
        return None
    if field_type == "enum":
        if not isinstance(value, str):
            return f"字段 {name} 枚举值错误：非字符串 {value!r}"
        # constraint 形式 "in:foo,bar,baz"
        if constraint.startswith("in:"):
            legal = {s.strip() for s in constraint[3:].split(",") if s.strip()}
            if value not in legal:
                return (
                    f"字段 {name} 枚举值非法：{value!r} 不在 {sorted(legal)} 中"
                )
        return None
    if field_type == "icd10":
        # ICD-10 编码区间校验（Task 24）：委托 rules.code_validators
        if not validate_code(value, "icd10"):
            return validation_error_message(name, value, "icd10")
        return None
    if field_type == "icd9_cm3":
        # ICD-9-CM-3 编码区间校验（Task 24）：委托 rules.code_validators
        if not validate_code(value, "icd9_cm3"):
            return validation_error_message(name, value, "icd9_cm3")
        return None
    if constraint.startswith("range:"):
        # 数值范围
        try:
            lo, hi = constraint[6:].split(",")
            lo_f, hi_f = float(lo), float(hi)
        except Exception:  # noqa: BLE001
            return None
        try:
            v = float(value)
        except Exception:  # noqa: BLE001
            return None
        if v < lo_f or v > hi_f:
            return f"字段 {name} 数值越界：{v} ∉ [{lo_f}, {hi_f}]"
    if constraint.startswith("^") and isinstance(value, str):
        if not re.match(constraint, value):
            return f"字段 {name} 不匹配正则 {constraint!r}：{value!r}"
    return None


class RuleCheckerProcessor(Processor):
    """规则审查 processor（Task 7 实装）。

    REQUIRES / PROVIDES
    -------------------
    - requires: ``{'extracted_fields'}``；
    - provides: ``{'rule_checked_fields'}`` —— 字段表已规则审查。
    """

    step_name = "rule_checker"
    REQUIRES_DEFAULT = {"extracted_fields"}
    PROVIDES_DEFAULT = {"rule_checked_fields"}

    def __init__(
        self,
        rule_set: BillingFieldRuleSet | None = None,
        known_extracted: set[str] | None = None,
    ) -> None:
        super().__init__()
        self._rule_set = rule_set
        # 仅在已抽取字段列表内做必填校验；其余必填字段由后续 LLM / 手工录入
        self._known_extracted = known_extracted

    def _resolve_rule_set(self) -> BillingFieldRuleSet:
        if self._rule_set is None:
            from case_bill_validation.config import get_settings
            from pathlib import Path

            settings = get_settings()
            self._rule_set = BillingFieldRuleSet.open(
                Path(settings.billing_db_path)
            )
        return self._rule_set

    # ------------------------------------------------------------------
    # 主路径
    # ------------------------------------------------------------------

    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        if doc.needs_manual:
            logger.debug(
                "rule_checker 跳过：case_id=%s 已标 needs_manual", doc.case_id
            )
            return doc

        rule_set = self._resolve_rule_set()
        fields = doc.extracted_fields

        # 已知抽取字段集合：默认走本次实际 extracted_fields 的 keys
        known = self._known_extracted
        if known is None:
            known = set(fields.keys())

        missing_hits = 0
        type_hits = 0
        type_checked = 0
        icd_code_hits = 0
        passed = 0
        already_flagged_fields: set[str] = set()

        # 必填校验：仅针对 known 中字段
        for fname in sorted(known):
            mark = fields.get(fname)
            spec = rule_set.get_field(fname)
            required = spec.required if spec else False
            # 若 spec 不存在（字段名不在 193 项内），必填视为 False，仅做类型
            if mark is None:
                if required:
                    rule_hit = RuleHit(
                        rule_id=f"required:{fname}",
                        rule_source=RuleSource.REQUIRED,
                        severity=RuleSeverity.ERROR,
                        message_text=f"必填字段 {fname} 缺失",
                    )
                    doc.rule_hits.append(rule_hit)
                    missing_hits += 1
                continue

            value = mark.value
            is_blank_value = _is_blank(value)

            # 必填缺失
            if required and is_blank_value:
                # 注意：extractor 已经把双重失败的字段标为 FLAGGED；
                # 这里只标新规则的 rule_hit，不重复推进状态机
                rule_hit = RuleHit(
                    rule_id=f"required:{fname}",
                    rule_source=RuleSource.REQUIRED,
                    severity=RuleSeverity.ERROR,
                    message_text=f"必填字段 {fname} 缺失（值为空）",
                    span=mark.span,
                )
                if not any(
                    h.rule_id == rule_hit.rule_id and h.span == rule_hit.span
                    for h in doc.rule_hits
                ):
                    doc.rule_hits.append(rule_hit)
                mark.add_rule_hit(rule_hit)
                missing_hits += 1
                if mark.status.value == "extracting":
                    mark.flag()
                    already_flagged_fields.add(fname)
                continue

            # 类型校验（仅在 spec 存在且 value 非空时）
            if spec is None:
                # 未知字段：不做类型校验（不在 193 项之内）
                if mark.status.value == "extracting":
                    mark.mark_rule_checked()
                passed += 1
                continue

            # ICD-10 / ICD-9-CM-3 编码区间校验（Task 24）：
            # 单独走 validate_code，rule_source=invalid_code，便于
            # 医保办按 ICD 错误单独过滤 + 申诉。
            if spec.field_type in ("icd10", "icd9_cm3"):
                if not validate_code(value, spec.field_type):
                    rule_hit = RuleHit(
                        rule_id=f"invalid_code:{fname}",
                        rule_source=RuleSource.INVALID_CODE,
                        severity=RuleSeverity.ERROR,
                        message_text=validation_error_message(
                            fname, value, spec.field_type
                        ),
                        span=mark.span,
                    )
                    if not any(
                        h.rule_id == rule_hit.rule_id
                        and h.span == rule_hit.span
                        for h in doc.rule_hits
                    ):
                        doc.rule_hits.append(rule_hit)
                    mark.add_rule_hit(rule_hit)
                    icd_code_hits += 1
                    if mark.status.value == "extracting":
                        mark.flag()
                        already_flagged_fields.add(fname)
                    continue
                # ICD 编码合法：推进 RULE_CHECKED
                if mark.status.value == "extracting":
                    mark.mark_rule_checked()
                passed += 1
                continue

            err = _validate_field_type(
                fname, value, spec.field_type, spec.value_constraint or ""
            )
            type_checked += 1
            if err is not None:
                rule_hit = RuleHit(
                    rule_id=f"type:{fname}",
                    rule_source=RuleSource.TYPE_CHECK,
                    severity=RuleSeverity.ERROR,
                    message_text=err,
                    span=mark.span,
                )
                if not any(
                    h.rule_id == rule_hit.rule_id and h.span == rule_hit.span
                    for h in doc.rule_hits
                ):
                    doc.rule_hits.append(rule_hit)
                mark.add_rule_hit(rule_hit)
                type_hits += 1
                if mark.status.value == "extracting":
                    mark.flag()
                    already_flagged_fields.add(fname)
                continue

            # 通过：推进 RULE_CHECKED
            if mark.status.value == "extracting":
                mark.mark_rule_checked()
            passed += 1

        # 审计流水
        doc.append_audit(
            AuditEntry(
                actor="rule_checker",
                action="rule_check",
                note=(
                    f"checked={type_checked} passed={passed} "
                    f"missing={missing_hits} type_errors={type_hits} "
                    f"icd_code_errors={icd_code_hits} "
                    f"flagged={sorted(already_flagged_fields)}"
                ),
            )
        )
        doc.record_step(self.step_name)
        logger.info(
            "rule_checker 完成：case_id=%s passed=%d missing=%d "
            "type_errors=%d icd_code_errors=%d",
            doc.case_id,
            passed,
            missing_hits,
            type_hits,
            icd_code_hits,
        )
        return doc


def make_rule_checker(
    rule_set: Optional[BillingFieldRuleSet] = None,
    known_extracted: set[str] | None = None,
) -> RuleCheckerProcessor:
    """工厂：方便测试时注入。"""
    return RuleCheckerProcessor(
        rule_set=rule_set, known_extracted=known_extracted
    )


__all__ = ["RuleCheckerProcessor", "make_rule_checker"]
