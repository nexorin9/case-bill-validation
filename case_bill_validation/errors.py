"""病案结算清单核验系统 · 业务异常基类 + 5 个子类。

设计要点（与 ``error_codes.py`` 配对）
-----------------------------------

- **基类** :class:`CaseBillError` 携带 ``error_code`` / ``http_status`` / ``message`` /
  ``details`` / ``cause`` 五字段，对应 API 路由层 ``HTTPException`` 序列化；
- **5 个子类**分别对应 :class:`error_codes.InputErrorCode` /
  :class:`error_codes.PipelineErrorCode` / :class:`error_codes.WritebackErrorCode` /
  :class:`error_codes.ModelGatewayErrorCode` / :class:`error_codes.PersistenceErrorCode`；
  默认 ``http_status`` 来自 :class:`error_codes.ErrorCodeSpec`，
  抛出时可被 ``http_status=`` 参数覆盖（例如回写本地占位时降为 503）；
- **cause chain 透传**：构造时 ``cause=`` 透传原始异常；``to_dict()`` 递归序列化；
- **JSON 兼容** ``to_dict()`` 输出仅含 ``str / int / list / dict / None``，
  可直接 ``json.dumps``；
- **与既有散落异常的兼容性**：

  - ``case_bill_validation.export.case_writeback.WritebackError`` 仍是模块内
    ``RuntimeError`` 别名（``from .errors import WritebackError as CaseSystemWritebackError``
    风格桥接留给调用方按需做），本模块的 :class:`WritebackError` 不破坏既有用法；
  - ``InvalidTransitionError`` / ``PipelineConfigurationError`` / ``GatewayError`` 等
    独立异常仍按各自模块的定义存在；本模块为业务顶层异常的统一入口。
"""

from __future__ import annotations

from typing import Any, Mapping

from case_bill_validation.error_codes import (
    ErrorCodeSpec,
    InputErrorCode,
    ModelGatewayErrorCode,
    PersistenceErrorCode,
    PipelineErrorCode,
    WritebackErrorCode,
)


class CaseBillError(Exception):
    """业务顶层异常基类。

    Attributes:
        error_code: ``CBxxx`` 编号字符串（如 ``"CB001"``）。
        http_status: 抛出 API 路由时对应的 HTTP 状态码（默认 500）。
        message: 面向调用方的人类可读中文描述。
        details: 任意结构化补充信息（如 ``field_name`` / ``span`` /
          ``attempts`` 等），仅 ``str/int/list/dict/None``；非 JSON 兼容值
          由 :meth:`to_dict` 转 ``str`` 兜底。
        cause: 透传的原始异常；``str(cause)`` 进入 :meth:`to_dict`。
    """

    error_code: str = "CB000"
    http_status: int = 500
    message: str = "Internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        error_code: str | None = None,
        http_status: int | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        if message is None:
            message = self.message
        self.message: str = str(message)
        self.error_code: str = (
            str(error_code) if error_code is not None else self.error_code
        )
        self.http_status: int = (
            int(http_status) if http_status is not None else self.http_status
        )
        # 兜底非 JSON 兼容值
        self.details: dict[str, Any] = self._coerce_details(details)
        self.cause: BaseException | None = cause
        super().__init__(self.message)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON 兼容 dict，可直接 ``json.dumps``。

        cause 链递归展开为 ``{"error_code", "message", "details", "cause"}``，
        最终 ``cause`` 字段为 ``None`` 或字符串。
        """
        out: dict[str, Any] = {
            "error_code": self.error_code,
            "http_status": self.http_status,
            "message": self.message,
            "details": dict(self.details),
            "cause": _cause_to_dict(self.cause),
        }
        return out

    def __repr__(self) -> str:  # pragma: no cover - 调试友好
        return (
            f"{type(self).__name__}(error_code={self.error_code!r}, "
            f"http_status={self.http_status}, message={self.message!r})"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_details(
        details: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not details:
            return {}
        out: dict[str, Any] = {}
        for k, v in details.items():
            key = str(k)
            if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                out[key] = v
            else:
                out[key] = str(v)
        return out

    @classmethod
    def from_spec(
        cls,
        spec: ErrorCodeSpec,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        http_status: int | None = None,
    ) -> "CaseBillError":
        """由 :class:`ErrorCodeSpec` 直接构造。

        - 在基类 :class:`CaseBillError` 上调用 → 按 ``spec.category`` 自动派发到
          对应子类（:class:`InputError` / :class:`PipelineError` /
          :class:`WritebackError` / :class:`ModelGatewayError` /
          :class:`PersistenceError`），返回**类别匹配的子类实例**；
        - 在子类上调用 → 直接返回该子类实例（等价于 ``cls(...)`` 构造）。

        常用于 pipeline 内部抛错（先拿到 spec 再挑对子类）。
        """
        if cls is CaseBillError:
            target_cls = exception_for_category(spec.category)
            return target_cls(
                message=message or spec.default_message,
                error_code=spec.code,
                http_status=http_status if http_status is not None else spec.http_status,
                details=details,
                cause=cause,
            )
        return cls(
            message=message or spec.default_message,
            error_code=spec.code,
            http_status=http_status if http_status is not None else spec.http_status,
            details=details,
            cause=cause,
        )


# ---------------------------------------------------------------------------
# cause 链递归展开
# ---------------------------------------------------------------------------


_CAUSE_RECURSION_LIMIT = 16


def _cause_to_dict(cause: BaseException | None) -> dict[str, Any] | None:
    """递归展开 cause 链 → 嵌套 ``{"message", "cause"}`` dict。

    起点为最浅层（即 :attr:`CaseBillError.cause`），向下走 ``__cause__`` →
    ``__context__``；自引用或超深时截断，最终 :attr:`cause` 落到 ``None`` 或
    缺失。
    """
    if cause is None:
        return None
    seen: set[int] = set()
    cur: BaseException | None = cause
    chain: list[str] = []
    while cur is not None and len(chain) < _CAUSE_RECURSION_LIMIT:
        if id(cur) in seen:
            # 自引用循环：截断，不再追加
            break
        seen.add(id(cur))
        chain.append(str(cur))
        nxt = cur.__cause__ or cur.__context__
        cur = nxt

    # 从最深层向前嵌套：result = {"message": 最浅, "cause": {...深层...}}
    result: dict[str, Any] | None = None
    for msg in reversed(chain):
        result = {"message": msg, "cause": result}
    return result


# ---------------------------------------------------------------------------
# 5 个子类（每个默认对应一个错误码类别）
# ---------------------------------------------------------------------------


class InputError(CaseBillError):
    """输入错误（CB001-CB099）。

    默认 HTTP 400/415/422；用于文件读取失败、类型不支持、编码无法识别、
    字段值越界等。
    """

    error_code = InputErrorCode.CB001.code
    http_status = InputErrorCode.CB001.http_status
    message = InputErrorCode.CB001.default_message

    @classmethod
    def for_spec(
        cls,
        spec: ErrorCodeSpec,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        http_status: int | None = None,
    ) -> "InputError":
        return cls(
            message=message or spec.default_message,
            error_code=spec.code,
            http_status=http_status if http_status is not None else spec.http_status,
            details=details,
            cause=cause,
        )


class PipelineError(CaseBillError):
    """流水线错误（CB101-CB199）。

    默认 HTTP 422（业务规则失败）/ 500（内部错误）；用于状态机非法迁移、
    依赖图环、缺依赖、PROVIDES 重复、单据状态非法迁移等。
    """

    error_code = PipelineErrorCode.CB101.code
    http_status = PipelineErrorCode.CB101.http_status
    message = PipelineErrorCode.CB101.default_message

    @classmethod
    def for_spec(
        cls,
        spec: ErrorCodeSpec,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        http_status: int | None = None,
    ) -> "PipelineError":
        return cls(
            message=message or spec.default_message,
            error_code=spec.code,
            http_status=http_status if http_status is not None else spec.http_status,
            details=details,
            cause=cause,
        )


class WritebackError(CaseBillError):
    """病案系统回写错误（CB201-CB299）。

    默认 HTTP 502/503/504；用于 HTTP 4xx/5xx 客户端错、连接超时、
    指数退避 3 次后写死信等。

    .. note::

        ``case_bill_validation.export.case_writeback.WritebackError`` 是
        模块内的 :class:`RuntimeError` 别名；本类为业务顶层异常，抛出方
        应 ``from case_bill_validation.errors import WritebackError``。
        既有用法不受破坏。
    """

    error_code = WritebackErrorCode.CB201.code
    http_status = WritebackErrorCode.CB201.http_status
    message = WritebackErrorCode.CB201.default_message

    @classmethod
    def for_spec(
        cls,
        spec: ErrorCodeSpec,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        http_status: int | None = None,
    ) -> "WritebackError":
        return cls(
            message=message or spec.default_message,
            error_code=spec.code,
            http_status=http_status if http_status is not None else spec.http_status,
            details=details,
            cause=cause,
        )


class ModelGatewayError(CaseBillError):
    """ModelGateway 调用错误（CB301-CB399）。

    默认 HTTP 502/504；用于 401 凭据失效、500+ 上游错、超时、非 JSON
    响应、Schema 校验失败、重试 1 次后仍失败等。
    """

    error_code = ModelGatewayErrorCode.CB301.code
    http_status = ModelGatewayErrorCode.CB301.http_status
    message = ModelGatewayErrorCode.CB301.default_message

    @classmethod
    def for_spec(
        cls,
        spec: ErrorCodeSpec,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        http_status: int | None = None,
    ) -> "ModelGatewayError":
        return cls(
            message=message or spec.default_message,
            error_code=spec.code,
            http_status=http_status if http_status is not None else spec.http_status,
            details=details,
            cause=cause,
        )


class PersistenceError(CaseBillError):
    """持久化错误（CB401-CB499）。

    默认 HTTP 500/503；用于 SQLite 写失败、审计目录权限不足、hash 链
    断点、JSON Lines 解析失败等。
    """

    error_code = PersistenceErrorCode.CB401.code
    http_status = PersistenceErrorCode.CB401.http_status
    message = PersistenceErrorCode.CB401.default_message

    @classmethod
    def for_spec(
        cls,
        spec: ErrorCodeSpec,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        http_status: int | None = None,
    ) -> "PersistenceError":
        return cls(
            message=message or spec.default_message,
            error_code=spec.code,
            http_status=http_status if http_status is not None else spec.http_status,
            details=details,
            cause=cause,
        )


# ---------------------------------------------------------------------------
# 类别 → 子类的快速映射（API 路由层根据 error_code 前两位判定）
# ---------------------------------------------------------------------------


CATEGORY_EXCEPTION_MAP: dict[str, type[CaseBillError]] = {
    "input": InputError,
    "pipeline": PipelineError,
    "writeback": WritebackError,
    "llm": ModelGatewayError,
    "persistence": PersistenceError,
}


def exception_for_category(category: str) -> type[CaseBillError]:
    """按类别短名查异常类；找不到抛 :class:`KeyError`（类别越界属编程错误）。"""
    try:
        return CATEGORY_EXCEPTION_MAP[category]
    except KeyError as exc:
        raise KeyError(f"unknown category: {category!r}") from exc


__all__ = [
    "CaseBillError",
    "InputError",
    "PipelineError",
    "WritebackError",
    "ModelGatewayError",
    "PersistenceError",
    "CATEGORY_EXCEPTION_MAP",
    "exception_for_category",
]
