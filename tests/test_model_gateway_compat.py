"""ModelGateway 协议兼容性测试（Task 8）。

覆盖 4 类错误响应（401 / 500 / 超时 / 非 JSON）+ 2 类结构化校验失败
（缺必填字段 / 字段类型错）+ 重试 1 次后再失败抛 GatewayError + 审计
元数据完整。

参考：
- ``spec.md`` §6（OpenAI-compatible + JSON Schema + 4 类错误响应）；
- ``plan.md`` §6（ModelGateway 计划：401/超时/结构化校验 → 重试 1 次 →
  降级到 rule_checked + semantic_skipped + audit llm_unavailable_at）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from case_bill_validation.llm.gateway import (
    GatewayError,
    LLMCallMetadata,
    ModelGateway,
)


# ---------------------------------------------------------------------------
# fake transport：拦截 httpx 请求并按测试要求返回
# ---------------------------------------------------------------------------


def _make_transport(handler):
    """构造 httpx.MockTransport；handler(request) → httpx.Response。"""

    async def _call(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.MockTransport(_call)


def _ok_response(payload: dict[str, Any] | str) -> httpx.Response:
    """构造一个 200 OK + 合法 OpenAI chat.completions 响应。"""
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
    """OpenAI-compatible chat.completions + response_format=json_object 形态。"""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _make_gateway(handler, *, max_attempts: int = 2, timeout: int = 5) -> ModelGateway:
    """构造一个挂在 fake transport 上的 ModelGateway。"""
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


def _ok_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "field": {"type": "string"},
            "status": {"type": "string"},
            "reason_text": {"type": "string"},
            "evidence_span": {
                "type": "object",
                "properties": {
                    "start_char": {"type": "integer"},
                    "end_char": {"type": "integer"},
                },
                "required": ["start_char", "end_char"],
            },
        },
        "required": ["field", "status", "reason_text", "evidence_span"],
    }


# ---------------------------------------------------------------------------
# 1. 401 — 鉴权失败（重试 1 次后再失败抛 GatewayError）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_401_raises_gateway_error_after_retry():
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
                case_id="case-001",
                field_name="主要诊断代码",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_ok_schema(),
            )
        # 401 重试 1 次 → 共 2 次调用
        assert call_count["n"] == 2
        # 审计元数据完整
        meta = ei.value.audit_metadata
        assert meta["case_id"] == "case-001"
        assert meta["field_name"] == "主要诊断代码"
        assert meta["http_code"] == 401
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert "error_message" in meta
        assert meta["model"] == "gpt-4o-mini"
        assert meta["base_url"] == "https://stub.example/v1"
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 2. 500 — 服务端错误（重试 1 次后再失败抛 GatewayError）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_500_raises_gateway_error_after_retry():
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
                case_id="case-002",
                field_name="主要手术操作代码",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_ok_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["http_code"] == 500
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 3. 超时（httpx.TimeoutException）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_timeout_raises_gateway_error_after_retry():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # 模拟超时：抛 ReadTimeout
        raise httpx.ReadTimeout("simulated timeout", request=request)

    gw = _make_gateway(handler)
    try:
        with pytest.raises(GatewayError) as ei:
            await gw.chat_structured(
                case_id="case-003",
                field_name="入院日期",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_ok_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["http_code"] is None
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert "latency_ms" in meta
        # 因 TimeoutException 包成 cause
        assert isinstance(ei.value.cause, httpx.ReadTimeout)
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 4. 非 JSON 响应（重试 1 次后再失败抛 GatewayError）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_non_json_raises_gateway_error_after_retry():
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
                case_id="case-004",
                field_name="出院日期",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_ok_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["http_code"] == 200
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert "error_message" in meta
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 5. 结构化校验失败：缺必填字段（重试 1 次后再失败抛 GatewayError）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_schema_missing_required_raises_gateway_error_after_retry():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # 缺 evidence_span 必填字段
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
                case_id="case-005",
                field_name="主要诊断代码",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_ok_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert "缺必填字段" in meta["error_message"] or "evidence_span" in meta["error_message"]
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 6. 结构化校验失败：字段类型错（status 应为 string 实际 integer）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_schema_type_mismatch_raises_gateway_error_after_retry():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        # status 给了 integer
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
                case_id="case-006",
                field_name="主要诊断代码",
                messages=[{"role": "user", "content": "..."}],
                schema_name="semantic_check",
                json_schema=_ok_schema(),
            )
        assert call_count["n"] == 2
        meta = ei.value.audit_metadata
        assert meta["attempts"] == 2
        assert meta["downstream_status"] == "unavailable"
        assert "类型" in meta["error_message"]
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 7. 成功路径（200 + 合法 schema）→ 第一次即返回 + audit metadata.success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_success_returns_raw_and_metadata():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        payload = {
            "field": "主要诊断代码",
            "status": "confirmed",
            "reason_text": "依据：出院小结中已明确记载 ICD-10 A01.01。",
            "evidence_span": {"start_char": 10, "end_char": 20, "page_no": 1},
        }
        return _ok_response(_json_object_payload(payload))

    gw = _make_gateway(handler)
    try:
        result = await gw.chat_structured(
            case_id="case-007",
            field_name="主要诊断代码",
            messages=[{"role": "user", "content": "..."}],
            schema_name="semantic_check",
            json_schema=_ok_schema(),
        )
        assert call_count["n"] == 1
        raw = result["_raw"]
        assert raw["status"] == "confirmed"
        assert raw["field"] == "主要诊断代码"
        meta = result["_metadata"]
        assert meta["case_id"] == "case-007"
        assert meta["http_code"] == 200
        assert meta["downstream_status"] == "success"
        assert meta["attempts"] == 1
        assert meta["prompt_hash"]  # sha256 hex
        assert meta["response_hash"]
        assert meta["latency_ms"] >= 0
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 8. 401 → 第一次失败，第二次 200 → 仍然成功（重试 1 次后成功）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_gateway_401_then_200_returns_success():
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
            "field": "x",
            "status": "confirmed",
            "reason_text": "ok",
            "evidence_span": {"start_char": 0, "end_char": 1},
        }
        return _ok_response(_json_object_payload(payload))

    gw = _make_gateway(handler)
    try:
        result = await gw.chat_structured(
            case_id="case-008",
            field_name="x",
            messages=[{"role": "user", "content": "..."}],
            schema_name="semantic_check",
            json_schema=_ok_schema(),
        )
        assert call_count["n"] == 2
        assert result["_metadata"]["attempts"] == 2
        assert result["_metadata"]["downstream_status"] == "retried_success"
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------
# 9. audit_metadata() 与 to_audit_dict() 一致性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_error_to_audit_dict_includes_error_class():
    gw_err = GatewayError(
        "boom",
        audit_metadata={
            "case_id": "case-009",
            "field_name": "field-x",
            "model": "gpt-4o-mini",
            "http_code": 500,
            "downstream_status": "unavailable",
        },
        cause=ValueError("inner cause"),
    )
    out = gw_err.to_audit_dict()
    assert out["case_id"] == "case-009"
    assert out["error_class"] == "ValueError"
    assert "inner cause" in out["error_message"]


# ---------------------------------------------------------------------------
# 10. StubModelGateway 默认行为（未配置 → 抛 GatewayError）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_gateway_unconfigured_raises_gateway_error():
    from case_bill_validation.llm.gateway import StubModelGateway

    stub = StubModelGateway()
    with pytest.raises(GatewayError) as ei:
        await stub.chat_structured(
            case_id="case-stub",
            field_name="任何字段",
            messages=[{"role": "user", "content": "..."}],
            schema_name="semantic_check",
            json_schema=_ok_schema(),
        )
    assert ei.value.audit_metadata["downstream_status"] == "unavailable"


# ---------------------------------------------------------------------------
# 11. StubModelGateway 配置响应 → 返回成功 payload + call_log 记录
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_gateway_configured_returns_payload_and_logs_call():
    from case_bill_validation.llm.gateway import StubModelGateway

    stub = StubModelGateway(
        responses={
            "主要诊断代码": {
                "field": "主要诊断代码",
                "status": "confirmed",
                "reason_text": "ok",
                "evidence_span": {"start_char": 0, "end_char": 5},
            }
        }
    )
    result = await stub.chat_structured(
        case_id="case-stub-2",
        field_name="主要诊断代码",
        messages=[{"role": "user", "content": "..."}],
        schema_name="semantic_check",
        json_schema=_ok_schema(),
    )
    assert result["_raw"]["status"] == "confirmed"
    assert len(stub.call_log) == 1
    assert stub.call_log[0]["case_id"] == "case-stub-2"
    assert stub.call_log[0]["schema_name"] == "semantic_check"


# ---------------------------------------------------------------------------
# 12. chat_structured_sync 同步入口（CLI / 调试）
# ---------------------------------------------------------------------------


def test_model_gateway_chat_structured_sync_raises_gateway_error():
    """同步入口；保持与异步接口一致的错误语义。"""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            401,
            content=b'{"error":{"message":"nope"}}',
            headers={"content-type": "application/json"},
        )

    transport = _make_transport(handler)
    client = httpx.AsyncClient(timeout=5, transport=transport)
    gw = ModelGateway(
        base_url="https://stub.example/v1",
        api_key="sk-stub",
        model="gpt-4o-mini",
        timeout=5,
        max_attempts=2,
        client=client,
    )
    with pytest.raises(GatewayError):
        gw.chat_structured_sync(
            case_id="case-sync",
            field_name="主要诊断代码",
            messages=[{"role": "user", "content": "..."}],
            schema_name="semantic_check",
            json_schema=_ok_schema(),
        )


# ---------------------------------------------------------------------------
# 13. ModelGateway.from_settings() 走 settings（用 monkeypatch 改 env）
# ---------------------------------------------------------------------------


def test_model_gateway_from_settings_reads_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MODEL_GATEWAY_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "sk-env")
    monkeypatch.setenv("MODEL_GATEWAY_MODEL", "gpt-4o-mini-env")
    monkeypatch.setenv("MODEL_GATEWAY_TIMEOUT", "12")
    # 解除可能的 .env 干扰
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path / "audit"))
    gw = ModelGateway.from_settings()
    assert gw.base_url == "https://env.example/v1"
    assert gw.api_key == "sk-env"
    assert gw.model == "gpt-4o-mini-env"
    assert gw.timeout == 12
