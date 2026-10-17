"""SSE 推送测试（Task 9）。

覆盖：

1. 上传 → SSE 至少收到 5 条 step 事件（segmenter / extractor /
   cross_validator / rule_checker / semantic_checker）+ done；
2. 字段事件数 = doc.extracted_fields 字段数（每字段一次 field 事件）；
3. SSE 端点收到 ``subscribed`` 事件作为第 0 条；
4. 不存在的 case_id → 404；

使用 ``httpx.AsyncClient + ASGITransport``（TestClient 的同步 iter 在
某些情况下无法驱动 async 任务调度；异步 client 是更可靠的 SSE 验证方式）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from case_bill_validation.server import PIPELINE_STEP_ORDER, create_app


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def async_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# 解析 SSE 文本 → events 列表
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """极简 SSE 解析：每段以 ``\\n\\n`` 分隔，``data: <json>`` 解析为 dict。

    兼容 ``event: <name>`` / ``id: <id>`` 行；心跳 ``: ping`` 行被忽略。
    """
    events: list[dict[str, Any]] = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        event_name = "message"
        event_id: str | None = None
        data_lines: list[str] = []
        for line in chunk.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("id:"):
                event_id = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload["_event"] = event_name
        if event_id is not None:
            payload["_id"] = event_id
        events.append(payload)
    return events


async def _upload(async_client: AsyncClient, fixtures_dir) -> str:
    sample = fixtures_dir / "discharge_01.txt"
    with sample.open("rb") as f:
        resp = await async_client.post(
            "/api/cases",
            files={"file": ("discharge_01.txt", f, "text/plain")},
        )
    assert resp.status_code == 202, resp.text
    return resp.json()["case_id"]


async def _consume_sse_until_done(
    async_client: AsyncClient, case_id: str, *, max_lines: int = 200, timeout: float = 60.0
) -> list[dict[str, Any]]:
    """打开 SSE 流，消费直到收到 ``done`` 事件，返回 events 列表。"""
    lines: list[str] = []
    deadline = time.time() + timeout
    async with async_client.stream("GET", f"/api/cases/{case_id}/stream") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            lines.append(line)
            if len(lines) > max_lines:
                break
            if '"step": "done"' in "\n".join(lines):
                break
            if time.time() > deadline:
                break
    return _parse_sse("\n".join(lines))


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


async def test_sse_emits_at_least_five_step_events_then_done(async_client, fixtures_dir):
    """SSE 至少收到 5 条 step 事件 + done。"""
    case_id = await _upload(async_client, fixtures_dir)
    events = await _consume_sse_until_done(async_client, case_id)

    step_events = [
        e
        for e in events
        if "step" in e
        and e.get("step") in PIPELINE_STEP_ORDER
    ]
    assert len(step_events) >= 5, (
        f"期望 ≥5 条 step 事件，实际 {len(step_events)}: {step_events}"
    )

    # 最后一条应是 done
    assert events[-1].get("step") == "done", f"最后一条应为 done: {events[-1]}"


async def test_sse_emits_field_events_for_each_extracted_field(async_client, fixtures_dir):
    """字段事件数 ≥ doc.extracted_fields 字段数。"""
    case_id = await _upload(async_client, fixtures_dir)
    events = await _consume_sse_until_done(async_client, case_id)
    field_events = [e for e in events if "field" in e and e.get("status")]

    doc_resp = await async_client.get(f"/api/cases/{case_id}")
    assert doc_resp.status_code == 200
    extracted_count = len(doc_resp.json().get("extracted_fields", {}) or {})

    assert (
        len(field_events) >= extracted_count >= 0
    ), f"field 事件 {len(field_events)} 应 ≥ extracted_fields {extracted_count}"


async def test_sse_first_event_is_subscribed(async_client, fixtures_dir):
    """SSE 第一条事件是 subscribed（避免空队列超时）。"""
    case_id = await _upload(async_client, fixtures_dir)
    events = await _consume_sse_until_done(async_client, case_id)

    assert events, "SSE 至少应有一条事件"
    assert events[0].get("step") == "subscribed", (
        f"首条事件应为 subscribed，实际 {events[0]}"
    )


async def test_sse_unknown_case_id_returns_404(async_client):
    """不存在的 case_id → 404。"""
    resp = await async_client.get("/api/cases/no-such-case/stream")
    assert resp.status_code == 404


async def test_sse_response_has_event_and_data_lines(async_client, fixtures_dir):
    """SSE 响应体含 ``event:`` / ``data:`` 行（标准 SSE 格式）。"""
    case_id = await _upload(async_client, fixtures_dir)

    async with async_client.stream("GET", f"/api/cases/{case_id}/stream") as resp:
        assert resp.status_code == 200
        lines: list[str] = []
        async for line in resp.aiter_lines():
            lines.append(line)
            if '"step": "done"' in "\n".join(lines):
                break
            if len(lines) > 100:
                break

    text = "\n".join(lines)
    assert "event:" in text, "SSE 响应应至少含一行 event:"
    assert "data:" in text, "SSE 响应应至少含一行 data:"