"""病案结算清单核验系统 · 业务错误码常量。

编码区间（与 spec.md §7 错误处理层对齐）：

- **CB001-CB099** —— 输入错误（InputError）：文件 / 表单 / 字段缺失或格式无效，对应 HTTP 4xx；
- **CB101-CB199** —— 流水线错误（PipelineError）：处理器失败、依赖图非法、状态机迁移非法，对应 HTTP 422/500；
- **CB201-CB299** —— 回写错误（WritebackError）：病案系统 HTTP 客户端失败或死信，对应 HTTP 502/503；
- **CB301-CB399** —— LLM 错误（ModelGatewayError）：ModelGateway 401/超时/Schema 校验失败，对应 HTTP 502/504；
- **CB401-CB499** —— 持久化错误（PersistenceError）：审计 / SQLite 写失败或 hash 链断裂，对应 HTTP 500/503。

设计原则
--------

- **唯一**：每个 ``CBxxx`` 编号全局唯一；冲突由 :mod:`tests.test_errors` 自检。
- **可枚举**：按类别聚合到独立类，业务层既可用 ``InputErrorCode.CB001`` 取常量，
  也可用 ``"CB001"`` 字符串；``ALL_CODES`` 提供全量列表供启动期校验。
- **配套映射**：每个码携带 ``http_status`` 默认值与 ``default_message`` 描述；
  业务层抛出 :class:`case_bill_validation.errors.CaseBillError` 子类时可被覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ErrorCodeSpec:
    """单个错误码的元数据。

    Attributes:
        code: 形如 ``CB001`` 的编号。
        http_status: HTTP 默认状态码。
        default_message: 默认错误描述（中文、面向接口调用方）。
        category: 所属类别短名，便于按区间过滤。
    """

    code: str
    http_status: int
    default_message: str
    category: str

    def __str__(self) -> str:  # pragma: no cover - 调试友好
        return f"{self.code} [{self.http_status}] {self.default_message}"


# ---------------------------------------------------------------------------
# CB001-CB099 · 输入错误（InputError）
# ---------------------------------------------------------------------------


class InputErrorCode:
    """输入错误：文件 / 表单 / 字段缺失或格式无效。

    HTTP 默认 4xx；具体由 :class:`case_bill_validation.errors.InputError`
    在抛出时覆盖。
    """

    CB001 = ErrorCodeSpec("CB001", 400, "上传文件读取失败", "input")
    CB002 = ErrorCodeSpec("CB002", 415, "上传文件类型不支持（仅 PDF/Word/纯文本）", "input")
    CB003 = ErrorCodeSpec("CB003", 422, "文件编码无法识别", "input")
    CB004 = ErrorCodeSpec("CB004", 400, "上传文件为空", "input")
    CB005 = ErrorCodeSpec("CB005", 400, "case_id 缺失或格式无效", "input")
    CB006 = ErrorCodeSpec("CB006", 422, "字段值越界（span 不在原文边界内）", "input")
    CB007 = ErrorCodeSpec("CB007", 400, "上传路径越权（不允许相对路径或 ..）", "input")
    CB008 = ErrorCodeSpec("CB008", 400, "请求体缺少必填表单字段", "input")

    ALL: Final[tuple[ErrorCodeSpec, ...]] = (
        CB001,
        CB002,
        CB003,
        CB004,
        CB005,
        CB006,
        CB007,
        CB008,
    )


# ---------------------------------------------------------------------------
# CB101-CB199 · 流水线错误（PipelineError）
# ---------------------------------------------------------------------------


class PipelineErrorCode:
    """流水线错误：处理器失败、依赖图非法、状态机迁移非法。

    HTTP 默认 422（业务规则失败）/ 500（内部错误）；具体由
    :class:`case_bill_validation.errors.PipelineError` 在抛出时覆盖。
    """

    CB101 = ErrorCodeSpec("CB101", 500, "处理器抛错（pipeline.run 内部异常）", "pipeline")
    CB102 = ErrorCodeSpec("CB102", 422, "状态机非法迁移（字段状态变更不符合 ALLOWED 表）", "pipeline")
    CB103 = ErrorCodeSpec("CB103", 500, "processor 依赖图存在环", "pipeline")
    CB104 = ErrorCodeSpec("CB104", 500, "processor REQUIRES 缺失（前序步骤未提供）", "pipeline")
    CB105 = ErrorCodeSpec("CB105", 500, "processor PROVIDES 重复（同一资源被多个步骤声明）", "pipeline")
    CB106 = ErrorCodeSpec("CB106", 422, "单据状态非法迁移（document_state 转换被拒）", "pipeline")
    CB107 = ErrorCodeSpec("CB107", 422, "processor 抛错且降级策略未配置", "pipeline")
    CB108 = ErrorCodeSpec("CB108", 500, "段落切分失败（regex 全部未命中）", "pipeline")

    ALL: Final[tuple[ErrorCodeSpec, ...]] = (
        CB101,
        CB102,
        CB103,
        CB104,
        CB105,
        CB106,
        CB107,
        CB108,
    )


# ---------------------------------------------------------------------------
# CB201-CB299 · 回写错误（WritebackError）
# ---------------------------------------------------------------------------


class WritebackErrorCode:
    """病案系统 HTTP 客户端回写错误。

    HTTP 默认 502（上游服务不可达）/ 503（自身已达重试上限写死信）；具体由
    :class:`case_bill_validation.errors.WritebackError` 在抛出时覆盖。
    """

    CB201 = ErrorCodeSpec("CB201", 502, "病案系统回写失败（HTTP 4xx 客户端错）", "writeback")
    CB202 = ErrorCodeSpec("CB202", 502, "病案系统回写失败（HTTP 5xx 服务端错）", "writeback")
    CB203 = ErrorCodeSpec("CB203", 504, "病案系统回写超时（连接 / 读超时）", "writeback")
    CB204 = ErrorCodeSpec("CB204", 503, "病案系统回写指数退避 3 次后写死信目录", "writeback")
    CB205 = ErrorCodeSpec("CB205", 500, "病案系统 URL / 凭据未配置", "writeback")
    CB206 = ErrorCodeSpec("CB206", 502, "病案系统连接被拒（DNS / TCP 失败）", "writeback")

    ALL: Final[tuple[ErrorCodeSpec, ...]] = (
        CB201,
        CB202,
        CB203,
        CB204,
        CB205,
        CB206,
    )


# ---------------------------------------------------------------------------
# CB301-CB399 · LLM 错误（ModelGatewayError）
# ---------------------------------------------------------------------------


class ModelGatewayErrorCode:
    """LLM 网关错误。

    HTTP 默认 502（上游不可达）/ 504（超时）；具体由
    :class:`case_bill_validation.errors.ModelGatewayError` 在抛出时覆盖。
    """

    CB301 = ErrorCodeSpec("CB301", 502, "ModelGateway 401（凭据缺失或失效）", "llm")
    CB302 = ErrorCodeSpec("CB302", 502, "ModelGateway 500+（上游服务错）", "llm")
    CB303 = ErrorCodeSpec("CB303", 504, "ModelGateway 请求超时", "llm")
    CB304 = ErrorCodeSpec("CB304", 502, "ModelGateway 响应非 JSON 或无法解析", "llm")
    CB305 = ErrorCodeSpec("CB305", 422, "ModelGateway Schema 校验失败（缺字段或类型错）", "llm")
    CB306 = ErrorCodeSpec("CB306", 502, "ModelGateway 凭据未配置", "llm")
    CB307 = ErrorCodeSpec("CB307", 502, "ModelGateway 重试 1 次后仍失败", "llm")

    ALL: Final[tuple[ErrorCodeSpec, ...]] = (
        CB301,
        CB302,
        CB303,
        CB304,
        CB305,
        CB306,
        CB307,
    )


# ---------------------------------------------------------------------------
# CB401-CB499 · 持久化错误（PersistenceError）
# ---------------------------------------------------------------------------


class PersistenceErrorCode:
    """持久化错误：审计 / SQLite 写失败或 hash 链断裂。

    HTTP 默认 500（自身 IO 失败）/ 503（下游存储不可用）；具体由
    :class:`case_bill_validation.errors.PersistenceError` 在抛出时覆盖。
    """

    CB401 = ErrorCodeSpec("CB401", 500, "SQLite 写失败（billing_fields.db 锁或 IO 错）", "persistence")
    CB402 = ErrorCodeSpec("CB402", 500, "审计目录权限不足（chmod 0o555 后仍尝试写）", "persistence")
    CB403 = ErrorCodeSpec("CB403", 500, "审计 hash 链断点（被篡改或断行）", "persistence")
    CB404 = ErrorCodeSpec("CB404", 500, "审计 JSON Lines 解析失败（单行损坏）", "persistence")
    CB405 = ErrorCodeSpec("CB405", 503, "审计目录创建失败（磁盘满 / 权限）", "persistence")

    ALL: Final[tuple[ErrorCodeSpec, ...]] = (
        CB401,
        CB402,
        CB403,
        CB404,
        CB405,
    )


# ---------------------------------------------------------------------------
# 全量索引（启动期唯一性 / HTTP 状态映射 / 类别过滤）
# ---------------------------------------------------------------------------


ALL_CODES: Final[tuple[ErrorCodeSpec, ...]] = (
    *InputErrorCode.ALL,
    *PipelineErrorCode.ALL,
    *WritebackErrorCode.ALL,
    *ModelGatewayErrorCode.ALL,
    *PersistenceErrorCode.ALL,
)


_CATEGORY_PREFIX: Final[tuple[tuple[str, type], ...]] = (
    ("CB0", InputErrorCode),
    ("CB1", PipelineErrorCode),
    ("CB2", WritebackErrorCode),
    ("CB3", ModelGatewayErrorCode),
    ("CB4", PersistenceErrorCode),
)


def codes_by_category(category: str) -> tuple[ErrorCodeSpec, ...]:
    """按类别短名（``input`` / ``pipeline`` / ``writeback`` / ``llm`` / ``persistence``）过滤。"""
    return tuple(spec for spec in ALL_CODES if spec.category == category)


def spec_for(code: str) -> ErrorCodeSpec:
    """按编号（如 ``"CB001"``）查 :class:`ErrorCodeSpec`；找不到抛 :class:`KeyError`。"""
    for spec in ALL_CODES:
        if spec.code == code:
            return spec
    raise KeyError(f"unknown error code: {code!r}")


def assert_codes_unique() -> None:
    """启动期校验：所有码唯一。重复时抛 :class:`AssertionError`。

    由 :mod:`tests.test_errors` 调用；亦可在 ``__main__`` 启动时调用。
    """
    seen: set[str] = set()
    for spec in ALL_CODES:
        assert spec.code not in seen, f"duplicate error code: {spec.code}"
        seen.add(spec.code)
    assert len(seen) == len(ALL_CODES), "ALL_CODES 长度与去重后长度不一致"
