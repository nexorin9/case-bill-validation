"""权威规则层：193 项字段规范、违规负面清单、跨字段校验。

加载顺序（启动期）::

    init_billing_db(db_path) -> BillingFieldRuleSet.open(db_path)
    -> ViolationCatalog.open(db_path)
    -> CrossFieldRules.open(db_path)

任一加载失败均抛 :class:`case_bill_validation.errors.ConfigurationError`，
由 server 启动逻辑捕获后选择降级策略。

权威源：

- ``domain/dictionaries/医保结算清单字段规范.md``  (193 项分区与关键字段)
- ``domain/rules/医保违规负面清单.md``  (条例第 38/40 条)
"""

from __future__ import annotations

from .field_ruleset import BillingField, BillingFieldRuleSet
from .violation_catalog import ViolationCatalog, ViolationEntry
from .cross_field_rules import (
    Comparator,
    CrossFieldRule,
    CrossFieldRules,
)

__all__ = [
    "BillingField",
    "BillingFieldRuleSet",
    "ViolationCatalog",
    "ViolationEntry",
    "Comparator",
    "CrossFieldRule",
    "CrossFieldRules",
]