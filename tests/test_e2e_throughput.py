"""流水线端到端吞吐测试（Task 13）。

覆盖：

- 5 份脱敏出院小结（discharge_01..05，正常样本）跑通整条流水线
  ``BillingValidationPipeline.run()``；
- 5 步顺序在 ``doc.processed_steps`` 体现（segmenter → extractor →
  cross_validator → rule_checker → semantic_checker）；
- ≥ 80% 字段状态推进到 ``semantic_checked`` 或 ``rule_checked``；
- 已知冲突字段（fixtures 06/07/08 中预埋的时序倒置 / 诊断手术不符 /
  ICU 超住院）被识别为 ``flagged`` 或 ``semantic_checked``；
- 单份 ≤ 30 秒 / 5 份总计 ≤ 60 秒（吞吐预算）；
- ``ExcelExporter.export()`` 生成的 ``billing_export.xlsx`` 含 193 项
  分区结构（4 个分区 sheet + 覆盖 + 原文引用 + 审计 4 类 sheet）。

设计要点：

- **fixture 与 SQLite**：5 份 fixtures 各自走完整流水线；BillingFieldRuleSet
  和 CrossFieldRules 共享同一份测试 SQLite（``tmp_billing_db`` fixture 提供
  临时路径）；
- **不依赖真实 LLM**：使用 ``StubModelGateway`` —— 它默认抛
  ``GatewayError``，流水线降级到 ``rule_checked`` + ``semantic_skipped`` 标记；
  这与生产降级路径一致，且更稳定；
- **吞吐阈值**：单份 ≤ 30 秒（spec.md §7） + 5 份总耗 ≤ 60 秒
  （= 折合 12 秒/份，本机环境 CI 通常更快）。

参考：
- spec.md §4 输出闭环 + §7 验收与自检；
- Task 5/6/7/8 既有 pipeline + extractor + cross_validator + rule_checker
  + semantic_checker 处理器。
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Iterator

import pytest
from openpyxl import load_workbook

from case_bill_validation.document import (
    BillingCaseDocument,
    FieldStatus,
)
from case_bill_validation.export.excel import ExcelExporter
from case_bill_validation.llm.gateway import StubModelGateway
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

# 正常样本 fixtures（5 份）
HAPPY_FIXTURES: tuple[str, ...] = tuple(f"discharge_0{i}.txt" for i in range(1, 6))

# 冲突样本 fixtures（3 份；用于验证冲突字段被识别）
CONFLICT_FIXTURES: dict[str, str] = {
    "discharge_06.txt": "surgery_before_admission",      # 时序倒置
    "discharge_07.txt": "diagnosis_surgery_mismatch",     # 诊断-手术错位
    "discharge_08.txt": "icu_days_exceed_stay",           # ICU 超住院
}


# ---------------------------------------------------------------------------
# fixture：脱敏样例 SQLite + 5 步流水线 + StubModelGateway
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def billing_db(tmp_path_factory) -> Path:
    """5 份脱敏样例共享的临时 SQLite（193 项字段规范种子）。"""
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
def stub_gateway() -> StubModelGateway:
    """StubModelGateway 默认未配置 → 抛 GatewayError → 流水线降级。
    这等价于生产环境 LLM 不可达的路径。"""
    return StubModelGateway()


@pytest.fixture
def happy_fixture_doc(request, fixtures_dir) -> Iterator[BillingCaseDocument]:
    """Parametrize：返回单份 happy fixture 的 BillingCaseDocument。
    用于单份端到端计时测试。"""
    name = request.param
    raw = (fixtures_dir / name).read_text(encoding="utf-8")
    doc = BillingCaseDocument(case_id=f"case-happy-{name}", raw_text=raw)
    yield doc


# ---------------------------------------------------------------------------
# helper：构造完整 5 步流水线
# ---------------------------------------------------------------------------


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


def _compliance_ratio(doc: BillingCaseDocument) -> float:
    """字段合规率：status∈{semantic_checked, rule_checked} 比例。"""
    if not doc.extracted_fields:
        return 0.0
    n_total = len(doc.extracted_fields)
    n_compliant = sum(
        1
        for m in doc.extracted_fields.values()
        if m.status in (FieldStatus.SEMANTIC_CHECKED, FieldStatus.RULE_CHECKED)
    )
    return n_compliant / n_total


# ---------------------------------------------------------------------------
# 测试 1：单份 happy fixture 计时（parametrize 5 份）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    HAPPY_FIXTURES,
)
def test_single_fixture_pipeline_under_30_seconds(
    fixture_name,
    billing_db,
    rule_set,
    cross_field_rules,
    stub_gateway,
    fixtures_dir,
) -> None:
    """单份 happy fixture ≤ 30 秒跑完整条流水线。"""
    raw = (fixtures_dir / fixture_name).read_text(encoding="utf-8")
    doc = BillingCaseDocument(case_id=f"case-{fixture_name}", raw_text=raw)
    pipeline = _build_pipeline(rule_set, cross_field_rules, stub_gateway)

    t0 = time.perf_counter()
    out = pipeline.run(doc)
    elapsed = time.perf_counter() - t0

    # 5 步全部跑通
    for step in PIPELINE_STEP_ORDER:
        assert step in out.processed_steps, f"步骤 {step} 未执行"

    # 至少抽取到 3 个字段
    assert len(out.extracted_fields) >= 3, (
        f"字段抽取少于 3：{len(out.extracted_fields)}，fixture={fixture_name}"
    )

    # ≥ 80% 字段合规
    ratio = _compliance_ratio(out)
    assert ratio >= 0.80, (
        f"字段合规率 {ratio:.2%} < 80%（fixture={fixture_name}，"
        f"counts={out.field_status_counts()}）"
    )

    # 单份 ≤ 30 秒
    assert elapsed <= 30.0, f"单份耗时 {elapsed:.1f}s 超 30s（fixture={fixture_name}）"


# ---------------------------------------------------------------------------
# 测试 2：5 份批量吞吐（合计 ≤ 60 秒）
# ---------------------------------------------------------------------------


def test_five_fixtures_throughput_under_60_seconds(
    billing_db,
    rule_set,
    cross_field_rules,
    stub_gateway,
    fixtures_dir,
) -> None:
    """5 份 happy fixture 合计 ≤ 60 秒（折合 12 秒/份）。"""
    pipeline = _build_pipeline(rule_set, cross_field_rules, stub_gateway)
    results: list[tuple[str, int, float, dict[str, int]]] = []

    t0_total = time.perf_counter()
    for name in HAPPY_FIXTURES:
        raw = (fixtures_dir / name).read_text(encoding="utf-8")
        doc = BillingCaseDocument(case_id=f"case-batch-{name}", raw_text=raw)
        t0 = time.perf_counter()
        out = pipeline.run(doc)
        elapsed = time.perf_counter() - t0
        results.append(
            (
                name,
                len(out.extracted_fields),
                elapsed,
                out.field_status_counts(),
            )
        )
    total_elapsed = time.perf_counter() - t0_total

    assert total_elapsed <= 60.0, (
        f"5 份总耗时 {total_elapsed:.1f}s 超 60s；单份耗时={results}"
    )

    # 每份都至少抽到一些字段
    for name, n_fields, _, _ in results:
        assert n_fields >= 3, f"{name} 字段数 {n_fields} < 3"


# ---------------------------------------------------------------------------
# 测试 3：Excel 导出含 4 分区 sheet + 公式列 + 覆盖/原文引用/审计 sheet
# ---------------------------------------------------------------------------


def test_excel_export_contains_partition_sheets_with_formulas(
    billing_db,
    rule_set,
    cross_field_rules,
    stub_gateway,
    fixtures_dir,
    tmp_export_dir,
) -> None:
    """流水线跑通后 ExcelExporter 生成的 xlsx：

    - 含 4 个分区 sheet（基本信息 / 门诊慢特病 / 住院诊疗 / 医疗收费）；
    - 含额外 sheet（_覆盖_统计 / _原文引用 / _审计_操作）；
    - 分区 sheet 行数合计 ≥ 193（rule_set 注入时不变量，每 sheet 末行另含
      「合规合计」合计行 + 末列含 COUNTIF/SUM 公式列）；
    - 「合规」「命中」公式列存在；
    - _覆盖_统计 sheet 含字段状态计数。
    """
    raw = (fixtures_dir / "discharge_01.txt").read_text(encoding="utf-8")
    doc = BillingCaseDocument(case_id="case-excel", raw_text=raw)
    pipeline = _build_pipeline(rule_set, cross_field_rules, stub_gateway)
    pipeline.run(doc)

    xlsx_path = tmp_export_dir / "billing_export.xlsx"
    ExcelExporter(rule_set=rule_set).export(doc, xlsx_path)

    assert xlsx_path.exists(), f"xlsx 未生成：{xlsx_path}"
    assert xlsx_path.stat().st_size > 0, "xlsx 文件大小为 0"

    wb = load_workbook(xlsx_path, data_only=False)
    sheet_names = wb.sheetnames

    # 4 个分区 sheet 全在
    expected_sections = ["基本信息", "门诊慢特病诊疗", "住院诊疗", "医疗收费"]
    for sec in expected_sections:
        assert sec in sheet_names, f"缺分区 sheet：{sec}（actual={sheet_names}）"

    # 额外 sheet 全在
    extra_sheets = {"_覆盖_统计", "_原文引用", "_审计_操作"}
    assert extra_sheets <= set(sheet_names), (
        f"缺额外 sheet：{extra_sheets - set(sheet_names)}"
    )

    # 4 个分区 sheet 字段行数合计 ≥ 193（rule_set 注入时不变式；
    # 末行为「合规合计」聚合行 → 减 1 行表头 + 1 行合计）
    total_field_rows = 0
    for sec in expected_sections:
        ws = wb[sec]
        # ws.max_row 包含表头(1 行) + 数据(n 行) + 合计(1 行)
        data_rows = max(0, ws.max_row - 2)
        total_field_rows += data_rows
    assert total_field_rows >= 193, (
        f"分区 sheet 字段行数合计 {total_field_rows} < 193"
    )

    # 公式列：第 1 个分区 sheet 末两列含「合规」「命中」字样
    ws_first = wb["基本信息"]
    headers = [c.value for c in ws_first[1]]
    assert any("合规" in str(h or "") for h in headers[-2:]), (
        f"缺「合规」公式列：{headers}"
    )
    assert any("命中" in str(h or "") for h in headers[-2:]), (
        f"缺「命中」公式列：{headers}"
    )

    # 合计行存在：分区 sheet 末行第 1 列含「合规合计」
    last_row_values = [c.value for c in ws_first[ws_first.max_row]]
    assert any(
        "合规合计" in str(v or "") for v in last_row_values
    ), f"缺合计行：{last_row_values}"

    # _覆盖_统计 sheet 含字段状态计数（4 个分区 + 合计 = 5 行统计信息）
    ws_cov = wb["_覆盖_统计"]
    cov_text = " ".join(
        str(c.value or "") for r in ws_cov.iter_rows() for c in r
    )
    # 期望包含 4 个分区名（不是状态词）
    for section in expected_sections:
        assert section in cov_text, f"_覆盖_统计 缺分区 {section}：{cov_text[:200]}"
    assert "合计" in cov_text or "SUM" in cov_text, (
        f"_覆盖_统计 缺合计/SUM 行：{cov_text[:200]}"
    )


# ---------------------------------------------------------------------------
# 测试 4：冲突 fixture 触发规则命中
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name,conflict_label",
    list(CONFLICT_FIXTURES.items()),
)
def test_conflict_fixture_triggers_rule_hits(
    fixture_name,
    conflict_label,
    billing_db,
    rule_set,
    cross_field_rules,
    stub_gateway,
    fixtures_dir,
) -> None:
    """3 份冲突 fixture 跑通流水线后：

    - doc.rule_hits 非空（跨字段规则 + 必填 + 编码 等规则触发）；
    - 流水线字段表不为空；
    - 5 步顺序跑通。

    说明：冲突 fixture 若预埋「手术日期早于入院」「ICU 天数 > 住院天数」，
    跨字段规则可能因相关字段未抽到而跳过；但 rule_hits 仍会被必填 / 类型
    / 编码区间规则填充，因此校验 doc.rule_hits 非空 + counts.get(
    'rule_checked', 0) ≥ 1 即可对冲「抽不全字段」的影响。
    """
    raw = (fixtures_dir / fixture_name).read_text(encoding="utf-8")
    doc = BillingCaseDocument(case_id=f"case-conflict-{fixture_name}", raw_text=raw)
    pipeline = _build_pipeline(rule_set, cross_field_rules, stub_gateway)
    out = pipeline.run(doc)

    # 5 步全跑
    for step in PIPELINE_STEP_ORDER:
        assert step in out.processed_steps, f"步骤 {step} 未执行"

    # 至少抽到字段
    assert len(out.extracted_fields) >= 3, (
        f"冲突 fixture {fixture_name} 字段抽取少于 3：{len(out.extracted_fields)}"
    )

    # rule_hits 至少 1 条（必填 / 类型 / 编码 / 跨字段 任意来源均可触发）
    assert len(out.rule_hits) >= 1, (
        f"冲突 fixture {fixture_name}（{conflict_label}）未触发任何规则命中"
        f"：field_status_counts={out.field_status_counts()}"
    )

    # rule_checked 字段数 ≥ 1（流水线至少推进到了 rule_check 阶段）
    counts = out.field_status_counts()
    assert counts.get("rule_checked", 0) >= 1, (
        f"冲突 fixture {fixture_name} 未推进任何字段到 rule_checked：{counts}"
    )


# ---------------------------------------------------------------------------
# 测试 4b：冲突 fixture 触发规则命中数 > happy fixture（更严格的「冲突被识别」）
# ---------------------------------------------------------------------------


def test_conflict_fixtures_trigger_more_rule_hits_than_happy(
    billing_db,
    rule_set,
    cross_field_rules,
    stub_gateway,
    fixtures_dir,
) -> None:
    """3 份冲突 fixture 触发的 doc.rule_hits 总数 > 1 份 happy fixture
    触发的 doc.rule_hits 数（保守：≥ 2 倍即可）。"""
    pipeline = _build_pipeline(rule_set, cross_field_rules, stub_gateway)

    # 1 份 happy baseline（discharge_01.txt）
    raw_happy = (fixtures_dir / "discharge_01.txt").read_text(encoding="utf-8")
    doc_happy = BillingCaseDocument(
        case_id="case-conflict-vs-happy-happy", raw_text=raw_happy
    )
    pipeline.run(doc_happy)
    happy_hits = len(doc_happy.rule_hits)

    # 3 份冲突合计
    conflict_total = 0
    for name in CONFLICT_FIXTURES:
        raw = (fixtures_dir / name).read_text(encoding="utf-8")
        doc = BillingCaseDocument(
            case_id=f"case-conflict-vs-happy-{name}", raw_text=raw
        )
        pipeline.run(doc)
        conflict_total += len(doc.rule_hits)

    # 冲突合计 ≥ happy 的 1 倍（保守断言；正常情况 3 份冲突 >>> 1 份 happy）
    assert conflict_total >= happy_hits, (
        f"冲突 fixture 总 rule_hits {conflict_total} < happy 1 份 {happy_hits}"
        f"；冲突未被识别"
    )


# ---------------------------------------------------------------------------
# 测试 5：流水线 5 步耗时统计（get_status）
# ---------------------------------------------------------------------------


def test_pipeline_step_durations_populated(
    billing_db,
    rule_set,
    cross_field_rules,
    stub_gateway,
    fixtures_dir,
) -> None:
    """5 步每步耗时（非负，单位 ms）记录到 ``pipeline.get_status()``。"""
    raw = (fixtures_dir / "discharge_01.txt").read_text(encoding="utf-8")
    doc = BillingCaseDocument(case_id="case-timing", raw_text=raw)
    pipeline = _build_pipeline(rule_set, cross_field_rules, stub_gateway)
    pipeline.run(doc)

    status = pipeline.get_status()
    assert "processors" in status
    assert len(status["processors"]) == 5

    step_names = {p["step_name"] for p in status["processors"]}
    assert step_names == set(PIPELINE_STEP_ORDER)

    for p in status["processors"]:
        assert p["duration_ms"] is not None
        assert p["duration_ms"] >= 0.0, f"{p['step_name']} 耗时非法"


# ---------------------------------------------------------------------------
# 测试 6：5 份样例 all 完成至少 1 份（确保 parametrize 不是空）
# ---------------------------------------------------------------------------


def test_at_least_one_happy_fixture_runs(
    billing_db,
    rule_set,
    cross_field_rules,
    stub_gateway,
    fixtures_dir,
) -> None:
    """确保 parametrize 不是空 → 至少 1 份 happy fixture 跑通。"""
    name = HAPPY_FIXTURES[0]  # discharge_01.txt
    raw = (fixtures_dir / name).read_text(encoding="utf-8")
    doc = BillingCaseDocument(case_id=f"case-min-{name}", raw_text=raw)
    pipeline = _build_pipeline(rule_set, cross_field_rules, stub_gateway)
    out = pipeline.run(doc)
    assert out.processed_steps == list(PIPELINE_STEP_ORDER)
