"""降级测试：LLM 不可达时流水线仍输出 status='rule_checked' + semantic_skipped。

Task 8 实装：
- ``SemanticCheckerProcessor`` 调 ``ModelGateway``；
- 失败 → 字段保持 ``RULE_CHECKED`` + ``RuleHit(rule_source=llm_skipped,
  severity=info, message='semantic_skipped')`` + 审计流水记录；
- 不静默写数据（业务状态机不在失败时推进）；
- ``logs/audit/llm_calls.jsonl`` 记录 unavailable 审计条目。

参考：
- ``spec.md`` §5（"失败降级"维度）；
- ``plan.md`` §6（降级路径）；
- 真实路径（gate-running）由 ``Task 9 + 13`` 集成测试覆盖，本测试
  聚焦单 processor 行为。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldMark,
    FieldSource,
    FieldStatus,
    RuleSeverity,
    RuleSource,
    SpanRef,
)
from case_bill_validation.llm.gateway import GatewayError, StubModelGateway
from case_bill_validation.pipeline.semantic_checker import (
    SemanticCheckerProcessor,
    make_semantic_checker,
)


# ---------------------------------------------------------------------------
# helper：构造一份"已被规则审查标 flagged"的 doc
# ---------------------------------------------------------------------------


def _make_doc_with_flagged_field(audit_dir: Path) -> tuple[BillingCaseDocument, str]:
    raw_text = (
        "出院小结\n"
        "姓名：张三\n"
        "入院日期：2026-01-12\n"
        "出院日期：2026-01-30\n"
        "主要诊断代码：A99\n"  # 故意非 ICD-10 编码 → rule_checker 失败 → flagged
        "主要手术操作日期：2025-12-01\n"  # 早于入院日期 → cross_validator 失败
        "实际住院天数：18\n"
    )
    doc = BillingCaseDocument(case_id="case-degrade-001", raw_text=raw_text)

    # 模拟 cross_validator 已经把"主要手术操作日期"标 flagged
    flagged_field = FieldMark(
        field_name="主要手术操作日期",
        value="2025-12-01",
        span=SpanRef(start_char=raw_text.index("2025-12-01"), end_char=raw_text.index("2025-12-01") + 10),
        confidence=0.9,
        status=FieldStatus.FLAGGED,
        source=FieldSource.REGEX,
    )
    doc.upsert_field(flagged_field)

    # 模拟 rule_checker 已经把"主要诊断代码"标 flagged
    diag_field = FieldMark(
        field_name="主要诊断代码",
        value="A99",  # 不合法 ICD-10 → rule_checker 失败
        span=SpanRef(start_char=raw_text.index("A99"), end_char=raw_text.index("A99") + 3),
        confidence=0.9,
        status=FieldStatus.FLAGGED,
        source=FieldSource.REGEX,
    )
    doc.upsert_field(diag_field)

    # 1 个普通 rule_checked 字段（应保持 RULE_CHECKED 不变）
    normal = FieldMark(
        field_name="实际住院天数",
        value="18",
        span=SpanRef(start_char=raw_text.index("18"), end_char=raw_text.index("18") + 2),
        confidence=0.9,
        status=FieldStatus.RULE_CHECKED,
        source=FieldSource.REGEX,
    )
    doc.upsert_field(normal)

    return doc, raw_text


# ---------------------------------------------------------------------------
# 1. LLM 不可达 → 字段降级到 rule_checked + semantic_skipped
# ---------------------------------------------------------------------------


def test_semantic_checker_degrades_on_gateway_error(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    doc, _ = _make_doc_with_flagged_field(audit_dir)

    # StubModelGateway 把所有调用都置为失败
    stub = StubModelGateway(
        errors={
            "主要诊断代码": GatewayError(
                "stub unavailable",
                audit_metadata={
                    "case_id": doc.case_id,
                    "field_name": "主要诊断代码",
                    "model": "stub",
                    "prompt_hash": "",
                    "response_hash": "",
                    "latency_ms": 0.0,
                    "http_code": None,
                    "downstream_status": "unavailable",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "schema_name": "semantic_check",
                    "attempts": 1,
                    "base_url": "stub://local",
                },
            ),
            "主要手术操作日期": GatewayError(
                "stub unavailable",
                audit_metadata={
                    "case_id": doc.case_id,
                    "field_name": "主要手术操作日期",
                    "model": "stub",
                    "prompt_hash": "",
                    "response_hash": "",
                    "latency_ms": 0.0,
                    "http_code": None,
                    "downstream_status": "unavailable",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "schema_name": "semantic_check",
                    "attempts": 1,
                    "base_url": "stub://local",
                },
            ),
        }
    )

    proc = SemanticCheckerProcessor(llm_gateway=stub, audit_dir=audit_dir)
    out_doc = proc.process(doc)

    # 1) 流水线不中断
    assert out_doc is doc
    assert "semantic_checker" in out_doc.processed_steps

    # 2) flagged 字段保持 RULE_CHECKED，不被推进到 SEMANTIC_CHECKED
    assert out_doc.extracted_fields["主要诊断代码"].status == FieldStatus.RULE_CHECKED
    assert out_doc.extracted_fields["主要手术操作日期"].status == FieldStatus.RULE_CHECKED

    # 3) 字段表里追加 RuleHit(rule_source=llm_skipped, severity=info, message='semantic_skipped')
    for fname in ("主要诊断代码", "主要手术操作日期"):
        mark = out_doc.extracted_fields[fname]
        llm_skipped_hits = [
            h
            for h in mark.rule_hits
            if h.rule_source == RuleSource.LLM_SKIPPED
        ]
        assert len(llm_skipped_hits) >= 1, f"{fname} 缺 LLM_SKIPPED 规则命中"
        assert llm_skipped_hits[0].severity == RuleSeverity.INFO
        assert llm_skipped_hits[0].message_text == "semantic_skipped"

    # 4) doc.rule_hits 同步追加 llm_skipped
    assert any(
        h.rule_source == RuleSource.LLM_SKIPPED for h in out_doc.rule_hits
    )

    # 5) 审计流水记 "semantic_check_unavailable"
    unavailable_audits = [
        e
        for e in out_doc.audit_log
        if e.action == "semantic_check_unavailable"
    ]
    assert len(unavailable_audits) == 2

    # 6) llm_calls.jsonl 写两条 unavailable 记录
    llm_log = audit_dir / "llm_calls.jsonl"
    assert llm_log.exists()
    lines = llm_log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(ln) for ln in lines]
    assert all(p["downstream_status"] == "unavailable" for p in parsed)
    assert {p["field_name"] for p in parsed} == {
        "主要诊断代码",
        "主要手术操作日期",
    }


# ---------------------------------------------------------------------------
# 2. LLM 不可达 → 流水线最终 audit_log 含 semantic_check 条目（action+计数）
# ---------------------------------------------------------------------------


def test_semantic_checker_logs_semantic_check_audit_summary_on_failure(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    doc, _ = _make_doc_with_flagged_field(audit_dir)

    stub = StubModelGateway(
        errors={
            "主要诊断代码": GatewayError("stub unavailable", audit_metadata={
                "case_id": doc.case_id, "field_name": "主要诊断代码",
                "model": "stub", "prompt_hash": "", "response_hash": "",
                "latency_ms": 0.0, "http_code": None,
                "downstream_status": "unavailable",
                "timestamp": "2026-01-01T00:00:00Z",
                "schema_name": "semantic_check", "attempts": 1,
                "base_url": "stub://local",
            }),
            "主要手术操作日期": GatewayError("stub unavailable", audit_metadata={
                "case_id": doc.case_id, "field_name": "主要手术操作日期",
                "model": "stub", "prompt_hash": "", "response_hash": "",
                "latency_ms": 0.0, "http_code": None,
                "downstream_status": "unavailable",
                "timestamp": "2026-01-01T00:00:00Z",
                "schema_name": "semantic_check", "attempts": 1,
                "base_url": "stub://local",
            }),
        }
    )
    proc = SemanticCheckerProcessor(llm_gateway=stub, audit_dir=audit_dir)
    out_doc = proc.process(doc)

    summary_audits = [
        e
        for e in out_doc.audit_log
        if e.action == "semantic_check"
    ]
    assert len(summary_audits) == 1
    note = summary_audits[0].note or ""
    assert "flagged=2" in note
    assert "success=0" in note
    assert "skipped=2" in note


# ---------------------------------------------------------------------------
# 3. LLM 成功 → 字段推进到 SEMANTIC_CHECKED + 写语义证据
# ---------------------------------------------------------------------------


def test_semantic_checker_success_advances_field_to_semantic_checked(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    doc, raw_text = _make_doc_with_flagged_field(audit_dir)

    diag_value = doc.extracted_fields["主要诊断代码"].value
    diag_span = doc.extracted_fields["主要诊断代码"].span
    stub = StubModelGateway(
        responses={
            "主要诊断代码": {
                "field": "主要诊断代码",
                "status": "confirmed",
                "reason_text": "依据：原文第 N 行明确记载",
                "evidence_span": {
                    "start_char": diag_span.start_char,
                    "end_char": diag_span.end_char,
                },
            },
            "主要手术操作日期": {
                "field": "主要手术操作日期",
                "status": "flagged",
                "reason_text": "语义核确认：手术日期早于入院日期 12 天",
                "evidence_span": {
                    "start_char": raw_text.index("2025-12-01"),
                    "end_char": raw_text.index("2025-12-01") + 10,
                },
            },
        }
    )

    proc = SemanticCheckerProcessor(llm_gateway=stub, audit_dir=audit_dir)
    out_doc = proc.process(doc)

    # 字段状态推进到 SEMANTIC_CHECKED
    assert (
        out_doc.extracted_fields["主要诊断代码"].status
        == FieldStatus.SEMANTIC_CHECKED
    )
    assert (
        out_doc.extracted_fields["主要手术操作日期"].status
        == FieldStatus.SEMANTIC_CHECKED
    )
    # 字段 mark 上加语义 RuleHit
    diag_mark = out_doc.extracted_fields["主要诊断代码"]
    sem_hits = [h for h in diag_mark.rule_hits if h.rule_source == RuleSource.SEMANTIC]
    assert len(sem_hits) == 1
    assert "依据" in sem_hits[0].message_text
    # note 字段更新为 reason_text（便于 UI 展示）
    assert "依据" in (diag_mark.note or "")

    # llm_calls.jsonl 落两条 success
    llm_log = audit_dir / "llm_calls.jsonl"
    assert llm_log.exists()
    parsed = [
        json.loads(ln)
        for ln in llm_log.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert all(p["downstream_status"] == "success" for p in parsed)
    assert len(parsed) == 2


# ---------------------------------------------------------------------------
# 4. needs_manual 文档 → processor 跳过不调 LLM（无 llm_calls.jsonl）
# ---------------------------------------------------------------------------


def test_semantic_checker_skips_when_doc_needs_manual(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    doc, _ = _make_doc_with_flagged_field(audit_dir)
    doc.needs_manual = True

    # 即使 stub 配置了响应，也不会被调用
    stub = StubModelGateway(
        responses={
            "主要诊断代码": {
                "field": "主要诊断代码",
                "status": "confirmed",
                "reason_text": "ok",
                "evidence_span": {"start_char": 0, "end_char": 1},
            }
        }
    )
    proc = SemanticCheckerProcessor(llm_gateway=stub, audit_dir=audit_dir)
    out_doc = proc.process(doc)

    # 字段状态保持不变（仍 FLAGGED）
    assert out_doc.extracted_fields["主要诊断代码"].status == FieldStatus.FLAGGED
    # stub 完全没被调用
    assert stub.call_log == []
    # llm_calls.jsonl 不存在或为空
    llm_log = audit_dir / "llm_calls.jsonl"
    assert (not llm_log.exists()) or llm_log.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# 5. 无 flagged 字段 → 跳过 LLM 调用（noop_no_flagged）
# ---------------------------------------------------------------------------


def test_semantic_checker_skips_when_no_flagged_fields(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    doc = BillingCaseDocument(
        case_id="case-degrade-005",
        raw_text="简单文本",
    )
    # 加 1 个 RULE_CHECKED 字段（不是 flagged）
    doc.upsert_field(
        FieldMark(
            field_name="姓名",
            value="张三",
            span=SpanRef(start_char=0, end_char=2),
            confidence=0.9,
            status=FieldStatus.RULE_CHECKED,
            source=FieldSource.REGEX,
        )
    )

    stub = StubModelGateway()  # 没配置响应
    proc = SemanticCheckerProcessor(llm_gateway=stub, audit_dir=audit_dir)
    out_doc = proc.process(doc)

    # audit_log 有 noop_no_flagged
    assert any(
        e.action == "noop_no_flagged" for e in out_doc.audit_log
    )
    # stub 没被调用
    assert stub.call_log == []


# ---------------------------------------------------------------------------
# 6. evidence_span 越界 → span=None，但不阻断流水线
# ---------------------------------------------------------------------------


def test_semantic_checker_tolerates_out_of_bounds_evidence_span(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    doc, _ = _make_doc_with_flagged_field(audit_dir)
    raw_text = doc.raw_text
    diag_span = doc.extracted_fields["主要诊断代码"].span

    stub = StubModelGateway(
        responses={
            "主要诊断代码": {
                "field": "主要诊断代码",
                "status": "confirmed",
                "reason_text": "ok",
                # 故意越界
                "evidence_span": {
                    "start_char": 0,
                    "end_char": len(raw_text) + 999,
                },
            },
            "主要手术操作日期": {
                "field": "主要手术操作日期",
                "status": "confirmed",
                "reason_text": "ok",
                "evidence_span": {
                    "start_char": 0,
                    "end_char": 5,
                },
            },
        }
    )
    proc = SemanticCheckerProcessor(llm_gateway=stub, audit_dir=audit_dir)
    out_doc = proc.process(doc)

    # 字段仍推进到 SEMANTIC_CHECKED
    assert (
        out_doc.extracted_fields["主要诊断代码"].status
        == FieldStatus.SEMANTIC_CHECKED
    )
    # 越界 span 被替换为 None（语义证据落空，但不阻断）
    sem_hits = [
        h
        for h in out_doc.extracted_fields["主要诊断代码"].rule_hits
        if h.rule_source == RuleSource.SEMANTIC
    ]
    assert len(sem_hits) == 1
    assert sem_hits[0].span is None


# ---------------------------------------------------------------------------
# 7. make_semantic_checker 工厂可用
# ---------------------------------------------------------------------------


def test_make_semantic_checker_factory_works(tmp_path: Path):
    stub = StubModelGateway()
    proc = make_semantic_checker(llm_gateway=stub, audit_dir=tmp_path / "audit")
    assert isinstance(proc, SemanticCheckerProcessor)
    assert proc._llm_gateway is stub
    assert proc._audit_dir == tmp_path / "audit"


# ---------------------------------------------------------------------------
# 8. 全流水线降级：拼起 5 个 processor 时 LLM 不可达 → 仍跑通
# ---------------------------------------------------------------------------


def test_full_pipeline_degrades_when_llm_unavailable(tmp_path: Path):
    """与 ``test_pipeline_order.py`` 互补：拼接 5 processor + 注入 stub 失败
    GatewayError → 流水线仍跑完 + 字段降级到 RULE_CHECKED。"""
    from case_bill_validation.pipeline.core import BillingValidationPipeline
    from case_bill_validation.pipeline.cross_validator import (
        CrossValidatorProcessor,
    )
    from case_bill_validation.pipeline.extractor import ExtractorProcessor
    from case_bill_validation.pipeline.rule_checker import RuleCheckerProcessor
    from case_bill_validation.pipeline.segmenter import SegmenterProcessor
    from case_bill_validation.rules.cross_field_rules import (
        Comparator,
        CrossFieldRule,
        CrossFieldRules,
    )
    from case_bill_validation.rules.field_ruleset import (
        BillingFieldRuleSet,
    )
    from case_bill_validation.rules.migrations import FieldSpec

    # 用最小 BillingFieldRuleSet.from_specs(...) 构造（避开 SQLite）
    rule_set = BillingFieldRuleSet.from_specs(
        [
            FieldSpec(
                name="主要诊断代码",
                section="住院诊疗",
                field_type="icd10",
                required=False,
                value_constraint="",
                description="主要诊断 ICD-10 编码",
                related_violation="",
                example_value="A01.01",
            ),
            FieldSpec(
                name="主要手术操作日期",
                section="住院诊疗",
                field_type="date",
                required=False,
                value_constraint="",
                description="主要手术操作日期",
                related_violation="",
                example_value="2026-01-15",
            ),
            FieldSpec(
                name="入院日期",
                section="基本信息",
                field_type="date",
                required=False,
                value_constraint="",
                description="入院日期",
                related_violation="",
                example_value="2026-01-12",
            ),
        ],
        enforce_count=False,
    )

    raw_text = (
        "入院日期：2026-01-12\n"
        "主要诊断代码：A99\n"  # 非 ICD-10
        "主要手术操作日期：2025-12-01\n"  # 早于入院日期 → cross_validator 失败
    )
    doc = BillingCaseDocument(case_id="case-full-pipe", raw_text=raw_text)

    cross_rules = CrossFieldRules(
        rules=[
            CrossFieldRule(
                name="手术日期 ≥ 入院日期",
                lhs_field="主要手术操作日期",
                rhs_field="入院日期",
                comparator=Comparator.GE,
                expected_value="",
                error_message="手术日期不能早于入院日期",
                suggested_action="核对手术日期与入院日期",
                severity="error",
            ),
        ]
    )

    # 最小 stub：所有字段 LLM 都失败
    stub = StubModelGateway(
        errors={
            "主要诊断代码": GatewayError("stub unavailable", audit_metadata={
                "case_id": "case-full-pipe",
                "field_name": "主要诊断代码",
                "model": "stub",
                "downstream_status": "unavailable",
                "attempts": 1,
            }),
            "主要手术操作日期": GatewayError("stub unavailable", audit_metadata={
                "case_id": "case-full-pipe",
                "field_name": "主要手术操作日期",
                "model": "stub",
                "downstream_status": "unavailable",
                "attempts": 1,
            }),
        }
    )

    pipe = BillingValidationPipeline(
        [
            SegmenterProcessor(),
            ExtractorProcessor(rule_set=rule_set, target_field_names=[
                "入院日期", "主要诊断代码", "主要手术操作日期",
            ]),
            CrossValidatorProcessor(rules=cross_rules),
            RuleCheckerProcessor(rule_set=rule_set),
            SemanticCheckerProcessor(llm_gateway=stub, audit_dir=tmp_path / "audit"),
        ]
    )

    out_doc = pipe.run(doc)

    # 5 步全部完成（含降级）
    assert out_doc.processed_steps == [
        "segmenter",
        "extractor",
        "cross_validator",
        "rule_checker",
        "semantic_checker",
    ]
    # 主要诊断代码 / 主要手术操作日期 仍是 RULE_CHECKED（降级）
    assert (
        out_doc.extracted_fields["主要诊断代码"].status
        == FieldStatus.RULE_CHECKED
    )
    assert (
        out_doc.extracted_fields["主要手术操作日期"].status
        == FieldStatus.RULE_CHECKED
    )
    # audit_log 含 semantic_check_unavailable
    assert any(
        e.action == "semantic_check_unavailable"
        for e in out_doc.audit_log
    )


# ---------------------------------------------------------------------------
# 9. append_llm_call 直接调用（不依赖 processor）
# ---------------------------------------------------------------------------


def test_append_llm_call_writes_jsonl(tmp_path: Path):
    from case_bill_validation.persistence.audit import append_llm_call

    audit_dir = tmp_path / "audit"
    append_llm_call(
        {
            "case_id": "case-009",
            "field_name": "x",
            "model": "stub",
            "downstream_status": "unavailable",
            "attempts": 1,
        },
        audit_dir=audit_dir,
    )
    p = audit_dir / "llm_calls.jsonl"
    assert p.exists()
    parsed = [json.loads(ln) for ln in p.read_text(encoding="utf-8").strip().split("\n")]
    assert len(parsed) == 1
    assert parsed[0]["case_id"] == "case-009"
    assert parsed[0]["downstream_status"] == "unavailable"


# ---------------------------------------------------------------------------
# 10. AuditLog hash 链追加 + verify_chain
# ---------------------------------------------------------------------------


def test_audit_log_hash_chain_appends_and_verifies(tmp_path: Path):
    from case_bill_validation.persistence.audit import AuditLog

    log = AuditLog.open("case-010", audit_dir=tmp_path / "audit")
    log.append({"actor": "rule_checker", "action": "rule_check", "note": "ok"})
    log.append({"actor": "semantic_checker", "action": "semantic_check", "note": "ok"})
    ok, err = log.verify_chain()
    assert ok, f"hash chain broken: {err}"
    # 链破坏后再 verify 应失败
    p = tmp_path / "audit" / "case-010.jsonl"
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    tampered = json.loads(lines[0])
    tampered["note"] = "TAMPERED"
    lines[0] = json.dumps(tampered, ensure_ascii=False, sort_keys=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok2, err2 = log.verify_chain()
    assert not ok2
    assert err2 is not None
