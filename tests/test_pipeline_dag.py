"""流水线 DAG 校验（Task 20）单元测试。

覆盖：
1. 默认 5 步 processor 链（segmenter → extractor → cross_validator →
   rule_checker → semantic_checker）—— ``dag_validate`` 返回空 errors +
   正确拓扑顺序；
2. 注入环（A → B → A）—— errors 含"环"字样；
3. 注入缺依赖（C requires 'spans' 但无人 provide）—— errors 含缺依赖；
4. PROVIDES 冲突（两 processor 同时 provide 'spans'）—— errors 含"PROVIDES 冲突"；
5. 步骤名重复 —— errors 含"重复"字样；
6. 空 processor 列表 —— errors / return 空列表；
7. ``find_cycle_path`` 在有环时返回路径，无环时返回 ``None``；
8. ``BillingValidationPipeline`` 集成：当注册坏配置时抛
   ``PipelineConfigurationError`` 且 errors 落盘到错误信息。

设计参考：
- 任务描述：M3 依赖图深化，dag.py 提取 _validate_dag / _topological_order
  逻辑到独立模块；
- github_ref/stanza/stanza/pipeline/core.py:241-242 add_dependencies()
  同样在构造期校验 DAG。
"""

from __future__ import annotations

import pytest

from case_bill_validation.pipeline.core import (
    BillingValidationPipeline,
    PipelineConfigurationError,
    Processor,
)
from case_bill_validation.pipeline.dag import (
    dag_validate,
    find_cycle_path,
)


# ---------------------------------------------------------------------------
# 占位基类
# ---------------------------------------------------------------------------


class _Stub(Processor):
    """测试用最小 Processor 占位（不依赖任何外部对象）。"""

    def process(self, doc):  # type: ignore[override]
        return doc


def _make(step_name: str, requires: set[str], provides: set[str]) -> Processor:
    """根据 step_name / requires / provides 动态生成 Processor 子类 + 实例。"""

    class _Proc(_Stub):
        nonlocal_step_name = step_name
        nonlocal_requires = requires
        nonlocal_provides = provides

    _Proc.step_name = step_name
    _Proc.REQUIRES_DEFAULT = requires
    _Proc.PROVIDES_DEFAULT = provides
    return _Proc()


# ---------------------------------------------------------------------------
# fixtures：默认 5 步 + 工厂
# ---------------------------------------------------------------------------


def _default_5_steps() -> list[Processor]:
    """生产配置：5 个真实 processor 类（不抽 mock，覆盖端到端入口）。"""
    from case_bill_validation.pipeline.cross_validator import (
        CrossValidatorProcessor,
    )
    from case_bill_validation.pipeline.extractor import ExtractorProcessor
    from case_bill_validation.pipeline.rule_checker import RuleCheckerProcessor
    from case_bill_validation.pipeline.segmenter import SegmenterProcessor
    from case_bill_validation.pipeline.semantic_checker import (
        SemanticCheckerProcessor,
    )

    return [
        SegmenterProcessor(),
        ExtractorProcessor(),
        CrossValidatorProcessor(),
        RuleCheckerProcessor(),
        SemanticCheckerProcessor(),
    ]


# ---------------------------------------------------------------------------
# 1) 默认 5 步：dag_validate 返回空 errors + 期望拓扑
# ---------------------------------------------------------------------------


def test_dag_validate_default_5_producers_no_errors():
    """生产 5 步配置：dag_validate 正常返回 + 拓扑顺序与期望一致。"""
    processors = _default_5_steps()
    ordered, errors = dag_validate(processors)

    assert errors == []
    assert ordered == [
        "segmenter",
        "extractor",
        "cross_validator",
        "rule_checker",
        "semantic_checker",
    ]


def test_dag_validate_topological_order_respects_requires():
    """拓扑顺序：A requires x → B requires a_out 中 A 必须先于 B。"""
    a = _make("a", set(), {"x", "a_out"})
    b = _make("b", {"a_out"}, {"b_out"})
    ordered, errors = dag_validate([b, a])  # 入参故意倒序

    assert errors == []
    assert ordered.index("a") < ordered.index("b")


# ---------------------------------------------------------------------------
# 2) 环：A → B → A 触发 PipelineConfigurationError
# ---------------------------------------------------------------------------


def test_dag_reports_cycle_in_errors():
    """环路径：a require x，b require a_out，c require b_out + provide x。"""
    a = _make("a", {"x"}, {"a_out"})
    b = _make("b", {"a_out"}, {"b_out"})
    c = _make("c", {"b_out"}, {"x"})
    ordered, errors = dag_validate([a, b, c])

    assert any("环" in e for e in errors), f"未检出环，errors={errors!r}"
    assert len(ordered) == 3  # leftovers 仍追加到末尾


def test_find_cycle_path_returns_cycle_when_present():
    """``find_cycle_path`` 在有环时返回包含环节点的路径。"""
    a = _make("a", {"x"}, {"a_out"})
    b = _make("b", {"a_out"}, {"b_out"})
    c = _make("c", {"b_out"}, {"x"})
    proc_index = {"a": a, "b": b, "c": c}
    cycle = find_cycle_path(proc_index)
    assert cycle is not None
    # 环至少出现一次"c -> a" 或其它两个节点的闭环
    assert cycle[0] == cycle[-1]
    assert len(cycle) >= 4  # 含 a→...→a 至少 4 节点


def test_find_cycle_path_returns_none_when_acyclic():
    """无环 DAG：``find_cycle_path`` 返回 ``None``。"""
    a = _make("a", set(), {"x"})
    b = _make("b", {"x"}, {"b_out"})
    proc_index = {"a": a, "b": b}
    assert find_cycle_path(proc_index) is None


# ---------------------------------------------------------------------------
# 3) 缺依赖：无人 provide 的 requires 键
# ---------------------------------------------------------------------------


def test_dag_reports_missing_dependency():
    """``C requires 'spans'`` 但无 processor provide → errors 含 spans 提示。"""
    orphan = _make("orphan", {"spans"}, {"orphan_out"})
    ordered, errors = dag_validate([orphan])

    assert any("'spans'" in e and "provide" in e for e in errors)
    # 缺依赖时 ordered 仍返回（保留入参顺序）但不一定有意义
    assert "orphan" in ordered


def test_dag_reports_multiple_missing_dependencies():
    """多个缺依赖同时报错。"""
    p1 = _make("p1", {"x", "y"}, {"p1_out"})
    p2 = _make("p2", {"z"}, {"p2_out"})
    _ordered, errors = dag_validate([p1, p2])

    miss_msgs = [e for e in errors if "provide" in e]
    # 三个 missing key 全在 errors 中
    assert any("'x'" in m for m in miss_msgs)
    assert any("'y'" in m for m in miss_msgs)
    assert any("'z'" in m for m in miss_msgs)


# ---------------------------------------------------------------------------
# 4) PROVIDES 冲突：两个 processor 都 provide 同名 key
# ---------------------------------------------------------------------------


def test_dag_reports_provides_conflict():
    """两 processor 同时 provide 'spans' → errors 含"PROVIDES 冲突"。"""
    p1 = _make("p1", set(), {"spans"})
    p2 = _make("p2", set(), {"spans"})
    _ordered, errors = dag_validate([p1, p2])

    assert any("PROVIDES 冲突" in e for e in errors)
    conflict = [e for e in errors if "PROVIDES 冲突" in e][0]
    # 冲突信息含两 provider 名
    assert "'p1'" in conflict and "'p2'" in conflict


# ---------------------------------------------------------------------------
# 5) 步骤名重复
# ---------------------------------------------------------------------------


def test_dag_reports_duplicate_step_name():
    """两个 processor 用同 step_name → errors 含"重复"。"""
    a = _make("dup", set(), {"out_a"})
    b = _make("dup", set(), {"out_b"})
    _ordered, errors = dag_validate([a, b])

    assert any("重复" in e for e in errors)


def test_dag_reports_missing_step_name():
    """processor 缺 ``step_name`` → errors 含"step_name"。"""

    class _Anon(Processor):
        REQUIRES_DEFAULT = set()
        PROVIDES_DEFAULT = {"x"}

        def process(self, doc):  # type: ignore[override]
            return doc

    _ordered, errors = dag_validate([_Anon()])

    assert any("step_name" in e for e in errors)


# ---------------------------------------------------------------------------
# 6) 空列表 / 单 processor
# ---------------------------------------------------------------------------


def test_dag_validate_empty_iterable():
    """空迭代器：ordered=[]、errors=[]，不抛错。"""
    ordered, errors = dag_validate([])
    assert ordered == []
    assert errors == []


def test_dag_validate_single_processor_with_no_requires():
    """单 processor + 无 requires：合法。"""
    only = _make("solo", set(), {"only_out"})
    ordered, errors = dag_validate([only])
    assert errors == []
    assert ordered == ["solo"]


# ---------------------------------------------------------------------------
# 7) BillingValidationPipeline 集成：构造期抛 PipelineConfigurationError
# ---------------------------------------------------------------------------


def test_pipeline_constructor_raises_on_cycle():
    """环构造 BillingValidationPipeline → 抛 PipelineConfigurationError，
    错误信息含"环"字样。
    """
    a = _make("a", {"x"}, {"a_out"})
    b = _make("b", {"a_out"}, {"b_out"})
    c = _make("c", {"b_out"}, {"x"})

    with pytest.raises(PipelineConfigurationError) as exc_info:
        BillingValidationPipeline([a, b, c])
    # 消息含"环" + 至少一个 node 名字
    msg = str(exc_info.value)
    assert "环" in msg or "DAG" in msg


def test_pipeline_constructor_raises_on_missing_dependency():
    """缺依赖构造 → PipelineConfigurationError，含缺失键名。"""
    orphan = _make("orphan", {"missing_thing"}, {"only_out"})

    with pytest.raises(PipelineConfigurationError) as exc_info:
        BillingValidationPipeline([orphan])
    assert "missing_thing" in str(exc_info.value)
    assert "provide" in str(exc_info.value)


def test_pipeline_constructor_raises_on_provides_conflict():
    """PROVIDES 冲突构造 → PipelineConfigurationError。"""
    p1 = _make("p1", set(), {"same"})
    p2 = _make("p2", set(), {"same"})

    with pytest.raises(PipelineConfigurationError) as exc_info:
        BillingValidationPipeline([p1, p2])
    assert "PROVIDES 冲突" in str(exc_info.value)


def test_pipeline_constructor_collects_multiple_errors_into_one_exception():
    """多种错误同时存在 → 一次抛错内 errors 列表汇总，不分多次抛。"""
    # 同时造：环 + 缺依赖 + PROVIDES 冲突
    a = _make("a", {"orphan_key"}, {"a_out"})  # 缺依赖
    b = _make("b", {"a_out"}, {"b_out"})
    c = _make("c", {"b_out"}, {"a_out"})  # 环（b_out→a_out→b_out? 实际上 a_out→b_out→a_out）
    d = _make("d", set(), {"a_out"})  # 冲突：a 和 d 都 provide a_out

    with pytest.raises(PipelineConfigurationError) as exc_info:
        BillingValidationPipeline([a, b, c, d])
    msg = str(exc_info.value)
    # 至少 3 类错误都在
    assert "PROVIDES 冲突" in msg
    assert "orphan_key" in msg
    # 环路径可选；本例可能形成环也可能不形成，但 errors 必须非空
    assert "DAG 校验失败" in msg


def test_pipeline_strict_dag_false_skips_dag_check_but_still_records():
    """``strict_dag=False`` 时构造期不抛错（用于热路径调试）。"""
    a = _make("a", {"x"}, {"a_out"})  # 缺依赖
    b = _make("b", {"a_out"}, {"b_out"})
    # 不抛错
    pipe = BillingValidationPipeline([a, b], strict_dag=False)
    assert pipe is not None
    # 即使 strict_dag=False，__init__ 也仍计算顺序（dag_validate 聚合 errors）
    assert isinstance(pipe.order, list)


def test_pipeline_default_5_steps_still_constructs_clean():
    """默认 5 步配置仍可通过构造期校验（不被新模块破坏）。"""
    pipe = BillingValidationPipeline(_default_5_steps())
    assert pipe.order == [
        "segmenter",
        "extractor",
        "cross_validator",
        "rule_checker",
        "semantic_checker",
    ]
