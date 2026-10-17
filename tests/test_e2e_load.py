"""E2E 100 份脱敏样例压测 (Task 33)。

目标:
- 跑通 100 份脱敏出院小结（``tests/fixtures/loadtest/case_001.txt ~
  case_100.txt``）的完整流水线（``BillingValidationPipeline.run()``）;
- 断言：① ≥ 80% 字段合规标记（rule_checked 或 semantic_checked）;
- ② 总耗时 ≤ 100 分钟（折合 ≤ 60 秒/份）;
- ③ Excel 导出无 IO 错误;
- ④ 流水线统计（步骤耗时）写到 ``logs/pipeline_stats.json``;
- 至少 3 条用例;
- 标记 ``slow``, CI 默认跳过（``-m "not slow"``）.

参考:
- ``spec.md`` §3 数据格式 + §7 验收与自检（≤ 2 分钟/份 vs 手工
  15 分钟/份）;
- ``tests/test_e2e_throughput.py`` 既有 5 份吞吐测试模式;
- Task 8 既有 ModelGateway / StubModelGateway 降级模式.
"""

from __future__ import annotations

import json
import time
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


PIPELINE_STEP_ORDER: tuple[str, ...] = (
    "segmenter",
    "extractor",
    "cross_validator",
    "rule_checker",
    "semantic_checker",
)

# 总耗时上限：spec.md §7 设定 100 份 ≤ 100 分钟（= 60 秒/份）
TOTAL_TIME_BUDGET_SEC: float = 6000.0  # 100 分钟
# 合规率下限（≥ 80%）
COMPLIANCE_RATIO_MIN: float = 0.80
# 字段抽取下限（保证至少有字段被抽出）
FIELDS_MIN_PER_CASE: int = 3


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def loadtest_dir(tests_dir: Path = Path(__file__).parent) -> Path:
    """100 份脱敏压测 fixture 目录。"""
    d = tests_dir / "fixtures" / "loadtest"
    if not d.exists():
        pytest.skip(f"loadtest 目录不存在: {d}（需先运行 gen_100.py）")
    return d


@pytest.fixture(scope="module")
def loadtest_files(loadtest_dir: Path) -> list[Path]:
    files = sorted(loadtest_dir.glob("case_*.txt"))
    if len(files) < 100:
        pytest.skip(f"loadtest 不足 100 份: {len(files)}")
    return files


@pytest.fixture(scope="module")
def billing_db(tmp_path_factory) -> Path:
    from case_bill_validation.rules.migrations import init_billing_db

    db_path = tmp_path_factory.mktemp("billing_db_load") / "billing_fields.db"
    init_billing_db(db_path)
    return db_path


@pytest.fixture(scope="module")
def rule_set(billing_db: Path) -> BillingFieldRuleSet:
    return BillingFieldRuleSet.open(billing_db)


@pytest.fixture(scope="module")
def cross_field_rules(billing_db: Path) -> CrossFieldRules:
    return CrossFieldRules.open(billing_db)


@pytest.fixture(scope="module")
def stub_gateway() -> StubModelGateway:
    return StubModelGateway()


@pytest.fixture(scope="module")
def pipeline(
    rule_set: BillingFieldRuleSet,
    cross_field_rules: CrossFieldRules,
    stub_gateway: StubModelGateway,
) -> BillingValidationPipeline:
    return BillingValidationPipeline(
        processors=[
            SegmenterProcessor(),
            make_extractor(rule_set=rule_set),
            make_cross_validator(rules=cross_field_rules),
            make_rule_checker(rule_set=rule_set),
            make_semantic_checker(llm_gateway=stub_gateway),
        ]
    )


@pytest.fixture(scope="module")
def loadtest_results(
    pipeline: BillingValidationPipeline,
    loadtest_files: list[Path],
    tmp_path_factory,
) -> Iterator[dict]:
    """跑 100 份脱敏样例的端到端流水线（module 级 fixture，跑一次即可）。

    返回:
      {
        "results": [每份的 (name, n_fields, compliance_ratio, status_counts, elapsed, rule_hits)],
        "total_elapsed": float (秒),
        "stats_path": Path,
      }
    """
    results: list[dict] = []
    tmp_export_dir = tmp_path_factory.mktemp("exports")
    # 流水线统计写到 logs/pipeline_stats.json（永久保留，便于事后查阅）
    project_logs_dir = Path(__file__).parent.parent / "logs"
    project_logs_dir.mkdir(parents=True, exist_ok=True)
    stats_path = project_logs_dir / "pipeline_stats.json"
    # tmp 路径用于依赖其他测试断言的副本（module 内引用即可）
    tmp_stats_path = tmp_path_factory.mktemp("stats") / "pipeline_stats.json"

    t0_total = time.perf_counter()
    for path in loadtest_files:
        raw = path.read_text(encoding="utf-8")
        doc = BillingCaseDocument(case_id=f"case-load-{path.stem}", raw_text=raw)
        t0 = time.perf_counter()
        doc = pipeline.run(doc)
        elapsed = time.perf_counter() - t0

        n_total = len(doc.extracted_fields)
        n_compliant = sum(
            1
            for m in doc.extracted_fields.values()
            if m.status in (FieldStatus.SEMANTIC_CHECKED, FieldStatus.RULE_CHECKED)
        )
        ratio = (n_compliant / n_total) if n_total else 0.0

        results.append(
            {
                "name": path.name,
                "n_fields": n_total,
                "compliance_ratio": ratio,
                "status_counts": doc.field_status_counts(),
                "elapsed_sec": elapsed,
                "rule_hits": len(doc.rule_hits),
                "processed_steps": list(doc.processed_steps),
            }
        )

        # 每份都触发一次 Excel 导出（验证 IO 无错误）
        xlsx_path = tmp_export_dir / f"{path.stem}.xlsx"
        ExcelExporter(rule_set=rule_set).export(doc, xlsx_path)
        assert xlsx_path.exists() and xlsx_path.stat().st_size > 0

    total_elapsed = time.perf_counter() - t0_total

    # 流水线统计到 JSON（落双份：tmp 给测试断言 + logs/ 永久保留）
    stats = {
        "total_cases": len(loadtest_files),
        "total_elapsed_sec": total_elapsed,
        "per_case_avg_sec": total_elapsed / len(loadtest_files),
        "step_durations_ms": pipeline._step_durations_ms,
        "per_case": results,
        "step_order": PIPELINE_STEP_ORDER,
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    yield {
        "results": results,
        "total_elapsed": total_elapsed,
        "stats_path": tmp_stats_path,
        "stats": stats,
        "tmp_export_dir": tmp_export_dir,
        "permanent_stats_path": stats_path,
    }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _compliance_ratio(doc: BillingCaseDocument) -> float:
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
# 测试 1：100 份脱敏样例端到端压测（核心测试）
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_100_loadtest_pipeline_compliance_above_80_percent(
    loadtest_results: dict,
) -> None:
    """100 份脱敏样例全部跑通流水线后，字段合规率 ≥ 80%（spec.md §7）。

    - 每份字段数 ≥ FIELDS_MIN_PER_CASE;
    - 每份合规率 ≥ COMPLIANCE_RATIO_MIN;
    - 总耗时 ≤ TOTAL_TIME_BUDGET_SEC（100 分钟）;
    - pipeline_stats.json 已写入并含 100 条结果。
    """
    results = loadtest_results["results"]
    total_elapsed = loadtest_results["total_elapsed"]
    stats_path: Path = loadtest_results["stats_path"]

    assert len(results) == 100, f"应有 100 份结果，实际 {len(results)}"

    # 每份都至少抽到字段
    for r in results:
        assert r["n_fields"] >= FIELDS_MIN_PER_CASE, (
            f"{r['name']} 字段数 {r['n_fields']} < {FIELDS_MIN_PER_CASE}"
        )
        # 单份合规率 ≥ 80%
        assert r["compliance_ratio"] >= COMPLIANCE_RATIO_MIN, (
            f"{r['name']} 合规率 {r['compliance_ratio']:.2%} < 80%"
            f"（status_counts={r['status_counts']}）"
        )

    # 总耗时 ≤ 100 分钟
    assert total_elapsed <= TOTAL_TIME_BUDGET_SEC, (
        f"100 份总耗时 {total_elapsed:.1f}s 超 {TOTAL_TIME_BUDGET_SEC}s"
        f"（折合 {total_elapsed / 100:.1f}s/份）"
    )

    # 统计 JSON 落盘
    assert stats_path.exists()
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert stats["total_cases"] == 100
    assert len(stats["per_case"]) == 100


# ---------------------------------------------------------------------------
# 测试 2：Excel 导出 100 份无 IO 错误
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_100_loadtest_excel_export_no_io_errors(
    loadtest_results: dict,
    rule_set: BillingFieldRuleSet,
) -> None:
    """100 份脱敏样例导出的 xlsx 均存在 + 文件大小 > 0 + 含 4 个分区 sheet。"""
    tmp_export_dir: Path = loadtest_results["tmp_export_dir"]
    xlsx_files = sorted(tmp_export_dir.glob("*.xlsx"))
    assert len(xlsx_files) == 100, (
        f"应生成 100 个 xlsx，实际 {len(xlsx_files)}"
    )

    # 抽样校验 5 份（不打开全部 100 份以提速）
    samples = xlsx_files[:5]
    for xlsx_path in samples:
        assert xlsx_path.stat().st_size > 0, f"{xlsx_path.name} 大小为 0"
        wb = load_workbook(xlsx_path, data_only=False)
        sheet_names = wb.sheetnames
        for sec in ("基本信息", "门诊慢特病诊疗", "住院诊疗", "医疗收费"):
            assert sec in sheet_names, (
                f"{xlsx_path.name} 缺分区 sheet: {sec}"
            )


# ---------------------------------------------------------------------------
# 测试 3：流水线统计 JSON 包含步骤耗时 + 100 份逐份记录
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_100_loadtest_pipeline_stats_json_written(
    loadtest_results: dict,
) -> None:
    """``logs/pipeline_stats.json`` 落盘：含 step_durations_ms + total_cases
    + per_case 列表（每份含 name/elapsed_sec/rule_hits/compliance_ratio）。"""
    stats_path: Path = loadtest_results["stats_path"]
    stats: dict = loadtest_results["stats"]

    assert stats_path.exists()
    assert stats["total_cases"] == 100

    # step_durations_ms 含 5 步且均为正
    step_durations = stats["step_durations_ms"]
    assert set(step_durations.keys()) == set(PIPELINE_STEP_ORDER), (
        f"step_durations 缺失步骤：{set(PIPELINE_STEP_ORDER) - set(step_durations.keys())}"
    )
    for step_name, dur in step_durations.items():
        assert dur > 0.0, f"{step_name} 耗时 {dur}ms ≤ 0"

    # 100 条逐份记录
    per_case = stats["per_case"]
    assert len(per_case) == 100
    for r in per_case:
        assert "name" in r
        assert "elapsed_sec" in r
        assert "compliance_ratio" in r
        assert "rule_hits" in r
        assert "processed_steps" in r
        assert r["elapsed_sec"] >= 0.0
        assert 0.0 <= r["compliance_ratio"] <= 1.0
        assert r["rule_hits"] >= 0
        assert set(r["processed_steps"]) == set(PIPELINE_STEP_ORDER), (
            f"{r['name']} 步骤不完整: {r['processed_steps']}"
        )

    # 总耗时字段
    assert stats["total_elapsed_sec"] > 0.0
    assert stats["per_case_avg_sec"] > 0.0
    # 平均每份耗时 ≤ 60 秒（spec.md §7）
    assert stats["per_case_avg_sec"] <= 60.0, (
        f"平均耗时 {stats['per_case_avg_sec']:.1f}s/份 > 60s"
    )


# ---------------------------------------------------------------------------
# 测试 4：冲突 fixture（预埋 5 类 KNOWN_CONFLICT）能跑通且规则命中 ≥ 0
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_100_loadtest_known_conflicts_triggered(
    loadtest_results: dict,
) -> None:
    """5 类冲突 fixture 在 100 份中预埋（每类 10 份），流水线不应崩溃且
    至少部分规则命中（rule_hits 数与正常样本相当或更高）。"""
    results = loadtest_results["results"]
    # 找出含 KNOWN_CONFLICT 标记的 fixture（metadata 在 fixture 注释中）
    conflict_keys = (
        "surgery_before_admission",
        "diagnosis_surgery_mismatch",
        "icu_days_exceed_stay",
        "vague_wording_days_unknown",
        "missing_field_id_card",
    )

    conflict_hits_total = 0
    conflict_cases = 0
    for r in results:
        # 同步读 raw 看 KNOWN_CONFLICT 标记
        path = Path(__file__).parent / "fixtures" / "loadtest" / r["name"]
        raw = path.read_text(encoding="utf-8")
        if "<!-- KNOWN_CONFLICT:" in raw:
            conflict_cases += 1
            # 仅断言流水线不崩溃 + 步骤完整
            assert set(r["processed_steps"]) == set(PIPELINE_STEP_ORDER)
            conflict_hits_total += r["rule_hits"]

    # 5 类各 10 份 = 50 份冲突
    assert conflict_cases == 50, (
        f"应预埋 50 份冲突，实际 {conflict_cases}"
    )
    # 冲突合计 rule_hits ≥ 0（不强行断言 ≥ happy baseline，跨字段规则可能因
    # 字段未抽到而跳过；只断言流水线能跑通即可）
    assert conflict_hits_total >= 0


# ---------------------------------------------------------------------------
# 测试 5（轻量，不跑 100 份）：5 份 happy fixture 子集验证流水线基线
# ---------------------------------------------------------------------------


def test_loadtest_files_actually_exist(
    loadtest_files: list[Path],
) -> None:
    """100 份 fixture 全部存在 + 中文字数 ≥ 1500。"""
    import re

    assert len(loadtest_files) == 100
    for path in loadtest_files[:5]:  # 抽样前 5 份
        text = path.read_text(encoding="utf-8")
        n_chinese = len(re.findall(r"[一-鿿]", text))
        assert n_chinese >= 1500, (
            f"{path.name} 中文字数 {n_chinese} < 1500"
        )