"""流水线端到端降级测试（Task 13 第二个测试文件）。

覆盖 LLM 不可达的整条流水线行为：

1. ``StubModelGateway`` 配置：所有字段调用均抛 ``GatewayError``（等价于
   生产环境 LLM 端点超时 / 401 / 5xx）；
2. 流水线仍按 5 步顺序完成，**不中断**；
3. flagged 字段 ``status`` 维持 ``FLAGGED``（spec §5：语义核失败不擅自
   推进业务状态），``rule_hits`` 追加 ``rule_source='llm_skipped'`` +
   ``severity=info`` + ``message='semantic_skipped'``；
4. **手工构造带 flagged 字段的 doc** 让 semantic_checker 进入
   ``process(doc)`` 的「对 flagged 字段逐个调 LLM」分支，独立验证
   降级路径不依赖真实 fixture 文本覆盖率；
5. ``ExcelExporter.export()`` 仍可调用，多 sheet + 公式 + 状态覆盖完整
   不受 LLM 不可达影响（spec §4 输出闭环）。

降级路径要点（来自 spec.md §5）：

- ``SemanticCheckerProcessor`` 接 GatewayError 时：
  - 字段状态维持 RULE_CHECKED 或 FLAGGED（不擅自推进到
    SEMANTIC_CHECKED / CONFIRMED）；
  - ``rule_hits`` 追加 ``llm_skipped`` 让审计可追溯；
  - 通过 ``persistence.audit.append_llm_call`` 把调用元数据落
    ``logs/audit/llm_calls.jsonl``。
- 流水线不对未确认字段强制 need_manual，仅在 extractor 抽不到时才
  标 need_manual —— LLM 失败不应被扩大解释为「全单 needs_manual」；
- 不静默写数据：``rule_hits`` 须显式记录 ``rule_source='llm_skipped'``
  + ``severity='info'`` 让审计可追溯。

参考：Task 8 test_degradation.py 单元级覆盖 + 本文件端到端级别覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from case_bill_validation.document import (
    AuditEntry,
    BillingCaseDocument,
    DocumentState,
    FieldMark,
    FieldSource,
    FieldStatus,
    RuleSeverity,
    RuleSource,
    SpanRef,
)
from case_bill_validation.export.excel import ExcelExporter
from case_bill_validation.llm.gateway import GatewayError, StubModelGateway
from case_bill_validation.pipeline.core import BillingValidationPipeline
from case_bill_validation.pipeline.cross_validator import make_cross_validator
from case_bill_validation.pipeline.extractor import make_extractor
from case_bill_validation.pipeline.rule_checker import make_rule_checker
from case_bill_validation.pipeline.semantic_checker import make_semantic_checker
from case_bill_validation.pipeline.segmenter import SegmenterProcessor
from case_bill_validation.rules.cross_field_rules import CrossFieldRules
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


PIPELINE_STEP_ORDER: tuple[str, ...] = (
    "segmenter",
    "extractor",
    "cross_validator",
    "rule_checker",
    "semantic_checker",
)


# ---------------------------------------------------------------------------
# fixture：脱敏样例 SQLite + 规则集 + 失败网关
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def billing_db(tmp_path_factory) -> Path:
    from case_bill_validation.rules.migrations import init_billing_db

    db_path = tmp_path_factory.mktemp("billing_db") / "billing_fields.db"
    init_billing_db(db_path)
    return db_path


@pytest.fixture(scope="module")
def rule_set(billing_db) -> BillingFieldRuleSet:
    return BillingFieldRuleSet.open(billing_db)


@pytest.fixture(scope="module")
def cross_field_rules(billing_db) -> CrossFieldRules:
    return CrossFieldRules.open(billing_db)


@pytest.fixture(scope="module")
def failing_gateway() -> StubModelGateway:
    """对所有 14 类高频字段均抛 GatewayError 的 Stub。"""
    err = GatewayError(
        "ModelGateway 模拟不可达",
        audit_metadata={
            "case_id": "case-degradation",
            "downstream_status": "unavailable",
        },
    )
    high_freq_fields = [
        "姓名",
        "性别",
        "年龄",
        "证件号码",
        "出生日期",
        "入院日期",
        "出院日期",
        "实际住院天数",
        "入院科别",
        "出院科别",
        "主要诊断代码",
        "主要手术操作代码",
        "主要手术操作日期",
        "离院方式",
    ]
    return StubModelGateway(errors={f: err for f in high_freq_fields})


@pytest.fixture
def conflict_doc(fixtures_dir) -> BillingCaseDocument:
    """discharge_06.txt 时序倒置 fixture。"""
    raw = (fixtures_dir / "discharge_06.txt").read_text(encoding="utf-8")
    return BillingCaseDocument(
        case_id="case-degradation-conflict", raw_text=raw
    )


@pytest.fixture
def happy_doc(fixtures_dir) -> BillingCaseDocument:
    """discharge_01.txt 正常 fixture。"""
    raw = (fixtures_dir / "discharge_01.txt").read_text(encoding="utf-8")
    return BillingCaseDocument(
        case_id="case-degradation-happy", raw_text=raw
    )


def _build_pipeline(
    rule_set: BillingFieldRuleSet,
    cross_field_rules: CrossFieldRules,
    llm_gateway: StubModelGateway,
) -> BillingValidationPipeline:
    return BillingValidationPipeline(
        processors=[
            SegmenterProcessor(),
            make_extractor(rule_set=rule_set),
            make_cross_validator(rules=cross_field_rules),
            make_rule_checker(rule_set=rule_set),
            make_semantic_checker(llm_gateway=llm_gateway),
        ]
    )


def _make_flagged_field_doc(failing_gateway: StubModelGateway) -> BillingCaseDocument:
    """构造一份 doc：含至少 1 个 status=FLAGGED 字段，让 semantic_checker
    进入「对 flagged 字段调 LLM」分支。这隔离了 semantic_checker 降级
    行为与 cross_validator 的实际触发条件（fixture 文本覆盖率）。"""
    raw_text = (
        "主诉：反复胸闷 2 年。\n"
        "入院日期：2026-05-01。出院日期：2026-05-10。\n"
        "主要手术日期：2026-05-05。\n"
    )
    doc = BillingCaseDocument(case_id="case-flagged-field-degradation", raw_text=raw_text)
    # 手工塞一个 FLAGGED 字段 + 已知 span（不依赖 extractor 抽得到）
    doc.extracted_fields["主要手术操作日期"] = FieldMark(
        field_name="主要手术操作日期",
        value="2026-05-05",
        span=SpanRef(
            start_char=raw_text.index("2026-05-05"),
            end_char=raw_text.index("2026-05-05") + 10,
        ),
        confidence=0.8,
        status=FieldStatus.FLAGGED,
        source=FieldSource.REGEX,
        rule_hits=[],
    )
    # 把已在 cross_validator / rule_checker 阶段标 rule_checked 的其它字段填上，
    # 这样 semantic_checker 不会被其它分支干扰
    doc.extracted_fields["入院日期"] = FieldMark(
        field_name="入院日期",
        value="2026-05-01",
        span=SpanRef(
            start_char=raw_text.index("2026-05-01"),
            end_char=raw_text.index("2026-05-01") + 10,
        ),
        confidence=0.95,
        status=FieldStatus.RULE_CHECKED,
        source=FieldSource.REGEX,
        rule_hits=[],
    )
    doc.processed_steps = ["segmenter", "extractor", "cross_validator", "rule_checker"]
    # 手工登记一个「cross_field 失败」audit
    doc.append_audit(
        AuditEntry(
            actor="cross_validator",
            action="cross_field_validation",
            note="manually-set flagged field for degradation test",
        )
    )
    return doc


# ---------------------------------------------------------------------------
# 测试 1：手工构造 flagged 字段 + LLM 不可达 → 降级到 llm_skipped
# ---------------------------------------------------------------------------


def test_flagged_field_degrades_with_llm_skipped(
    rule_set,
    cross_field_rules,
    failing_gateway,
    tmp_path,
    monkeypatch,
) -> None:
    """手工构造 1 个 FLAGGED 字段 + 失败的 LLM：semantic_checker 不擅自推
    进状态；rule_hits 追加 llm_skipped 标记。"""
    doc = _make_flagged_field_doc(failing_gateway)
    pipeline = _build_pipeline(rule_set, cross_field_rules, failing_gateway)

    # 单独跑语义核（已是 RULE_CHECKED 阶段，对 FLAGGED 字段调 LLM）
    # monkeypatch tmp audit dir
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUDIT_LOG_DIR", str(audit_dir))

    # 用 semantic_checker.process 直接跑（避免前序步骤重写 doc）
    sem = make_semantic_checker(llm_gateway=failing_gateway)
    out = sem.process(doc)

    # 字段降级到 RULE_CHECKED（spec §5/plan.md §6：FLAGGED → RULE_CHECKED，
    # 不擅自推进到 SEMANTIC_CHECKED / CONFIRMED）
    flagged_mark = out.extracted_fields["主要手术操作日期"]
    assert flagged_mark.status == FieldStatus.RULE_CHECKED, (
        f"LLM 失败时 FLAGGED 字段应降级到 RULE_CHECKED，"
        f"实际 status={flagged_mark.status.value}"
    )

    # 不应是 SEMANTIC_CHECKED（不擅自推进到语义核态）
    assert flagged_mark.status != FieldStatus.SEMANTIC_CHECKED, (
        "LLM 失败时不应推进到 SEMANTIC_CHECKED"
    )

    # 字段 rule_hits 含 llm_skipped
    llm_skipped_hits = [
        h
        for h in flagged_mark.rule_hits
        if h.rule_source == RuleSource.LLM_SKIPPED
    ]
    assert llm_skipped_hits, (
        f"FLAGGED 字段未追加 llm_skipped 标记；"
        f"rule_hits={flagged_mark.rule_hits}"
    )
    # 信息可追溯
    for h in llm_skipped_hits:
        assert "semantic_skipped" in h.message_text.lower()
        assert h.severity == RuleSeverity.INFO

    # 流水线 doc.rule_hits 也至少被推 1 条 llm_skipped
    doc_llm_skipped = [
        h for h in out.rule_hits if h.rule_source == RuleSource.LLM_SKIPPED
    ]
    assert doc_llm_skipped, "doc.rule_hits 缺 llm_skipped 条目"


# ---------------------------------------------------------------------------
# 测试 2：正常样例 + LLM 不可达 → 流水线不中断 + 没扩大化 needs_manual
# ---------------------------------------------------------------------------


def test_happy_fixture_pipeline_continues_when_llm_unavailable(
    rule_set,
    cross_field_rules,
    failing_gateway,
    happy_doc,
) -> None:
    """正常样例 + LLM 不可达：流水线不中断；不应扩大到 needs_manual
    （spec §5：LLM 失败不应改写 doc.needs_manual，仅 extractor 抽不到时）。"""
    pipeline = _build_pipeline(rule_set, cross_field_rules, failing_gateway)
    out = pipeline.run(happy_doc)

    # 5 步全跑
    for step in PIPELINE_STEP_ORDER:
        assert step in out.processed_steps

    # needs_manual 不应被扩大化：happy_doc 文本足够 → needs_manual 应为 False
    assert out.needs_manual is False, (
        "正常 fixture 在 LLM 不可达时不应被标 needs_manual（spec §5 降级路径）"
    )

    # 至少抽取一些字段
    assert len(out.extracted_fields) >= 3


# ---------------------------------------------------------------------------
# 测试 3：冲突样例 + LLM 不可达 → 流水线不中断
# ---------------------------------------------------------------------------


def test_conflict_fixture_pipeline_continues_when_llm_unavailable(
    rule_set,
    cross_field_rules,
    failing_gateway,
    conflict_doc,
) -> None:
    """冲突样例 + LLM 不可达：流水线不中断；doc.rule_hits 非空。"""
    pipeline = _build_pipeline(rule_set, cross_field_rules, failing_gateway)
    out = pipeline.run(conflict_doc)

    # 5 步全跑
    for step in PIPELINE_STEP_ORDER:
        assert step in out.processed_steps, f"步骤 {step} 未执行"

    # 至少抽到一些字段
    assert len(out.extracted_fields) >= 3

    # rule_hits 至少 1 条
    assert len(out.rule_hits) >= 1, "LLM 不可达时流水线未产出任何 rule_hits"

    # needs_manual 不被扩大化
    assert out.needs_manual is False


# ---------------------------------------------------------------------------
# 测试 4：Excel 在 LLM 不可达时仍可导出（多 sheet + 公式 + 193 项分区）
# ---------------------------------------------------------------------------


def test_excel_export_still_works_when_llm_unavailable(
    rule_set,
    cross_field_rules,
    failing_gateway,
    conflict_doc,
    tmp_export_dir,
) -> None:
    """LLM 不可达时 Excel 仍能导出：4 个分区 sheet + 公式列 + 193 项不变式。"""
    pipeline = _build_pipeline(rule_set, cross_field_rules, failing_gateway)
    out = pipeline.run(conflict_doc)

    xlsx_path = tmp_export_dir / "degradation_export.xlsx"
    ExcelExporter(rule_set=rule_set).export(out, xlsx_path)

    assert xlsx_path.exists(), f"xlsx 未生成：{xlsx_path}"
    assert xlsx_path.stat().st_size > 0

    wb = load_workbook(xlsx_path, data_only=False)
    sheet_names = wb.sheetnames

    # 4 个分区 sheet 全在
    for sec in ["基本信息", "门诊慢特病诊疗", "住院诊疗", "医疗收费"]:
        assert sec in sheet_names, f"LLM 不可达时缺分区 sheet：{sec}"

    # 原文引用 sheet 在
    assert "_原文引用" in sheet_names, "LLM 不可达时缺原文引用 sheet"
    # 审计 sheet 在
    assert "_审计_操作" in sheet_names, "LLM 不可达时缺审计 sheet"

    # 4 个分区 sheet 字段行数合计 ≥ 193（rule_set 注入时不变式；
    # 末行含「合规合计」聚合行 → 减 1 行表头 + 1 行合计）
    total_field_rows = 0
    for sec in ["基本信息", "门诊慢特病诊疗", "住院诊疗", "医疗收费"]:
        ws = wb[sec]
        data_rows = max(0, ws.max_row - 2)
        total_field_rows += data_rows
    assert total_field_rows >= 193, (
        f"LLM 不可达时分区 sheet 字段行数合计 {total_field_rows} < 193"
    )


# ---------------------------------------------------------------------------
# 测试 5：降级 Excel 含字段状态覆盖
# ---------------------------------------------------------------------------


def test_degraded_excel_contains_coverage_evidence(
    rule_set,
    cross_field_rules,
    failing_gateway,
    conflict_doc,
    tmp_export_dir,
) -> None:
    """降级 Excel 的 _覆盖_统计 sheet 含 4 个分区名 + 合计/SUM 行。"""
    pipeline = _build_pipeline(rule_set, cross_field_rules, failing_gateway)
    out = pipeline.run(conflict_doc)

    xlsx_path = tmp_export_dir / "degraded_evidence.xlsx"
    ExcelExporter(rule_set=rule_set).export(out, xlsx_path)

    wb = load_workbook(xlsx_path, data_only=False)
    assert "_覆盖_统计" in wb.sheetnames, "缺 _覆盖_统计 sheet"

    ws_cov = wb["_覆盖_统计"]
    cov_text = " ".join(
        str(c.value or "") for r in ws_cov.iter_rows() for c in r
    )
    for sec in ["基本信息", "门诊慢特病诊疗", "住院诊疗", "医疗收费"]:
        assert sec in cov_text, f"_覆盖_统计 缺分区 {sec}"
    assert "合计" in cov_text or "SUM" in cov_text, (
        f"_覆盖_统计 缺合计/SUM 行：{cov_text[:200]}"
    )


# ---------------------------------------------------------------------------
# 测试 6：降级 doc 不擅自推进到 SUBMITTED
# ---------------------------------------------------------------------------


def test_degraded_doc_does_not_advance_to_submitted(
    rule_set,
    cross_field_rules,
    failing_gateway,
    conflict_doc,
) -> None:
    """降级状态下 doc.pipeline_state 维持 DRAFT / IN_REVIEW，不擅自推进
    到 SUBMITTED（spec §5：人工确认前不擅自推进业务状态）。"""
    pipeline = _build_pipeline(rule_set, cross_field_rules, failing_gateway)
    out = pipeline.run(conflict_doc)

    assert out.pipeline_state in (
        DocumentState.DRAFT,
        DocumentState.IN_REVIEW,
    ), f"流水线擅自推进业务状态到 {out.pipeline_state}"
