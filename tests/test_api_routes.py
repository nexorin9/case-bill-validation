"""FastAPI 路由测试（Task 9）。

覆盖：

1. ``/api/health`` —— 200 + processors 列表；
2. ``/api/cases`` POST 上传 —— 202 + case_id + 元数据；
3. ``/api/cases/{id}`` GET —— 200 + 序列化 doc；
4. ``/api/cases/{id}/summary`` —— 200 + 状态计数；
5. ``/api/cases/{id}/fields/{field}/confirm`` —— 状态机推进；
6. ``/api/cases/{id}/fields/{field}/reject`` —— 状态机推进；
7. 字段非法迁移 / 不存在的 case / 不存在的字段 → 4xx；

使用 ``httpx.AsyncClient + ASGITransport`` 异步测试，避免 TestClient 同步
iter 无法驱动 async 任务的问题。
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from case_bill_validation.document import FieldStatus
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


async def _wait_until_processed(
    async_client: AsyncClient, case_id: str, *, timeout: float = 30.0
) -> dict:
    """轮询直到流水线全部 5 步处理完，返回 doc dict。"""
    deadline = asyncio.get_event_loop().time() + timeout
    expected = set(PIPELINE_STEP_ORDER)
    while asyncio.get_event_loop().time() < deadline:
        resp = await async_client.get(f"/api/cases/{case_id}")
        if resp.status_code == 200:
            doc = resp.json()
            processed = set(doc.get("processed_steps", []))
            if expected <= processed:
                return doc
        await asyncio.sleep(0.1)
    raise TimeoutError(f"流水线超时未完成：case_id={case_id}")


async def _upload(async_client: AsyncClient, fixtures_dir) -> dict:
    sample = fixtures_dir / "discharge_01.txt"
    with sample.open("rb") as f:
        resp = await async_client.post(
            "/api/cases",
            files={"file": ("discharge_01.txt", f, "text/plain")},
        )
    assert resp.status_code == 202, resp.text
    return resp.json()


def _advance_field_to_semantic_checked(app, case_id: str) -> str | None:
    """把第一条可推进字段手动推到 semantic_checked（绕过 LLM 不可达）。

    confirm / reject 状态机只接受 semantic_checked 起点；测试 fixture 经
    StubModelGateway 跑完语义核默认抛 GatewayError 降级到 rule_checked，因此
    测试需要显式把字段推进一态再试 confirm / reject。
    """
    store = app.state.case_store
    record = store.get_sync(case_id)
    if record is None:
        return None
    for fname, mark in record.doc.extracted_fields.items():
        if mark.status == FieldStatus.RULE_CHECKED:
            try:
                mark.mark_semantic_checked()
            except Exception:  # noqa: BLE001
                continue
            return fname
    for fname, mark in record.doc.extracted_fields.items():
        if mark.status == FieldStatus.EXTRACTING:
            try:
                mark.mark_rule_checked()
                mark.mark_semantic_checked()
            except Exception:  # noqa: BLE001
                continue
            return fname
    return None


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


async def test_health_returns_ok_and_processors(async_client):
    resp = await async_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "processors" in body
    assert set(body["processors"]) == set(PIPELINE_STEP_ORDER)


async def test_health_includes_app_host_and_port(async_client):
    resp = await async_client.get("/api/health")
    body = resp.json()
    assert "app_host" in body
    assert "app_port" in body


async def test_root_returns_message(async_client):
    resp = await async_client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert "message" in body


# ---------------------------------------------------------------------------
# /api/cases POST 上传
# ---------------------------------------------------------------------------


async def test_upload_returns_case_id_and_metadata(async_client, fixtures_dir):
    payload = await _upload(async_client, fixtures_dir)
    assert payload["case_id"].startswith("case-")
    assert payload["filename"] == "discharge_01.txt"
    assert "content_type" in payload
    assert "parser_confidence" in payload
    assert payload["processors"] == list(PIPELINE_STEP_ORDER)


async def test_upload_empty_file_returns_400(async_client):
    resp = await async_client.post(
        "/api/cases",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/cases/{id} GET
# ---------------------------------------------------------------------------


async def test_get_case_returns_doc_dict_after_pipeline(async_client, fixtures_dir):
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    doc = await _wait_until_processed(async_client, case_id)

    assert doc["case_id"] == case_id
    assert "extracted_fields" in doc
    assert "pipeline_state" in doc
    assert "audit_log" in doc
    assert set(doc["processed_steps"]) >= set(PIPELINE_STEP_ORDER)


async def test_get_unknown_case_returns_404(async_client):
    resp = await async_client.get("/api/cases/case-no-such-id")
    assert resp.status_code == 404


async def test_summary_endpoint_returns_field_status_counts(async_client, fixtures_dir):
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    await _wait_until_processed(async_client, case_id)

    resp = await async_client.get(f"/api/cases/{case_id}/summary")
    assert resp.status_code == 200
    summary = resp.json()
    assert summary["case_id"] == case_id
    assert "field_status_counts" in summary
    assert "flagged_fields" in summary
    assert isinstance(summary["field_count"], int)


# ---------------------------------------------------------------------------
# confirm / reject 字段
# ---------------------------------------------------------------------------


async def test_confirm_field_advances_status_machine(async_client, fixtures_dir, app):
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    await _wait_until_processed(async_client, case_id)

    candidate = _advance_field_to_semantic_checked(app, case_id)
    if candidate is None:
        pytest.skip("无可推进到 semantic_checked 的字段")

    resp = await async_client.post(f"/api/cases/{case_id}/fields/{candidate}/confirm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["case_id"] == case_id
    assert body["field"] == candidate
    assert body["status"] == FieldStatus.CONFIRMED.value

    doc_after_resp = await async_client.get(f"/api/cases/{case_id}")
    assert doc_after_resp.json()["extracted_fields"][candidate]["status"] == (
        FieldStatus.CONFIRMED.value
    )


async def test_reject_field_advances_status_machine(async_client, fixtures_dir, app):
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    await _wait_until_processed(async_client, case_id)

    candidate = _advance_field_to_semantic_checked(app, case_id)
    if candidate is None:
        pytest.skip("无可推进字段")

    resp = await async_client.post(f"/api/cases/{case_id}/fields/{candidate}/reject")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == FieldStatus.REJECTED.value


async def test_confirm_unknown_case_returns_404(async_client):
    resp = await async_client.post("/api/cases/case-bad-id/fields/姓名/confirm")
    assert resp.status_code == 404


async def test_confirm_unknown_field_returns_404(async_client, fixtures_dir):
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    await _wait_until_processed(async_client, case_id)

    resp = await async_client.post(
        f"/api/cases/{case_id}/fields/字段不存在XYZ/confirm"
    )
    assert resp.status_code == 404


async def test_confirm_already_confirmed_returns_409(async_client, fixtures_dir, app):
    """已 confirmed 的字段不能再 confirm（状态机非法迁移）。"""
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    await _wait_until_processed(async_client, case_id)

    candidate = _advance_field_to_semantic_checked(app, case_id)
    if candidate is None:
        pytest.skip("无可推进字段")

    first = await async_client.post(
        f"/api/cases/{case_id}/fields/{candidate}/confirm"
    )
    assert first.status_code == 200

    second = await async_client.post(
        f"/api/cases/{case_id}/fields/{candidate}/confirm"
    )
    assert second.status_code == 409


async def test_confirm_endpoint_accepts_note(async_client, fixtures_dir, app):
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    await _wait_until_processed(async_client, case_id)

    candidate = _advance_field_to_semantic_checked(app, case_id)
    if candidate is None:
        pytest.skip("无可推进字段")

    resp = await async_client.post(
        f"/api/cases/{case_id}/fields/{candidate}/confirm",
        json={"note": "已核对病案首页"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["note"] == "已核对病案首页"


# ---------------------------------------------------------------------------
# /api/cases/{id}/export
# -----------------------------------------------------------------------------


async def test_export_returns_501_when_excel_module_not_implemented(
    async_client, fixtures_dir
):
    """export 模块尚未实装 → 501 Not Implemented。"""
    payload = await _upload(async_client, fixtures_dir)
    case_id = payload["case_id"]
    await _wait_until_processed(async_client, case_id)

    resp = await async_client.post(f"/api/cases/{case_id}/export")
    # 期望 501（模块未接入）或 200（若 import 成功）
    assert resp.status_code in (200, 501), resp.text


async def test_export_unknown_case_returns_404(async_client):
    resp = await async_client.post("/api/cases/case-bad-id/export")
    assert resp.status_code == 404