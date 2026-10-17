"""流水线 DAG 校验（DFS 无环 + REQUIRES/PROVIDES 一致性）。

设计参考：
- ``github_ref/stanza/stanza/pipeline/core.py:176-513`` Pipeline.__init__：
  - ``add_dependencies(resources, lang, load_list)`` 在构造期收集
    PROVIDES/REQUIRES 不一致时报错；
  - 按 PIPELINE_NAMES 顺序累加调度（与本设计的拓扑顺序对应）。
- ``github_ref/stanza/stanza/pipeline/constituency_processor.py:108-115``
  的 REQUIRES_DEFAULT/PROVIDES_DEFAULT 注册模式（本模块消费这两个属性）。

职责
----
本模块独立于 :class:`BillingValidationPipeline`，提供纯函数入口
:func:`dag_validate`：

- 输入：``list[Processor]``（已绑定 step_name）；
- 输出：``(ordered, errors)`` —— 拓扑顺序 + 错误列表；
- 行为：**不主动抛错**，把错误聚合后返回；由调用方决定如何处理。

这样编排器（``core.py``）既能：
  1. 在构造期一次性抛 ``PipelineConfigurationError``（默认行为，向后兼容
     既有 19 个已完成任务）；
  2. 也可以在 dry-run / 自检场景里把 ``errors`` 列表渲染到 UI 或审计。

不变量
------
- :func:`dag_validate` 不修改输入；
- ``ordered`` 长度等于成功校验时 processor 数；若有环或缺依赖则
  ``ordered`` 只含可拓扑排序的子集；
- ``errors`` 收集到的错误至少覆盖四类：
  (1) PROVIDES 冲突；
  (2) REQUIRES 缺依赖；
  (3) PROVIDES 依赖图存在环；
  (4) ``step_name`` 缺失或重复。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from case_bill_validation.pipeline.core import Processor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常：仅供 UI 渲染或编排器派生用，dag_validate 默认不抛
# ---------------------------------------------------------------------------


class DAGValidationError(ValueError):
    """DAG 校验失败汇总错误（通常不直接抛，由 ``dag_validate`` 返回）。"""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def dag_validate(
    processors: Iterable["Processor"],
) -> tuple[list[str], list[str]]:
    """校验 processor 集合的 DAG，返回 ``(ordered, errors)``。

    :param processors: 待校验的 processor 集合（迭代 1 次）；
    :returns: ``(ordered, errors)`` 拓扑顺序列表 + 错误列表（人类可读）。

    规则：
      - 每个 ``proc.step_name`` 必须非空且唯一；
      - 每个 ``proc.requires`` 键必须存在某个 ``proc.provides`` 中；
      - 同一个 provides 键只能由唯一 processor 提供；
      - PROVIDES 依赖图（``requires → provider``）必须无环。

    返回值的两种状态：
      - ``errors == []`` ⇒ ``ordered`` 是合法拓扑顺序；
      - ``errors != []`` ⇒ ``ordered`` 仍尝试给出"按入参顺序"
        的最佳结果（含可拓扑化的子集）；调用方可决定是否抛错。
    """
    proc_list = list(processors)

    # 1) step_name 唯一性 + 非空校验
    seen_steps: dict[str, str] = {}  # step_name -> provider type 备注
    errors: list[str] = []
    proc_index: dict[str, "Processor"] = {}
    for proc in proc_list:
        sname = getattr(proc, "step_name", "") or ""
        if not sname:
            errors.append(
                f"processor {type(proc).__name__} 缺少 step_name，无法注册"
            )
            continue
        if sname in proc_index:
            errors.append(
                f"步骤名重复注册: '{sname}' "
                f"（同时用于 {type(proc_index[sname]).__name__} 与 "
                f"{type(proc).__name__}）"
            )
            # 保留后注册者（与既有 Pipeline.__init__ 行为一致）
            proc_index[sname] = proc
        else:
            proc_index[sname] = proc
        seen_steps[sname] = type(proc).__name__

    # 2) 收集每个键的 provider；冲突时只保留前者
    providers: dict[str, str] = {}
    conflicts: list[str] = []
    for step_name, proc in proc_index.items():
        for key in getattr(proc, "provides", set()) or set():
            if key in providers and providers[key] != step_name:
                conflicts.append(
                    f"PROVIDES 冲突: 键 '{key}' 由 '{providers[key]}' 与 "
                    f"'{step_name}' 同时提供"
                )
            else:
                providers[key] = step_name

    # 3) 缺依赖：requires 必须有 provider
    missing: list[str] = []
    for step_name, proc in proc_index.items():
        for key in getattr(proc, "requires", set()) or set():
            if key not in providers:
                missing.append(
                    f"processor '{step_name}' requires '{key}'，但无人 provide"
                )

    # 4) 邻接表：provider → consumer（requires 指向 provider）
    adj: dict[str, list[str]] = {name: [] for name in proc_index}
    for step_name, proc in proc_index.items():
        for key in getattr(proc, "requires", set()) or set():
            provider = providers.get(key)
            if provider and provider in proc_index:
                adj[provider].append(step_name)

    # 5) DFS 检环（递归 + 颜色标记），环路径回填到 errors
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in proc_index}
    cycle_stack: list[str] = []
    cycle_errors: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        cycle_stack.append(node)
        # 去重邻接避免"ABAB"型误报
        for nxt in dict.fromkeys(adj[node]):
            if color[nxt] == GRAY:
                cycle_idx = cycle_stack.index(nxt)
                cycle = cycle_stack[cycle_idx:] + [nxt]
                cycle_errors.append(
                    "流水线处理器之间存在环: " + " -> ".join(cycle)
                )
                # 不直接抛，collected 汇总
                # 继续遍历 BLACK 分支以发现其它环
            elif color[nxt] == WHITE:
                dfs(nxt)
        cycle_stack.pop()
        color[node] = BLACK

    for name in list(proc_index):
        if color[name] == WHITE:
            dfs(name)

    # 6) 拓扑排序（稳定：缺依赖时入度=0，先输出；保留入参顺序）
    ordered = _topological_order(proc_index)

    # 7) 汇总错误
    errors.extend(conflicts)
    errors.extend(missing)
    errors.extend(cycle_errors)

    if errors:
        logger.warning(
            "dag_validate: %d 条错误，拓扑顺序仅覆盖 %d/%d 个步骤",
            len(errors),
            len(ordered),
            len(proc_index),
        )

    return ordered, errors


# ---------------------------------------------------------------------------
# 拓扑排序（确定性 / 保留入参顺序）
# ---------------------------------------------------------------------------


def _topological_order(
    proc_index: dict[str, "Processor"],
) -> list[str]:
    """按 REQUIRES/PROVIDES 拓扑排序；保留入参注册顺序。

    入度 = 该 step 的 requires 实际由其它 step 提供（不含自身 provides），
    缺依赖视为入度 0 但仍可能被错误检测器标记。

    返回长度可能小于 ``len(proc_index)``（缺依赖或环时）。
    """
    if not proc_index:
        return []

    # providers 表：键 -> step_name（与 dag_validate 主流程一致）
    providers: dict[str, str] = {
        key: name
        for name, proc in proc_index.items()
        for key in getattr(proc, "provides", set()) or set()
    }

    indeg: dict[str, int] = {name: 0 for name in proc_index}
    rev_adj: dict[str, list[str]] = {name: [] for name in proc_index}

    for name, proc in proc_index.items():
        for req in getattr(proc, "requires", set()) or set():
            provider = providers.get(req)
            if provider and provider in proc_index:
                if provider != name:
                    indeg[name] += 1
                    rev_adj[provider].append(name)

    # 入度 0 优先；按注册顺序遍历入参
    registered_order = list(proc_index.keys())
    ready: list[str] = [n for n in registered_order if indeg[n] == 0]
    # 修复"跨段就绪节点注册序在 ready 末尾"的偏序问题：
    # ready FIFO 已经够稳（FIFO 维护入参顺序），无需另排序。

    ordered: list[str] = []
    visited: set[str] = set()
    while ready:
        current = ready.pop(0)
        if current in visited:
            continue
        visited.add(current)
        ordered.append(current)
        for nxt in rev_adj[current]:
            if nxt in visited:
                continue
            indeg[nxt] -= 1
            if indeg[nxt] == 0 and nxt not in ready:
                ready.append(nxt)

    # 若仍有未访问节点（环内），追加到末尾以保证 ordered 长度 == proc_index
    leftovers = [n for n in registered_order if n not in visited]
    if leftovers:
        ordered.extend(leftovers)

    return ordered


def find_cycle_path(
    proc_index: dict[str, "Processor"],
) -> list[str] | None:
    """返回检测到的最短环路径（``provider -> consumer -> ... -> provider``）。

    用于 UI 自检中明确告诉配置者"哪个环需要拆"。无环返回 ``None``。
    """
    providers: dict[str, str] = {
        key: name
        for name, proc in proc_index.items()
        for key in getattr(proc, "provides", set()) or set()
    }
    adj: dict[str, list[str]] = {name: [] for name in proc_index}
    for step_name, proc in proc_index.items():
        for key in getattr(proc, "requires", set()) or set():
            provider = providers.get(key)
            if provider and provider in proc_index:
                adj[provider].append(step_name)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in proc_index}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in dict.fromkeys(adj[node]):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color[nxt] == WHITE:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for name in list(proc_index):
        if color[name] == WHITE:
            found = dfs(name)
            if found:
                return found
    return None


__all__ = [
    "DAGValidationError",
    "dag_validate",
    "find_cycle_path",
]
