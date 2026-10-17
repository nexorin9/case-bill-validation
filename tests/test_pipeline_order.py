"""Pipeline 编排器（Task 5）端到端测试。

覆盖：
1. 5 个 processor 顺序执行（segmenter → extractor → cross_validator →
   rule_checker → semantic_checker），``doc.processed_steps`` 含全部；
2. 前一步输出在下一步输入中存在（REQUIRES 满足）；
3. processor 抛错时按 ``degradation_policy`` 降级而不中断流水线；
4. 依赖图无环 → 成功构造；
5. 错误配置检测：环 / 缺依赖 / PROVIDES 冲突 / 步骤名重复。

参考：
- 任务描述：M3 主路径核心，BillingValidationPipeline（保留 Stanza Pipeline
  REQUIRES/PROVIDES 依赖图模式）；
- github_ref/stanza/stanza/pipeline/core.py:176-513 Pipeline 顺序累加模式。
"""

from __future__ import annotations

import pytest

from case_bill_validation.document import AuditEntry, BillingCaseDocument
from case_bill_validation.pipeline.core import (
    BillingValidationPipeline,
    PipelineConfigurationError,
    Processor,
)


class _StubProcessor(Processor):
    """测试用占位基类——提供一个 ``process = lambda doc: doc`` 默认实现。"""

    def process(self, doc):  # noqa: D401
        return doc


class _A(_StubProcessor):
    step_name = "a"
    REQUIRES_DEFAULT = {"x"}
    PROVIDES_DEFAULT = {"a_out"}


class _B(_StubProcessor):
    step_name = "b"
    REQUIRES_DEFAULT = {"a_out"}
    PROVIDES_DEFAULT = {"b_out"}


class _Cycle(_StubProcessor):
    """b -> a -> b 形成环。"""

    step_name = "cyc"
    REQUIRES_DEFAULT = {"b_out"}
    PROVIDES_DEFAULT = {"x"}


class _Missing(_StubProcessor):
    step_name = "miss"
    REQUIRES_DEFAULT = {"y"}  # 无人提供 y
    PROVIDES_DEFAULT = {"miss_out"}


class _X1(_StubProcessor):
    step_name = "x1"
    REQUIRES_DEFAULT = set()
    PROVIDES_DEFAULT = {"shared"}


class _X2(_StubProcessor):
    step_name = "x2"
    REQUIRES_DEFAULT = set()
    PROVIDES_DEFAULT = {"shared"}


class _Dup1(_StubProcessor):
    step_name = "dup"
    REQUIRES_DEFAULT = set()
    PROVIDES_DEFAULT = {"a"}


class _Dup2(_StubProcessor):
    step_name = "dup"
    REQUIRES_DEFAULT = set()
    PROVIDES_DEFAULT = {"b"}


class _Anon(_StubProcessor):
    REQUIRES_DEFAULT = set()
    PROVIDES_DEFAULT = {"a"}


# ---------------------------------------------------------------------------
# fixtures —— 共用的 5 步流水线（生产配置）
# ---------------------------------------------------------------------------


EXPECTED_ORDER = [
    "segmenter",
    "extractor",
    "cross_validator",
    "rule_checker",
    "semantic_checker",
]


def _build_default_pipeline() -> BillingValidationPipeline:
    # 故意在函数内 import：避免循环引用
    from case_bill_validation.pipeline.cross_validator import (
        CrossValidatorProcessor,
    )
    from case_bill_validation.pipeline.extractor import ExtractorProcessor
    from case_bill_validation.pipeline.rule_checker import RuleCheckerProcessor
    from case_bill_validation.pipeline.segmenter import SegmenterProcessor
    from case_bill_validation.pipeline.semantic_checker import (
        SemanticCheckerProcessor,
    )

    return BillingValidationPipeline(
        [
            SegmenterProcessor(),
            ExtractorProcessor(),
            CrossValidatorProcessor(),
            RuleCheckerProcessor(),
            SemanticCheckerProcessor(),
        ]
    )


def _sample_doc(fixtures_dir) -> BillingCaseDocument:
    """用 fixture/discharge_01.txt 加载脱敏出院小结作为 ``raw_text``。

    fixtures_dir 由 pytest 提供（conftest.py 中定义）。
    """
    p = fixtures_dir / "discharge_01.txt"
    if p.exists():
        raw = p.read_text(encoding="utf-8")
    else:
        raw = "出院医嘱：注意休息，定期复查。\n诊断：上呼吸道感染。\n"
    return BillingCaseDocument(case_id="case-001", raw_text=raw)


# ---------------------------------------------------------------------------
# 1) 5 个 processor 顺序执行
# ---------------------------------------------------------------------------


def test_pipeline_runs_five_steps_in_order(fixtures_dir):
    """默认 5 个 processor 按 segmenter → extractor → cross_validator →
    rule_checker → semantic_checker 顺序执行；``processed_steps`` 含全部。
    """
    pipe = _build_default_pipeline()
    assert pipe.order == EXPECTED_ORDER

    doc = _sample_doc(fixtures_dir)
    out = pipe.run(doc)

    assert out is doc  # 同一对象，编排器不替换载体
    assert out.processed_steps == EXPECTED_ORDER


def test_pipeline_step_durations_populated_by_run(fixtures_dir):
    """``run`` 完成后 ``get_status`` 返回每步耗时。"""
    pipe = _build_default_pipeline()
    doc = _sample_doc(fixtures_dir)
    pipe.run(doc)
    status = pipe.get_status()
    processors = status["processors"]
    assert len(processors) == 5
    for entry in processors:
        assert entry["step_name"] in EXPECTED_ORDER
        # 占位 processor 也应记录到 >=0 的耗时
        assert entry["duration_ms"] is not None
        assert entry["duration_ms"] >= 0.0


# ---------------------------------------------------------------------------
# 2) REQUIRES 由上一步 PROVIDES 满足（链路自洽）
# ---------------------------------------------------------------------------


def test_pipeline_requires_provides_chain_satisfied():
    """每个 processor 的 REQUIRES 都由前序 processor 的 PROVIDES 提供。"""
    pipe = _build_default_pipeline()
    provided: set[str] = set()
    for name in pipe.order:
        proc = pipe.processors[name]
        # 该 processor 自己要求的所有键必须已在前置 provide 中
        assert proc.requires.issubset(provided | proc.provides), (
            f"{name} requires={sorted(proc.requires)} "
            f"not in prior provides={sorted(provided)}"
        )
        provided |= proc.provides


def test_pipeline_processed_doc_has_spans_after_segmenter(fixtures_dir):
    """segmenter 跑完后 ``doc.spans`` 非空（链路前一步产出落在下一步输入）。"""
    pipe = _build_default_pipeline()
    doc = _sample_doc(fixtures_dir)
    pipe.run(doc)
    # Task 4 segmenter 已在 fixture 上写出至少段落 + 句子 span
    assert len(doc.spans) >= 1
    assert isinstance(doc.sections, dict)


# ---------------------------------------------------------------------------
# 3) processor 抛错时降级而不中断流水线
# ---------------------------------------------------------------------------


class ExplodingProcessor(_StubProcessor):
    """测试用：每次 process 都抛 RuntimeError。"""

    step_name = "exploder"
    REQUIRES_DEFAULT = set()
    PROVIDES_DEFAULT = {"exploded"}

    def __init__(self, exc: BaseException | None = None) -> None:
        super().__init__()
        self._exc = exc or RuntimeError("boom")

    def process(self, doc):  # noqa: D401
        raise self._exc

    def degradation_policy(self, doc, exc):
        # 自定义降级：记录审计 + needs_manual
        doc.needs_manual = True
        doc.append_audit(
            AuditEntry(actor="pipeline", action="degraded", note=str(exc))
        )
        return doc


def test_pipeline_continues_when_a_processor_throws(fixtures_dir):
    """任一 processor 抛错 → 流水线继续跑剩余步骤；``processed_steps`` 含错误步。

    使用 ExplodingProcessor 替换 cross_validator，验证：
      - 异常未冒泡出 ``run()``；
      - ``doc.processed_steps`` 仍记录下被降级的步骤；
      - ``doc.needs_manual`` 被设为 True；
      - audit_log 含一条 note=str(exc) 的降解记录。
    """
    from case_bill_validation.pipeline.extractor import ExtractorProcessor
    from case_bill_validation.pipeline.rule_checker import RuleCheckerProcessor
    from case_bill_validation.pipeline.segmenter import SegmenterProcessor
    from case_bill_validation.pipeline.semantic_checker import (
        SemanticCheckerProcessor,
    )

    pipe = BillingValidationPipeline(
        [
            SegmenterProcessor(),
            ExtractorProcessor(),
            ExplodingProcessor(),
            RuleCheckerProcessor(),
            SemanticCheckerProcessor(),
        ]
    )

    doc = _sample_doc(fixtures_dir)
    out = pipe.run(doc)

    # 没有任何 processor 让 pipeline 中断（未抛错到调用方）
    assert out is doc
    # ExplodingProcessor 步骤仍被 ``processed_steps`` 收录
    assert "exploder" in out.processed_steps
    # 全部 5 步都在 processed_steps 中（包括被降级的 exploder）
    assert set(out.processed_steps) == {
        "segmenter",
        "extractor",
        "exploder",
        "rule_checker",
        "semantic_checker",
    }
    # needs_manual 标记被设置
    assert out.needs_manual is True
    # audit_log 含一条 note='boom' 的降解记录
    notes = [a.note for a in out.audit_log if a.action == "degraded"]
    assert any(n == "boom" for n in notes)


# ---------------------------------------------------------------------------
# 4) DAG 校验：错误配置检测
# ---------------------------------------------------------------------------


def test_dag_rejects_cycle():
    """环：A→B→A 触发 ``PipelineConfigurationError``。"""
    with pytest.raises(PipelineConfigurationError, match="环"):
        BillingValidationPipeline([_A(), _B(), _Cycle()])


def test_dag_rejects_missing_provider():
    """缺依赖：``_Missing.requires='y'`` 但无 processor provide。"""
    with pytest.raises(PipelineConfigurationError, match="'y'"):
        BillingValidationPipeline([_Missing()])


def test_dag_rejects_provides_conflict():
    """冲突：两个 processor 同时 provide 同名 key。"""
    with pytest.raises(PipelineConfigurationError, match="PROVIDES 冲突"):
        BillingValidationPipeline([_X1(), _X2()])


def test_dag_rejects_duplicate_step_name():
    """步骤名重复注册：``PipelineConfigurationError``。"""
    with pytest.raises(PipelineConfigurationError, match="重复"):
        BillingValidationPipeline([_Dup1(), _Dup2()])


def test_dag_rejects_processor_without_step_name():
    """processor 缺 ``step_name`` 字段。"""
    with pytest.raises(PipelineConfigurationError, match="step_name"):
        BillingValidationPipeline([_Anon()])


def test_dag_rejects_empty_processor_list():
    """空列表构造时报错。"""
    with pytest.raises(PipelineConfigurationError, match="至少需要一个"):
        BillingValidationPipeline([])


# ---------------------------------------------------------------------------
# 5) Pipeline 内置 list_processors / get_status API
# ---------------------------------------------------------------------------


def test_list_processors_returns_topological_order():
    pipe = _build_default_pipeline()
    assert pipe.list_processors() == EXPECTED_ORDER


def test_get_status_includes_requires_provides_for_each():
    pipe = _build_default_pipeline()
    status = pipe.get_status()
    seen_steps = {entry["step_name"] for entry in status["processors"]}
    assert seen_steps == set(EXPECTED_ORDER)
    for entry in status["processors"]:
        assert "requires" in entry
        assert "provides" in entry
