"""Pipeline 编排骨架：Processor 抽象基类 + BillingValidationPipeline 编排器。

设计参考：
- ``github_ref/stanza/stanza/pipeline/core.py:176-513`` Pipeline 主类：
  - ``processors`` 按步骤名注册（dict）；
  - ``process(doc, processors=None)`` 按 PIPELINE_NAMES 顺序累加；
  - 任一 processor 出错时按各自的降级策略继续。
- ``github_ref/stanza/stanza/pipeline/constituency_processor.py`` 11-20 行的
  ``PROVIDES_DEFAULT`` / ``REQUIRES_DEFAULT`` 注册模式。

REQUIRES / PROVIDES 协议
-------------------------
- ``requires: set[str]`` —— 该 processor 在 ``process(doc)`` 之前需要哪些键；
  编排器在拓扑排序 + DAG 校验时检查"上一步 processor 的 PROVIDES 覆盖这一步
  requires"。缺依赖 / 环 / 重复 PROVIDES 由 ``PipelineConfigurationError`` 抛。
- ``provides: set[str]`` —— ``process(doc)`` 完成后向 ``doc`` 注入的键；
  编排器记录到 ``doc.processed_steps`` 并暴露给 SSE 进度广播。

降级
----
任何 processor 抛错时，编排器调 ``processor.degradation_policy(doc, exc)``：
默认置 ``doc.needs_manual = True`` 并继续；子类可覆盖为"跳过本步骤"。
任一 processor 在抛错后必须返回 doc（即使是空壳）让流水线继续。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from case_bill_validation.document import BillingCaseDocument
from case_bill_validation.pipeline.dag import dag_validate, find_cycle_path

logger = logging.getLogger(__name__)


class PipelineConfigurationError(ValueError):
    """流水线配置错误（DAG 环 / REQUIRES 缺依赖 / PROVIDES 重复）。"""


class Processor(ABC):
    """流水线 processor 抽象基类。

    命名空间约定：
    - 步骤名（如 ``"segmenter"``）作 key 注册到编排器；
    - ``requires`` / ``provides`` 用文档上的命名键空间字符串（``"spans"`` 、
      ``"sections"`` 、``"extracted_fields"`` 等），便于跨步骤松耦合。

    子类必须实现 :meth:`process`；可选择性覆盖 :meth:`degradation_policy`
    来描述某字段抽取失败时该字段的状态推进目标（默认置 ``needs_manual``）。
    """

    #: 默认 requires；子类应覆盖。
    REQUIRES_DEFAULT: set[str] = set()
    #: 默认 provides；子类应覆盖。
    PROVIDES_DEFAULT: set[str] = set()

    #: 步骤名（注册到编排器用）；子类应覆盖。
    step_name: str = ""

    def __init__(self, **kwargs: Any) -> None:
        self._config: dict[str, Any] = kwargs
        # 实例级 requires/provides（与 default 解耦，便于测试注入）
        self.requires: set[str] = set(self.REQUIRES_DEFAULT)
        self.provides: set[str] = set(self.PROVIDES_DEFAULT)

    @abstractmethod
    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        """处理文档；返回 doc（同一对象或新对象均可，推荐同一对象 + record_step）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 降级策略
    # ------------------------------------------------------------------

    def degradation_policy(
        self, doc: BillingCaseDocument, exc: BaseException
    ) -> BillingCaseDocument:
        """默认降级策略：doc.needs_manual = True + 记录到 audit 后继续。

        子类可覆盖为"跳过本步骤"或"用规则替代 LLM"。
        """
        logger.warning(
            "processor %s 抛错: %s；按默认降级策略继续",
            self.step_name,
            exc,
        )
        doc.needs_manual = True
        return doc


class BillingValidationPipeline:
    """5 步链式 NLP 流水线的编排器。

    构造时按步骤名注册 ``dict[str, Processor]`` 实例，自动做：
      - 步骤名去重；
      - ``step_name`` 字段与 key 一致性自检；
      - DAG 校验（无环 + REQUIRES/PROVIDES 一致）。

    :meth:`run` 按拓扑顺序依次调用 ``processor.process(doc)``；任一抛错时调
    ``processor.degradation_policy(doc, exc)`` 后继续。完成后 ``doc.processed_steps``
    含全部成功步骤的 ``step_name``（用于 SSE 进度广播）。

    设计参考 ``github_ref/stanza/stanza/pipeline/core.py:390-431`` 的 ``process``
    —— 按 PIPELINE_NAMES 顺序累加，对应到本设计的拓扑顺序。
    """

    def __init__(self, processors: list[Processor], *, strict_dag: bool = True) -> None:
        if not processors:
            raise PipelineConfigurationError("至少需要一个 processor")

        # 注册：按步骤名去重（同名重复抛错）
        self.processors: dict[str, Processor] = {}
        for proc in processors:
            if not proc.step_name:
                raise PipelineConfigurationError(
                    f"processor {type(proc).__name__} 缺少 step_name"
                )
            if proc.step_name in self.processors:
                raise PipelineConfigurationError(
                    f"步骤名重复注册: {proc.step_name}"
                )
            self.processors[proc.step_name] = proc

        if strict_dag:
            self._validate_dag()

        # 拓扑顺序：按 REQUIRES 关系排序（委托给 dag 模块）
        ordered, _errors = dag_validate(self.processors.values())
        self._order = ordered

        # 步骤耗时统计（run 一次填充一次）
        self._step_durations_ms: dict[str, float] = {}

    # ------------------------------------------------------------------
    # DAG 校验
    # ------------------------------------------------------------------

    def _validate_dag(self) -> None:
        """校验 DAG：REQUIRES / PROVIDES 一致 + 无环（委托 ``dag.dag_validate``）。

        规则：
          - 每个 requires 键必须有同名 provider（其它 processor 的 provides）；
          - 同一 provides 键只能由一个 processor 提供（避免覆盖歧义）；
          - PROVIDES 依赖图 DFS 不可出现环；
          - ``step_name`` 必须非空且唯一。

        任何一条不满足都抛 :class:`PipelineConfigurationError`，错误信息汇总一次抛出。
        """
        _ordered, errors = dag_validate(self.processors.values())
        if errors:
            # 优先把环路径单独打印（更可读），再列全 errors
            cycle = find_cycle_path(self.processors)
            extra = ""
            if cycle is not None:
                extra = " | 环路径: " + " -> ".join(cycle)
            raise PipelineConfigurationError(
                "流水线 DAG 校验失败:\n  - "
                + "\n  - ".join(errors)
                + extra
            )

    # ------------------------------------------------------------------
    # 对外主路径
    # ------------------------------------------------------------------

    def run(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        """按拓扑序依次执行 5 个 processor；任一抛错时降级继续。

        返回 doc 同一对象（含 ``processed_steps`` + ``audit_log``）。
        """
        with logger_context(logger):
            for step_name in self._order:
                proc = self.processors[step_name]
                t0 = time.perf_counter()
                try:
                    doc = proc.process(doc)
                except BaseException as exc:  # noqa: BLE001 —— 编排器统一捕获
                    # 不让任何 processor 让流水线整体中断；按降级策略继续
                    logger.info(
                        "processor %s 抛错，调 degradation_policy: %s",
                        step_name,
                        exc,
                    )
                    doc = proc.degradation_policy(doc, exc)
                self._step_durations_ms[step_name] = (
                    time.perf_counter() - t0
                ) * 1000.0
                # 标记成功（或被降级）步骤已"处理"
                if step_name not in doc.processed_steps:
                    doc.processed_steps.append(step_name)
        return doc

    # ------------------------------------------------------------------
    # 调试 / 暴露状态
    # ------------------------------------------------------------------

    @property
    def _registered_processors(self) -> list[Processor]:
        """按插入顺序回放构造期入参 ``processors``（不挑 dict 顺序）。

        ``self.processors`` 是 dict，3.7+ 保留插入顺序，因此直接用 values()。
        """
        return list(self.processors.values())

    @property
    def order(self) -> list[str]:
        """只读：返回拓扑顺序。"""
        return list(self._order)

    def list_processors(self) -> list[str]:
        return list(self._order)

    def get_status(self) -> dict[str, Any]:
        """调试用：当前流水线拓扑 + 上次 run 耗时（若有）。"""
        return {
            "processors": [
                {
                    "step_name": name,
                    "requires": sorted(self.processors[name].requires),
                    "provides": sorted(self.processors[name].provides),
                    "duration_ms": self._step_durations_ms.get(name),
                }
                for name in self._order
            ],
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _NullContext:
    """极简 logger context 占位（保留扩展点）。"""

    def __enter__(self) -> "_NullContext":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def logger_context(_logger: logging.Logger) -> Any:
    """返回 logger 上下文管理器；占位实现，子类可替换。"""
    return _NullContext()


__all__ = [
    "Processor",
    "BillingValidationPipeline",
    "PipelineConfigurationError",
    "logger_context",
]
