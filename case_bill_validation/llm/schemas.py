"""LLM JSON Schema 结构化输出定义 + jsonschema 校验（Task 21 实装）。

为什么独立模块
-------------

之前 :mod:`case_bill_validation.llm.prompts` 内部以 Python ``dict`` 形式塞
``SEMANTIC_CHECK_SCHEMA`` / ``EXTRACTION_SCHEMA``；:mod:`gateway` 走的是
内置的精简校验（只校 ``required`` + ``properties[k].type``），无法覆盖
嵌套校验、enum、array items 等约束。

Task 21 把 schema **搬到此模块**，并切到 ``jsonschema`` 库做严格校验；
保留 schema 常量在 :mod:`llm.prompts` 的 re-export 入口，向后兼容已有
``from case_bill_validation.llm.prompts import SEMANTIC_CHECK_SCHEMA`` 调用。

设计要点
--------

- **协议一致性**：发到 LLM 的 ``json_schema`` 字段与本地校验器**用同一份
  schema dict**，避免「发给 LLM 的契约」与「落库前校验的契约」漂移；
- **校验入口**：:func:`validate_payload` 直接走 ``jsonschema.validate``，
  失败时抛 :class:`jsonschema.ValidationError`，由业务层把消息透传到
  :class:`GatewayError.audit_metadata`；
- **失败语义**：schema 校验失败视作 LLM 输出异常，**等同 HTTP 5xx 走重试**，
  ``max_attempts`` 后抛 ``GatewayError``（``downstream_status=unavailable``），
  业务层（``SemanticCheckerProcessor``）据此降级到 ``rule_checked`` +
  ``semantic_skipped`` 标记。
- **namespace**：schema 名为 ``"semantic_check"`` / ``"extraction"``（与
  LLMCallMetadata.schema_name 一致），便于审计回查。

参考
----

- ``spec.md`` §6（ModelGateway 结构化输出 JSON Schema）；
- ``plan.md`` §6（结构化输出 schema 定义）；
- jsonschema 官方 draft-07 校验器（``jsonschema.validate``）。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

import jsonschema
from jsonschema import Draft7Validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema 定义（dict-of-object，与发给 LLM 的契约完全一致）
# ---------------------------------------------------------------------------

#: 语义核 schema —— 用于 SemanticCheckerProcessor
#: ``{field, status, reason_text, evidence_span}`` 四字段。
SEMANTIC_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "field": {"type": "string", "minLength": 1},
        "status": {
            "type": "string",
            "enum": ["confirmed", "rejected", "flagged", "needs_manual"],
        },
        "reason_text": {"type": "string"},
        "evidence_span": {
            "type": "object",
            "properties": {
                "start_char": {"type": "integer", "minimum": 0},
                "end_char": {"type": "integer", "minimum": 0},
                "page_no": {"type": ["integer", "null"], "minimum": 0},
            },
            "required": ["start_char", "end_char"],
            "additionalProperties": False,
        },
    },
    "required": ["field", "status", "reason_text", "evidence_span"],
    "additionalProperties": False,
}


#: 抽取 schema —— 用于 ExtractorProcessor（LLM 抽取路径）
#: ``{fields: [{name, value, span_start, span_end, confidence}]}`` 一组结构化字段。
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "value": {"type": "string"},
                    "span_start": {"type": "integer", "minimum": 0},
                    "span_end": {"type": "integer", "minimum": 0},
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "required": ["name", "value", "span_start", "span_end"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fields"],
    "additionalProperties": False,
}


#: Schema 注册表（按名称 → schema dict 索引）。
#: 新增 schema 时 **同时** 更新 ``SCHEMAS`` 与 ``_BUILD_VALIDATORS`` 缓存。
SCHEMAS: dict[str, dict[str, Any]] = {
    "semantic_check": SEMANTIC_CHECK_SCHEMA,
    "extraction": EXTRACTION_SCHEMA,
}


# ---------------------------------------------------------------------------
# 缓存的 Validator（draft-07，避免每个 payload 重建）
# ---------------------------------------------------------------------------


_BUILD_VALIDATORS: dict[str, Draft7Validator] = {
    name: Draft7Validator(schema) for name, schema in SCHEMAS.items()
}


def get_validator(schema_name: str) -> Draft7Validator:
    """按 schema 名取预编译的 Draft7Validator。

    参数
    ----
    schema_name: :data:`SCHEMAS` 里出现的 schema 名（``"semantic_check"`` /
      ``"extraction"`` / ...）。

    抛出
    ----
    KeyError —— schema 不存在；上层若要在审计里区分，捕获后用 ``"unknown"``
    记录到 ``LLMCallMetadata.schema_name`` 即可。
    """
    try:
        return _BUILD_VALIDATORS[schema_name]
    except KeyError as exc:
        raise KeyError(
            f"未知 schema_name={schema_name!r}；可用：{sorted(SCHEMAS)}"
        ) from exc


def get_schema(schema_name: str) -> dict[str, Any]:
    """按名取 schema dict（发送 LLM 时用）。"""
    try:
        return SCHEMAS[schema_name]
    except KeyError as exc:
        raise KeyError(
            f"未知 schema_name={schema_name!r}；可用：{sorted(SCHEMAS)}"
        ) from exc


# ---------------------------------------------------------------------------
# 公开校验入口
# ---------------------------------------------------------------------------


def validate_payload(
    payload: Any, schema_name: str
) -> tuple[bool, str | None]:
    """用 jsonschema 校验 ``payload`` 是否符合指定 schema。

    参数
    ----
    payload: LLM 返回的 dict（也可能非 dict，按 schema 顶层 ``type=object`` 拒绝）；
    schema_name: :data:`SCHEMAS` 中已注册的 schema 名。

    返回
    ----
    ``(ok, error_message)``：校验通过时 ``(True, None)``；失败时
    ``(False, <jsonschema 错误摘要>)``，便于上层塞进
    :class:`GatewayError.audit_metadata["error_message"]`。
    """
    try:
        validator = get_validator(schema_name)
    except KeyError as exc:
        return False, str(exc)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if not errors:
        return True, None
    # 取第一个 error（业务侧一般只看首条）做摘要；附加总条数
    head = errors[0]
    localized = _localize_error(head)
    return False, f"{localized}（共 {len(errors)} 处失败）"


def _localize_error(err: jsonschema.ValidationError) -> str:
    """把 jsonschema 顶层错误翻译成中文友好摘要，匹配既有审计口径。

    翻译口径（与 :mod:`gateway` 的旧版本保持一致，便于审计日志回查）：
      - ``required`` → 「缺必填字段：<name>」；
      - ``type`` → 「字段类型不匹配：<message>（类型）」；
      - ``enum`` → 「枚举取值不合法：<message>」；
      - ``minLength`` → 「字符串长度不足：<message>」；
      - ``minimum`` / ``maximum`` → 「数值越界：<message>」；
      - 其它 → 「jsonschema <validator>: <message>」。
    """
    validator = err.validator
    raw = err.message
    path = ".".join(str(p) for p in err.path) or "<root>"
    if validator == "required":
        # jsonschema 写法：``"'xxx' is a required property"``
        missing = "?"
        if "'" in raw:
            try:
                missing = raw.split("'")[1]
            except IndexError:
                missing = "?"
        return f"{path}: 缺必填字段：{missing}"
    if validator == "type":
        return f"{path}: 字段类型不匹配：{raw}（类型）"
    if validator == "enum":
        return f"{path}: 枚举取值不合法：{raw}"
    if validator == "minLength":
        return f"{path}: 字符串长度不足：{raw}"
    if validator in {"minimum", "maximum"}:
        return f"{path}: 数值越界：{raw}"
    return f"{path}: jsonschema {validator}: {raw}"


def iter_errors(payload: Any, schema_name: str) -> Iterable[jsonschema.ValidationError]:
    """返回 jsonschema 全部校验错误（迭代器）；用于调试 / 日志。"""
    validator = get_validator(schema_name)
    return validator.iter_errors(payload)


def schema_names() -> tuple[str, ...]:
    """返回已注册 schema 名元组（稳定顺序）。"""
    return tuple(SCHEMAS.keys())


# ---------------------------------------------------------------------------
# 任意 schema dict 校验（用于 chat_structured 的 inline json_schema）
# ---------------------------------------------------------------------------


def validate_against_dict(
    payload: Any, json_schema: Mapping[str, Any]
) -> tuple[bool, str | None]:
    """用 jsonschema 校验 ``payload`` 是否符合**调用方传入**的 schema dict。

    与 :func:`validate_payload` 的区别：
      - :func:`validate_payload` 按 ``schema_name`` 走 :data:`SCHEMAS` 注册表；
      - :func:`validate_against_dict` 接受任意 schema dict，调用方在
        ``chat_structured(json_schema=...)`` 传入的契约就属于此类。

    二者底层都用 Draft-07 Validator 与 ``_localize_error``，错误口径一致。
    """
    try:
        validator = Draft7Validator(dict(json_schema))
    except (jsonschema.SchemaError, TypeError, ValueError) as exc:
        # jsonschema 在构造时遇到自身不合法的 schema 会抛 SchemaError，
        # 内部 _keywords.type 还可能透出 TypeError（如 ``{"type": 1234}``
        # 时 list iteration 失败）。统一兜住，返回错误消息而不是把异常
        # 透传给调用方。
        return False, f"schema 自身不合法：{exc}"
    try:
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    except (jsonschema.SchemaError, TypeError, ValueError) as exc:
        return False, f"schema 内部错误：{exc}"
    if not errors:
        return True, None
    head = errors[0]
    localized = _localize_error(head)
    return False, f"{localized}（共 {len(errors)} 处失败）"


# ---------------------------------------------------------------------------
# 兼容性 helper —— 给 prompts 模块重导出
# ---------------------------------------------------------------------------


def is_valid_payload(payload: Any, schema_name: str) -> bool:
    """``validate_payload`` 的布尔便捷入口（业务层偶有用）。"""
    return validate_payload(payload, schema_name)[0]


__all__ = [
    "SEMANTIC_CHECK_SCHEMA",
    "EXTRACTION_SCHEMA",
    "SCHEMAS",
    "get_schema",
    "get_validator",
    "validate_payload",
    "validate_against_dict",
    "iter_errors",
    "is_valid_payload",
    "schema_names",
]
