"""Span 绑定校验（Task 6）专项测试。

覆盖：

- span 必落在原文 raw_text 边界内（start_char >= 0 且 end_char <= len）；
- span 内文本 (``raw_text[start:end]``) 与 FieldMark.value 一致；
- 同一字段多次抽取时 span 唯一（同一 case_id 内同一字段名不出现两个 span）；
- 越界 span 在 BillingCaseDocument._validate_spans_within_raw_text 阶段被拒；
- Processor 端：抽出 span 后能 ``text_of(raw_text)`` 取回原值。

参考：

- 任务描述：M3.b span 须落在原文边界内 + 唯一；
- 测试 fixtures：``tests/fixtures/discharge_0[1-5].txt``。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldMark,
    SpanRef,
)
from case_bill_validation.pipeline.extractor import (
    ExtractorProcessor,
)


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _build_dummy_rule_set() -> object:
    """构造目标字段的最小 BillingFieldRuleSet（绕开 193 项不变式）。"""
    from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet
    from case_bill_validation.rules.migrations import FieldSpec

    names = ["入院日期", "出院日期", "实际住院天数", "性别", "年龄"]
    specs = [
        FieldSpec(name=n, section="住院诊疗", field_type="str", required=False,
                  value_constraint=".*", description="", related_violation="", example_value="")
        for n in names
    ]
    rs = BillingFieldRuleSet.__new__(BillingFieldRuleSet)
    rs._fields = {s.name: __import__("case_bill_validation.rules.field_ruleset",
                                      fromlist=["BillingField"]).BillingField(
        name=s.name, section=s.section, field_type=s.field_type, required=s.required,
        value_constraint=s.value_constraint, description=s.description,
        related_violation=s.related_violation, example_value=s.example_value,
    ) for s in specs}
    return rs


class TestSpanRef:
    def test_spanref_text_of_returns_substring(self) -> None:
        raw = "入院日期：2026-01-12"
        s = SpanRef(start_char=5, end_char=15)
        assert s.text_of(raw) == "2026-01-12"

    def test_spanref_end_must_be_greater_than_start(self) -> None:
        with pytest.raises(ValidationError):
            SpanRef(start_char=5, end_char=5)
        with pytest.raises(ValidationError):
            SpanRef(start_char=10, end_char=5)

    def test_spanref_negative_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SpanRef(start_char=-1, end_char=5)

    def test_spanref_frozen_blocks_mutation(self) -> None:
        s = SpanRef(start_char=0, end_char=10)
        with pytest.raises(ValidationError):
            s.start_char = 5  # type: ignore[misc]


class TestSpanWithinRawText:
    def test_billing_case_document_rejects_span_beyond_end(self) -> None:
        raw = "短文本" * 5  # 15 字
        s = SpanRef(start_char=0, end_char=20)  # 越界
        with pytest.raises(ValidationError):
            BillingCaseDocument(
                case_id="c1", raw_text=raw, spans=[s]
            )

    def test_billing_case_document_rejects_field_span_beyond_end(self) -> None:
        raw = "短文本"  # 6 字
        s = SpanRef(start_char=0, end_char=10)
        mark = FieldMark(
            field_name="入院日期",
            value="2026-01-12",
            span=s,
            confidence=0.9,
        )
        with pytest.raises(ValidationError):
            BillingCaseDocument(
                case_id="c1", raw_text=raw, extracted_fields={"入院日期": mark}
            )

    def test_billing_case_document_accepts_valid_span(self) -> None:
        raw = "入院日期：2026-01-12 出院日期：2026-01-26"
        s = SpanRef(start_char=5, end_char=15)
        mark = FieldMark(
            field_name="入院日期",
            value="2026-01-12",
            span=s,
            confidence=0.9,
        )
        doc = BillingCaseDocument(
            case_id="c1", raw_text=raw, extracted_fields={"入院日期": mark}
        )
        assert doc.extracted_fields["入院日期"].span is not None
        assert doc.extracted_fields["入院日期"].span.text_of(raw) == "2026-01-12"


class TestExtractorSpanBinding:
    def test_extracted_span_points_to_value_in_raw_text(self) -> None:
        """Extractor 抽出的 span.text_of(raw) == value。"""
        rule_set = _build_dummy_rule_set()
        proc = ExtractorProcessor(
            rule_set=rule_set, target_field_names=["入院日期"]
        )
        raw = (_FIXTURES_DIR / "discharge_01.txt").read_text(encoding="utf-8")
        doc = BillingCaseDocument(case_id="c1", raw_text=raw)
        out = proc.process(doc)
        m = out.extracted_fields["入院日期"]
        assert m.span is not None
        assert m.span.text_of(raw) == m.value

    def test_each_field_has_unique_span(self) -> None:
        """同一字段不出现多个 span（即不会重复 upsert 同一字段名）。"""
        rule_set = _build_dummy_rule_set()
        proc = ExtractorProcessor(
            rule_set=rule_set,
            target_field_names=["入院日期", "出院日期", "实际住院天数", "性别"],
        )
        raw = (_FIXTURES_DIR / "discharge_01.txt").read_text(encoding="utf-8")
        doc = BillingCaseDocument(case_id="c1", raw_text=raw)
        out = proc.process(doc)
        # 每个字段在 dict 中只有一份
        assert len(out.extracted_fields) == 4
        # 每个字段的 span 唯一（dict key 唯一即满足）
        for name, m in out.extracted_fields.items():
            assert m.span is not None, f"字段 {name} 缺 span"
            assert m.span.end_char > m.span.start_char
            # span 落在 raw_text 内
            assert m.span.end_char <= len(raw)
            # span 与 value 一致
            assert raw[m.span.start_char:m.span.end_char] == m.value

    def test_no_span_when_extraction_fails(self) -> None:
        """抽取失败时 span 为 None（而非越界/伪 span）。"""
        rule_set = _build_dummy_rule_set()
        proc = ExtractorProcessor(
            rule_set=rule_set, target_field_names=["入院日期"]
        )
        raw = "纯文本" * 5  # 没有任何关键信息
        doc = BillingCaseDocument(case_id="c1", raw_text=raw)
        out = proc.process(doc)
        m = out.extracted_fields["入院日期"]
        assert m.span is None
        assert m.status.value == "flagged"  # needs_manual → flagged

    def test_span_end_equals_value_length(self) -> None:
        """span 长度等于 value 字符串长度。"""
        rule_set = _build_dummy_rule_set()
        proc = ExtractorProcessor(
            rule_set=rule_set, target_field_names=["入院日期", "出院日期"]
        )
        raw = (_FIXTURES_DIR / "discharge_02.txt").read_text(encoding="utf-8")
        doc = BillingCaseDocument(case_id="c2", raw_text=raw)
        out = proc.process(doc)
        for name, m in out.extracted_fields.items():
            assert m.span is not None
            assert m.span.end_char - m.span.start_char == len(str(m.value))