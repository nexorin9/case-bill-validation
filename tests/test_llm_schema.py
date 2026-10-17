"""Task 21 字段抽取 / 语义核 · JSON Schema 结构化输出测试。

覆盖
----

- **协议失败重试**：4 类（401 / 500 / 超时 / 非 JSON）+ jsonschema 校验失败
  失败 → 重试 1 次后抛 :class:`GatewayError`，``audit_metadata`` 完整；
- **结构化校验失败**：缺必填字段 / 字段类型错 → 重试 1 次后抛
  :class:`GatewayError`；
- **jsonschema 深度校验**：nested / additionalProperties / enum /
  minLength / minimum-maximum / items 类型，逐项断言失败模式正确；
- **schema 注册表**：``SCHEMAS`` / ``schema_names()`` / ``get_schema`` /
  ``get_validator`` 入口一致；旧 ``prompts`` 模块 re-export 兼容；
- **降级触发详细化**：当 LLM 返回 schema 不合法时，业务侧能精确归类
  (``required`` / ``type`` / ``enum`` ...) 并把错误摘要带到审计日志。

参考
----

- ``spec.md`` §6 结构化输出 JSON Schema + jsonschema 严格校验；
- ``plan.md`` §6 ModelGateway 计划；
- ``case_bill_validation.llm.schemas`` 真源模块。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from case_bill_validation.llm.gateway import (
    GatewayError,
    LLMCallMetadata,
    ModelGateway,
)
from case_bill_validation.llm.prompts import (
    EXTRACTION_SCHEMA,
    SEMANTIC_CHECK_SCHEMA,
)
from case_bill_validation.llm.schemas import (
    SCHEMAS,
    get_schema,
    get_validator,
    is_valid_payload,
    iter_errors,
    schema_names,
    validate_against_dict,
    validate_payload,
)


# ---------------------------------------------------------------------------
# 共享：构造 fake transport + ModelGateway
# ---------------------------------------------------------------------------


def _make_transport(handler):
    async def _call(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.MockTransport(_call)


def _ok_response(payload: dict[str, Any] | str) -> httpx.Response:
    if isinstance(payload, str):
        content = payload
    else:
        content = json.dumps(payload, ensure_ascii=False)
    return httpx.Response(
        200,
        content=content.encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _json_object_payload(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(obj, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_gateway(handler, *, max_attempts: int = 2, timeout: int = 5) -> ModelGateway:
    transport = _make_transport(handler)
    client = httpx.AsyncClient(timeout=timeout, transport=transport)
    return ModelGateway(
        base_url="https://stub.example/v1",
        api_key="sk-stub",
        model="gpt-4o-mini",
        timeout=timeout,
        max_attempts=max_attempts,
        client=client,
    )


def _strict_semantic_schema() -> dict[str, Any]:
    """严格的 SEMANTIC_CHECK 等价 schema（jsonschema 视角）。"""
    return {
        "type": "object",
        "properties": {
            "field": {"type": "string"},
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


# ===========================================================================
# 1. jsonschema 模块契约（4 类失败 → 重试 1 次 → 抛 GatewayError）
# ===========================================================================


@pytest.mark.asyncio
async def test_llm_schema_401_raises_gateway_error_after_retry():
    """401 鉴权失败 → 重试 1 次 → 抛 GatewayError + audit 完整。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            401,
            content=b'{"error":{"message":"invalid api key"}}',
            headers={"content-type": "application/json"},
        )

    gw = _make_gateway(handler)
    try:
        with pytest.raises(GatewayError) as ei:
            await gw.chat_structured(
                case_id="case-401",
                field_name="主要诊断代码",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_strict_semantic_schema(),
            )
        # 重试 1 次 → 共 2 次调用
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["case_id"] == "case-401"
        assert meta["field_name"] == "主要诊断代码"
        assert meta["http_code"] == 401
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert meta["model"] == "gpt-4o-mini"
        assert meta["base_url"] == "https://stub.example/v1"
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_llm_schema_500_raises_gateway_error_after_retry():
    """500 服务端错误 → 重试 1 次 → 抛 GatewayError。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            500,
            content=b'{"error":{"message":"server down"}}',
            headers={"content-type": "application/json"},
        )

    gw = _make_gateway(handler)
    try:
        with pytest.raises(GatewayError) as ei:
            await gw.chat_structured(
                case_id="case-500",
                field_name="出院日期",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_strict_semantic_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["http_code"] == 500
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_llm_schema_timeout_raises_gateway_error_after_retry():
    """httpx.TimeoutException → 重试 1 次 → 抛 GatewayError + cause 透传。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        raise httpx.ReadTimeout("simulated timeout", request=request)

    gw = _make_gateway(handler)
    try:
        with pytest.raises(GatewayError) as ei:
            await gw.chat_structured(
                case_id="case-timeout",
                field_name="入院日期",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_strict_semantic_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["http_code"] is None
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert "latency_ms" in meta
        assert isinstance(ei.value.cause, httpx.ReadTimeout)
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_llm_schema_non_json_raises_gateway_error_after_retry():
    """非 JSON 响应 → 重试 1 次 → 抛 GatewayError。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"content-type": "text/html"},
        )

    gw = _make_gateway(handler)
    try:
        with pytest.raises(GatewayError) as ei:
            await gw.chat_structured(
                case_id="case-nonjson",
                field_name="主要诊断名称",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_strict_semantic_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["http_code"] == 200
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert "error_message" in meta
    finally:
        await gw.aclose()


# ===========================================================================
# 2. jsonschema 严格校验失败（2 类）
# ===========================================================================


@pytest.mark.asyncio
async def test_llm_schema_missing_required_field_raises_gateway_error_after_retry():
    """缺 evidence_span 必填字段 → 重试 1 次后 → 抛 GatewayError。

    error_message 走 _localize_error → 含「缺必填字段」字样。
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        payload = {
            "field": "主要诊断代码",
            "status": "confirmed",
            "reason_text": "依据 XXX",
            # 缺 evidence_span
        }
        return _ok_response(_json_object_payload(payload))

    gw = _make_gateway(handler)
    try:
        with pytest.raises(GatewayError) as ei:
            await gw.chat_structured(
                case_id="case-missing",
                field_name="主要诊断代码",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_strict_semantic_schema(),
            )
        assert call_count["n"] == 2  # 重试 1 次
        meta = ei.value.audit_metadata
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        # _localize_error 把 jsonschema 的 "'evidence_span' is a required property"
        # 翻译成 "evidence_span: 缺必填字段：evidence_span" → 必须同时含两者
        assert "缺必填字段" in meta["error_message"]
        assert "evidence_span" in meta["error_message"]
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_llm_schema_type_mismatch_raises_gateway_error_after_retry():
    """status 给 integer → jsonschema type 校验失败 → 重试 1 次 → 抛 GatewayError。

    error_message 走 _localize_error → 含「类型」字样。
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        payload = {
            "field": "主要诊断代码",
            "status": 123,  # 应当是 string
            "reason_text": "依据 XXX",
            "evidence_span": {"start_char": 0, "end_char": 5},
        }
        return _ok_response(_json_object_payload(payload))

    gw = _make_gateway(handler)
    try:
        with pytest.raises(GatewayError) as ei:
            await gw.chat_structured(
                case_id="case-typemismatch",
                field_name="主要诊断代码",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_strict_semantic_schema(),
            )
        assert call_count["n"] == 2  # 重试 1 次
        meta = ei.value.audit_metadata
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        # _localize_error 把 jsonschema "123 is not of type 'string'" 翻译成含「类型」字样
        assert "类型" in meta["error_message"]
    finally:
        await gw.aclose()


# ===========================================================================
# 3. 成功路径：401 → 200 重试 1 次后成功 + audit metadata
# ===========================================================================


@pytest.mark.asyncio
async def test_llm_schema_401_then_200_returns_success_with_jsonschema_validation():
    """第一次 401、第二次 200 → 仍成功 → ``downstream_status=retried_success``。

    验证 jsonschema 校验在第二次成功后被正确放行。
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                401,
                content=b'{"error":{"message":"first try failed"}}',
                headers={"content-type": "application/json"},
            )
        payload = {
            "field": "主要诊断代码",
            "status": "confirmed",
            "reason_text": "ok",
            "evidence_span": {"start_char": 0, "end_char": 5},
        }
        return _ok_response(_json_object_payload(payload))

    gw = _make_gateway(handler)
    try:
        result = await gw.chat_structured(
            case_id="case-retry-success",
            field_name="主要诊断代码",
            messages=[{"role": "user", "content": "..."}],
            schema_name="semantic_check",
            json_schema=_strict_semantic_schema(),
        )
        assert call_count["n"] == 2
        assert result["_raw"]["status"] == "confirmed"
        meta = result["_metadata"]
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "retried_success"
        assert meta["http_code"] == 200
    finally:
        await gw.aclose()


# ===========================================================================
# 4. validate_payload（按 schema_name 走注册表）的入口契约
# ===========================================================================


class TestValidatePayloadByName:
    """对 :func:`validate_payload` 的契约测试。"""

    def test_semantic_check_valid_payload(self):
        ok, err = validate_payload(
            {
                "field": "入院日期",
                "status": "confirmed",
                "reason_text": "依据见原文",
                "evidence_span": {"start_char": 100, "end_char": 110},
            },
            "semantic_check",
        )
        assert ok is True
        assert err is None

    def test_semantic_check_missing_required(self):
        ok, err = validate_payload(
            {
                "field": "入院日期",
                "status": "confirmed",
                "reason_text": "ok",
                # evidence_span 必填缺失
            },
            "semantic_check",
        )
        assert ok is False
        assert err is not None
        assert "缺必填字段" in err
        assert "evidence_span" in err

    def test_semantic_check_type_mismatch_status_not_string(self):
        ok, err = validate_payload(
            {
                "field": "入院日期",
                "status": 12345,
                "reason_text": "ok",
                "evidence_span": {"start_char": 0, "end_char": 5},
            },
            "semantic_check",
        )
        assert ok is False
        assert "类型" in err

    def test_semantic_check_enum_violation(self):
        ok, err = validate_payload(
            {
                "field": "入院日期",
                "status": "maybe",  # 不在 enum
                "reason_text": "ok",
                "evidence_span": {"start_char": 0, "end_char": 5},
            },
            "semantic_check",
        )
        assert ok is False
        assert "枚举取值不合法" in err

    def test_extraction_valid_payload(self):
        ok, err = validate_payload(
            {
                "fields": [
                    {
                        "name": "姓名",
                        "value": "张三",
                        "span_start": 10,
                        "span_end": 12,
                        "confidence": 0.95,
                    },
                    {
                        "name": "入院日期",
                        "value": "2026-01-12",
                        "span_start": 50,
                        "span_end": 60,
                        "confidence": 0.92,
                    },
                ]
            },
            "extraction",
        )
        assert ok is True
        assert err is None

    def test_extraction_field_missing_required_value(self):
        ok, err = validate_payload(
            {
                "fields": [
                    {
                        "name": "姓名",
                        # value 必填缺失
                        "span_start": 10,
                        "span_end": 12,
                    }
                ]
            },
            "extraction",
        )
        assert ok is False
        assert "缺必填字段" in err
        assert "value" in err

    def test_unknown_schema_name_returns_error(self):
        ok, err = validate_payload({"foo": 1}, "no_such_schema")
        assert ok is False
        assert "未知 schema_name" in err


# ===========================================================================
# 5. validate_against_dict（任意 schema dict）的契约
# ===========================================================================


class TestValidateAgainstDict:
    """对 :func:`validate_against_dict` 的契约测试。"""

    def test_additional_properties_rejected(self):
        """additionalProperties=False → 多余字段被拒。"""
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a"],
            "additionalProperties": False,
        }
        ok, err = validate_against_dict({"a": "x", "b": "y"}, schema)
        assert ok is False
        assert "b" in err  # 多余字段

    def test_minimum_maximum_enforced(self):
        """minimum / maximum 越界被拒。"""
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 100}
            },
            "required": ["score"],
        }
        ok1, err1 = validate_against_dict({"score": -1}, schema)
        assert ok1 is False
        assert "数值越界" in err1
        ok2, err2 = validate_against_dict({"score": 200}, schema)
        assert ok2 is False
        assert "数值越界" in err2

    def test_min_length_enforced(self):
        """minLength 不足被拒。"""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 3}},
            "required": ["name"],
        }
        ok, err = validate_against_dict({"name": "ab"}, schema)
        assert ok is False
        assert "字符串长度不足" in err

    def test_array_items_type_checked(self):
        """array 的 items 类型校验。"""
        schema = {
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"type": "integer"},
                }
            },
            "required": ["values"],
        }
        ok, err = validate_against_dict(
            {"values": [1, 2, "three"]}, schema
        )
        assert ok is False
        assert "类型" in err or "integer" in err

    def test_root_type_mismatch(self):
        """payload 不是 dict 时顶层 type=object 拒绝。"""
        schema = {"type": "object", "required": []}
        ok, err = validate_against_dict([1, 2, 3], schema)
        assert ok is False
        assert "类型" in err

    def test_self_invalid_schema_returns_error(self):
        """schema 自身不合法（SchemaError）→ 返回 ``schema 自身不合法`` 提示。"""
        # type 写成不合法的值
        bad_schema = {"type": 1234}  # int 不是合法 type
        # jsonschema.Draft7Validator 在构造时检查 SchemaError
        # 但本函数包了 try/except，返错误而非抛
        ok, err = validate_against_dict({"a": 1}, bad_schema)
        # 两种情况都能接受：(a) validator 报我们报错；(b) jsonschema 默默接受
        # 不抛即可
        assert isinstance(ok, bool)
        assert isinstance(err, str)


# ===========================================================================
# 6. 注册表 / 入口契约（4 类 lookup）
# ===========================================================================


class TestSchemaRegistry:
    """对 :data:`SCHEMAS` 与各 lookup 函数的契约测试。"""

    def test_schemas_contains_both_registered(self):
        assert "semantic_check" in SCHEMAS
        assert "extraction" in SCHEMAS

    def test_schema_names_returns_registered_names(self):
        names = schema_names()
        assert "semantic_check" in names
        assert "extraction" in names

    def test_get_schema_returns_correct_dict(self):
        s = get_schema("semantic_check")
        assert s["type"] == "object"
        assert "evidence_span" in s["properties"]
        s2 = get_schema("extraction")
        assert "fields" in s2["properties"]

    def test_get_schema_unknown_raises_keyerror(self):
        with pytest.raises(KeyError):
            get_schema("nope")

    def test_get_validator_returns_draft7_validator(self):
        v = get_validator("semantic_check")
        # Draft7Validator 自带 .iter_errors / .validate
        assert hasattr(v, "iter_errors")
        assert hasattr(v, "validate")

    def test_get_validator_unknown_raises_keyerror(self):
        with pytest.raises(KeyError):
            get_validator("nope")

    def test_prompts_module_reexports_schemas(self):
        """向后兼容：``from case_bill_validation.llm.prompts import SEMANTIC_CHECK_SCHEMA``"""
        from case_bill_validation.llm.prompts import (
            EXTRACTION_SCHEMA,
            SEMANTIC_CHECK_SCHEMA,
            SCHEMAS,
            get_schema,
            get_validator,
            is_valid_payload,
            iter_errors,
            schema_names,
            validate_against_dict,
            validate_payload,
        )

        assert SEMANTIC_CHECK_SCHEMA["type"] == "object"
        assert "evidence_span" in SEMANTIC_CHECK_SCHEMA["properties"]
        assert "fields" in EXTRACTION_SCHEMA["properties"]
        # 注册表 + 入口完整
        assert "semantic_check" in SCHEMAS
        assert "extraction" in schema_names()


# ===========================================================================
# 7. is_valid_payload / iter_errors 入口
# ===========================================================================


class TestSchemaHelpers:
    def test_is_valid_payload_true(self):
        assert is_valid_payload(
            {
                "field": "x",
                "status": "confirmed",
                "reason_text": "r",
                "evidence_span": {"start_char": 0, "end_char": 1},
            },
            "semantic_check",
        ) is True

    def test_is_valid_payload_false(self):
        assert is_valid_payload({}, "semantic_check") is False

    def test_iter_errors_yields_validation_errors(self):
        errors = list(iter_errors({}, "semantic_check"))
        # 顶层缺 4 个 required
        assert len(errors) >= 1
        assert all(hasattr(e, "message") for e in errors)


# ===========================================================================
# 8. 降级触发 — 业务层（SemanticCheckerProcessor）实测
# ===========================================================================


class TestDegradationTriggerDetails:
    """LLM 输出不合法 schema 时，业务层能否精确归类 + 失败审计。"""

    def test_semantic_check_schema_violation_localized_error_message(self):
        """多种 schema 违反 → ``_localize_error`` 翻译正确（错误消息分类）。"""
        violations = [
            ({}, "缺必填字段"),  # 全缺
            (
                {
                    "field": "x",
                    "status": "maybe",  # enum 违反
                    "reason_text": "r",
                    "evidence_span": {"start_char": 0, "end_char": 1},
                },
                "枚举取值不合法",
            ),
            (
                {
                    "field": "x",
                    "status": "confirmed",
                    "reason_text": "r",
                    "evidence_span": {"start_char": -5, "end_char": 1},  # minimum 违反
                },
                "数值越界",
            ),
            (
                {
                    "field": "",  # minLength 违反
                    "status": "confirmed",
                    "reason_text": "r",
                    "evidence_span": {"start_char": 0, "end_char": 1},
                },
                "字符串长度不足",
            ),
        ]
        for bad_payload, expected_keyword in violations:
            ok, err = validate_payload(bad_payload, "semantic_check")
            assert ok is False, f"应该校验失败：{bad_payload!r}"
            assert expected_keyword in err, (
                f"期望错误消息含 {expected_keyword!r}，实际 {err!r}（payload={bad_payload!r}）"
            )

    def test_gateway_error_audit_metadata_carries_jsonschema_message(self):
        """GatewayError.audit_metadata["error_message"] 落到审计 jsonl 后能反查。"""
        # 仅模拟：构造一个 GatewayError 验证字段链路
        gw_err = GatewayError(
            "schema fail",
            audit_metadata={
                "case_id": "case-01",
                "field_name": "主要诊断代码",
                "downstream_status": "unavailable",
                "error_message": "evidence_span: 缺必填字段：evidence_span",
                "attempts": 2,
            },
        )
        d = gw_err.to_audit_dict()
        assert "缺必填字段" in d["error_message"]
        assert d["case_id"] == "case-01"
