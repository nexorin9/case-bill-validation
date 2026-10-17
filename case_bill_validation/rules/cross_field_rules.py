"""跨字段时序 / 语义校验规则。

每条规则（``CrossFieldRule``）由两侧字段 + 比较器组成。比较器枚举：

- ``>=`` / ``<=`` / ``==`` —— 数值或日期序
- ``in_set`` / ``not_in_set`` —— 离散值集（rhs 字段为标签，lhs 字段为合法值集）
- ``date_diff_equals`` —— 两日期字段差值=expected_value 天（int）
- ``date_duration_equals`` —— lhs 整型天数 == |rhs 日期 - expected_value 字段日期|（task 23 新增）
- ``icd_chapter_consistent`` —— lhs ICD-10 章节应与 rhs ICD-9-CM-3 手术前缀一致（task 23 新增）

设计要点：

- 规则按 lhs 字段分组缓存为 ``self._rules_by_lhs``，避免流水线逐条
  扫描；
- 评估入口 :meth:`CrossFieldRules.evaluate(doc) -> list[RuleHit]` 仅读
  ``doc.extracted_fields``，不修改状态；状态推进由 rule_checker 处理器
  完成（后续 task 7 接入）；
- 默认 ≥10 条种子规则（task 23 扩展：诊断-手术相符性 + 实际住院天数核对日期差值）。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 比较器
# ---------------------------------------------------------------------------


class Comparator:
    """比较器标签常量（字符串 enum）。"""

    GE = ">="
    LE = "<="
    EQ = "=="
    IN_SET = "in_set"
    NOT_IN_SET = "not_in_set"
    DATE_DIFF_EQUALS = "date_diff_equals"
    DATE_DURATION_EQUALS = "date_duration_equals"        # task 23 新增
    ICD_CHAPTER_CONSISTENT = "icd_chapter_consistent"    # task 23 新增

    ALL = (
        GE, LE, EQ, IN_SET, NOT_IN_SET,
        DATE_DIFF_EQUALS, DATE_DURATION_EQUALS, ICD_CHAPTER_CONSISTENT,
    )


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossFieldRule:
    name: str
    lhs_field: str
    rhs_field: str
    comparator: str
    expected_value: str
    error_message: str
    suggested_action: str
    severity: str


@dataclass(frozen=True)
class RuleHitRecord:
    """评估期返回的命中记录（与 document.RuleHit 平行，便于解耦）。"""

    rule_id: str
    lhs_field: str
    rhs_field: str
    lhs_value: Any
    rhs_value: Any
    severity: str
    message_text: str
    suggested_action: str


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class CrossFieldRules:
    """跨字段校验规则集（内存）。"""

    def __init__(self, rules: list[CrossFieldRule]) -> None:
        self._rules = rules
        self._rules_by_lhs: dict[str, list[CrossFieldRule]] = defaultdict(list)
        for r in rules:
            self._rules_by_lhs[r.lhs_field].append(r)

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, db_path: Path) -> "CrossFieldRules":
        if not db_path.exists() or db_path.stat().st_size == 0:
            from .migrations import init_billing_db
            init_billing_db(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM cross_field_rules")
            rows = cur.fetchall()
        rules = [
            CrossFieldRule(
                name=r["name"],
                lhs_field=r["lhs_field"],
                rhs_field=r["rhs_field"],
                comparator=r["comparator"],
                expected_value=r["expected_value"] or "",
                error_message=r["error_message"],
                suggested_action=r["suggested_action"] or "",
                severity=r["severity"] or "error",
            )
            for r in rows
        ]
        return cls(rules)

    # ------------------------------------------------------------------
    # 查询 API
    # ------------------------------------------------------------------

    def all(self) -> list[CrossFieldRule]:
        return list(self._rules)

    def for_field(self, field_name: str) -> list[CrossFieldRule]:
        return list(self._rules_by_lhs.get(field_name, ()))

    def count(self) -> int:
        return len(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    # ------------------------------------------------------------------
    # 评估入口
    # ------------------------------------------------------------------

    def evaluate(self, fields: dict[str, Any]) -> list[RuleHitRecord]:
        """对字段表做完整评估，返回全部 hit（含成功与失败，便于审计）。

        - ``fields`` 须为 ``{field_name: value}``（可为 ``FieldMark.value`` 或
          直接值；本函数只读 value，不读 span/status）；
        - 缺值字段直接跳过（与 ``billing_fields`` 必填校验分工）；
        - 返回的 :class:`RuleHitRecord` 数量为「已评估规则数」（不计缺值）。
        """
        hits: list[RuleHitRecord] = []
        for rule in self._rules:
            lhs = fields.get(rule.lhs_field)
            rhs = fields.get(rule.rhs_field)
            if lhs is None or rhs is None:
                continue  # 必填校验负责

            # ----- task 23 新增：日期-天数相符（lhs int == |rhs 日期 - ref 日期|）-----
            if rule.comparator == Comparator.DATE_DURATION_EQUALS:
                hit = self._evaluate_date_duration_equals(rule, lhs, rhs, fields)
                if hit is not None:
                    hits.append(hit)
                continue

            # ----- task 23 新增：ICD 章节一致性（主诊断章节 ↔ 主手术前缀）-----
            if rule.comparator == Comparator.ICD_CHAPTER_CONSISTENT:
                hit = self._evaluate_icd_chapter_consistent(rule, lhs, rhs)
                if hit is not None:
                    hits.append(hit)
                continue

            passed, lhs_val, rhs_val = _compare(rule, lhs, rhs)
            hits.append(
                RuleHitRecord(
                    rule_id=rule.name,
                    lhs_field=rule.lhs_field,
                    rhs_field=rule.rhs_field,
                    lhs_value=lhs_val,
                    rhs_value=rhs_val,
                    severity=rule.severity if not passed else "info",
                    message_text=_format_message(rule, lhs_val, rhs_val, passed),
                    suggested_action=rule.suggested_action,
                )
            )
        return hits

    # ------------------------------------------------------------------
    # task 23 新增 comparator 评估分支
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_date_duration_equals(
        rule: CrossFieldRule,
        lhs: Any,
        rhs: Any,
        fields: dict[str, Any],
    ) -> RuleHitRecord | None:
        """``实际住院天数 == |rhs 日期 - ref 日期|``。

        - ``expected_value`` 必须以 ``"FIELD:"`` 前缀开头，后接第三个字段名（如
        ``"FIELD:出院日期"``），从 ``fields`` 中读取该日期字段实际值；
        - lhs 须可解析为 int 天数；rhs / ref 须可解析为 ``%Y-%m-%d`` 格式；
        - 任一字段缺失或格式错误则跳过（必填校验/类型校验负责，不在跨字段层
        报错）。
        """
        expected = (rule.expected_value or "").strip()
        if not expected.startswith("FIELD:"):
            return None
        ref_field = expected[len("FIELD:") :].strip()
        ref_value = fields.get(ref_field)

        start_d = _parse_date(rhs)
        end_d = _parse_date(ref_value)
        try:
            stated_days = int(lhs)
        except (TypeError, ValueError):
            return None
        if start_d is None or end_d is None:
            return None

        diff_days = (end_d - start_d).days
        passed = diff_days == stated_days

        return RuleHitRecord(
            rule_id=rule.name,
            lhs_field=rule.lhs_field,
            rhs_field=rule.rhs_field,
            lhs_value=stated_days,
            rhs_value=f"{diff_days}",
            severity=rule.severity if not passed else "info",
            message_text=_format_message(
                rule,
                stated_days,
                f"出院-入院={diff_days}天",
                passed,
            ),
            suggested_action=rule.suggested_action,
        )

    @staticmethod
    def _evaluate_icd_chapter_consistent(
        rule: CrossFieldRule, lhs: Any, rhs: Any
    ) -> RuleHitRecord | None:
        """主诊断 ICD 章节 ↔ 主手术 ICD 章节一致性。

        - ``expected_value`` 格式：``"章节:前缀,前缀|章节:前缀,..."``，例如
        ``"I:35,36,37,38,39|K:42,43,44"``；
        - lhs 取首字母（A-Z）作为诊断章节；rhs 取数字前缀（2 位或 3 位 + 可选
        小数点）；
        - 章节或前缀未在映射内则跳过（不在已知章节内不阻断）；
        - passed = rhs 任意前缀命中章节对应手术前缀集合。
        """
        diag = str(lhs).strip().upper()
        surg = str(rhs).strip()
        if not diag or not surg:
            return None
        chapter = diag[0]
        if not chapter.isalpha():
            return None

        mapping = _parse_chapter_map(rule.expected_value)
        legal_prefixes = mapping.get(chapter, set())
        if not legal_prefixes:
            return None  # 未知章节，跳过

        # 截取手术码前缀（数字部分，2-3 位）；去小数点后比较
        surg_prefix = surg.split(".")[0]
        passed = surg_prefix in legal_prefixes

        return RuleHitRecord(
            rule_id=rule.name,
            lhs_field=rule.lhs_field,
            rhs_field=rule.rhs_field,
            lhs_value=diag,
            rhs_value=surg,
            severity=rule.severity if not passed else "info",
            message_text=_format_message(
                rule,
                f"{diag}(章节 {chapter})",
                f"{surg}(前缀 {surg_prefix})",
                passed,
            ),
            suggested_action=rule.suggested_action,
        )


# ---------------------------------------------------------------------------
# 比较器实现
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        v = value.strip()
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d"):
            try:
                return datetime.strptime(v[: len(fmt) + 6], fmt) if len(v) > 10 else datetime.strptime(v, fmt)
            except ValueError:
                continue
    return None


def _parse_chapter_map(spec: str) -> dict[str, set[str]]:
    """解析 ``"I:35,36,37|K:42,43,44"`` 类章节→手术前缀映射。

    返回 ``{章节字母: {前缀集合}}``；空 / 格式错误段被忽略。
    """
    out: dict[str, set[str]] = {}
    if not spec:
        return out
    for pair in spec.split("|"):
        pair = pair.strip()
        if ":" not in pair:
            continue
        chapter, prefixes = pair.split(":", 1)
        chapter = chapter.strip()
        if not chapter:
            continue
        out[chapter] = {p.strip() for p in prefixes.split(",") if p.strip()}
    return out


def _coerce_compare_pair(rule: CrossFieldRule, lhs: Any, rhs: Any) -> tuple[Any, Any]:
    """按比较器把 lhs/rhs 转成可比较类型。"""
    if rule.comparator in (Comparator.GE, Comparator.LE, Comparator.EQ):
        # 日期优先；否则数值
        l_d = _parse_date(lhs)
        r_d = _parse_date(rhs)
        if l_d and r_d:
            return l_d, r_d
        try:
            return float(lhs), float(rhs)
        except (TypeError, ValueError):
            return lhs, rhs
    if rule.comparator in (Comparator.IN_SET, Comparator.NOT_IN_SET):
        # lhs 是值，rhs 是字符串集合（"a,b,c"）；集合内容是 rhs 字段当前值的合法集合
        return lhs, rhs  # rhs 实际上是"判别用的集合"，下面 IN/NOT_IN 里再处理
    if rule.comparator == Comparator.DATE_DIFF_EQUALS:
        l_d = _parse_date(lhs)
        r_d = _parse_date(rhs)
        return l_d, r_d
    return lhs, rhs


def _compare(
    rule: CrossFieldRule, lhs: Any, rhs: Any
) -> tuple[bool, Any, Any]:
    """执行单条规则。返回 (passed, lhs_display, rhs_display)。"""
    try:
        if rule.comparator == Comparator.GE:
            a, b = _coerce_compare_pair(rule, lhs, rhs)
            return (a >= b, lhs, rhs)
        if rule.comparator == Comparator.LE:
            a, b = _coerce_compare_pair(rule, lhs, rhs)
            return (a <= b, lhs, rhs)
        if rule.comparator == Comparator.EQ:
            a, b = _coerce_compare_pair(rule, lhs, rhs)
            return (a == b, lhs, rhs)
        if rule.comparator == Comparator.IN_SET:
            # 真实场景：lhs 是字段值，rhs 是另一字段；expected_value 是合法集合
            legal = {s.strip() for s in (rule.expected_value or "").split(",") if s.strip()}
            return (str(lhs) in legal, lhs, rhs)
        if rule.comparator == Comparator.NOT_IN_SET:
            # 语义：expected_value 列出"rhs 合法值"（离院方式 1/9），
            # 当 rhs ∈ expected_value 时 lhs 允许为空；否则（rhs 是 2/3/4）
            # lhs 必须有值。
            legal = {s.strip() for s in (rule.expected_value or "").split(",") if s.strip()}
            if str(rhs) in legal:
                # 离院方式 1 或 9：接收机构可空 → 通过
                return (not lhs or str(lhs).strip() == "", lhs, rhs)
            # 离院方式 2/3/4：必须填接收机构
            return (bool(lhs) and str(lhs).strip() != "", lhs, rhs)
        if rule.comparator == Comparator.DATE_DIFF_EQUALS:
            a, b = _coerce_compare_pair(rule, lhs, rhs)
            if not (a and b):
                return True, lhs, rhs
            diff = abs((a - b).days)
            expected = int(rule.expected_value or "0")
            return (diff == expected, lhs, rhs)
    except Exception:  # noqa: BLE001  —— 比较失败视为违规
        return False, lhs, rhs
    return False, lhs, rhs


def _format_message(
    rule: CrossFieldRule, lhs: Any, rhs: Any, passed: bool
) -> str:
    if passed:
        return f"[{rule.name}] 通过：{rule.lhs_field}={lhs} ↔ {rule.rhs_field}={rhs}"
    return (
        rule.error_message.format(lhs=lhs, rhs=rhs)
        if "{lhs}" in rule.error_message
        else f"[{rule.name}] {rule.error_message}（lhs={lhs} rhs={rhs}）"
    )


__all__ = [
    "Comparator",
    "CrossFieldRule",
    "CrossFieldRules",
    "RuleHitRecord",
]