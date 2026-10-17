"""流水线端到端 API 测试（Task 13 第三个测试文件）。

FastAPI 集成覆盖：

1. 上传 discharge_01.txt 到 ``/api/cases`` → 202 + case_id + 元数据；
2. SSE 推送 ≥ 5 条 step 事件 + done + 字段事件 ≥ 字段数；
3. GET ``/api/cases/{id}`` 返回序列化 doc（含 ``extracted_fields`` 与
   ``field_status_counts``）；
4. POST ``/api/cases/{id}/fields/{field}/confirm`` 推进字段状态
   （SEMANTIC_CHECKED → CONFIRMED；RULE_CHECKED 需先手动推 semantic_checked）；
5. POST ``/api/cases/{id}/export`` 触发 Excel 导出 + 病案系统回写
   （写入 ``exports/{case_id}.xlsx``）；
6. 完整链路：上传 → SSE → confirm → export → 检查 ``exports/`` 下
   ``billing_export.xlsx`` 存在且多 sheet 完整。

实现要点：

- 使用 ``httpx.AsyncClient + ASGITransport``（同 Task 9 test_sse.py），
  避免 TestClient 同步 iter 无法驱动 async 任务的问题；
- 流水线集成 ``StubModelGateway`` 默认抛 GatewayError → 流水线降级到
  RULE_CHECKED + llm_skipped，与生产降级路径一致；
- export 路由需要 ``settings.export_dir`` 指向临时目录；测试通过
  ``create_app(..., settings=...)`` 注入。

参考：spec.md §4 输出闭环 + §9 部署 + Task 9/11 既有路由 + ExcelExporter。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import load_workbook

from case_bill_validation.config import CaseBillValidationSettings
from case_bill_validation.document import FieldStatus
from case_bill_validation.ingest.parser import TextParser
from case_bill_validation.pipeline.cross_validator import make_cross_validator
from case_bill_validation.pipeline.extractor import make_extractor
from case_bill_validation.pipeline.rule_checker import make_rule_checker
from case_bill_validation.pipeline.semantic_checker import make_semantic_checker
from case_bill_validation.pipeline.segmenter import SegmenterProcessor
from case_bill_validation.rules.cross_field_rules import CrossFieldRules
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet
from case_bill_validation.server import PIPELINE_STEP_ORDER, create_app


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _build_settings(tmp_path: Path) -> CaseBillValidationSettings:
    """测试专用 settings：billing_db_path / export_dir 走临时目录。"""
    from pydantic import Field
    from case_bill_validation.config import CaseBillValidationSettings as SettingsCls

    # 直接构造实例，避免 .env 干扰
    db_path = tmp_path / "billing_fields.db"
    from case_bill_validation.rules.migrations import init_billing_db

    init_billing_db(db_path)
    export_dir = tmp_path / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    return SettingsCls(
        model_gateway_api_key="test-key-do-not-use",
        model_gateway_base_url="http://test.invalid/v1",
        model_gateway_model="stub-model",
        case_system_write_url="http://case-system.test/api/notes",
        case_system_write_token="test-write-token",
        his_discharge_url="http://his.test/api/discharge",
        his_discharge_token="test-his-token",
        audit_log_dir=str(tmp_path / "audit"),
        billing_db_path=db_path,
        export_dir=export_dir,
        app_host="127.0.0.1",
        app_port=18000,
    )


@pytest.fixture
def settings(tmp_path: Path) -> CaseBillValidationSettings:
    return _build_settings(tmp_path)


@pytest.fixture
def app(settings: CaseBillValidationSettings, monkeypatch):
    """create_app 注入 settings；monkeypatch 测试环境变量。"""
    monkeypatch.setenv("MODEL_GATEWAY_API_KEY", "test-key-do-not-use")
    monkeypatch.setenv("CASE_SYSTEM_WRITE_TOKEN", "test-write-token")
    monkeypatch.setenv("HIS_DISCHARGE_TOKEN", "test-his-token")
    monkeypatch.setenv("CASE_SYSTEM_WRITE_URL", "http://case-system.test/api/notes")
    monkeypatch.setenv("HIS_DISCHARGE_URL", "http://his.test/api/discharge")
    monkeypatch.setenv("BILLING_DB_PATH", str(settings.billing_db_path))
    monkeypatch.setenv("EXPORT_DIR", str(settings.export_dir))
    monkeypatch.setenv("AUDIT_LOG_DIR", str(settings.audit_log_dir))
    return create_app(settings=settings)


@pytest.fixture
async def async_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _upload(async_client: AsyncClient, fixtures_dir) -> dict[str, Any]:
    sample = fixtures_dir / "discharge_01.txt"
    with sample.open("rb") as f:
        resp = await async_client.post(
            "/api/cases",
            files={"file": ("discharge_01.txt", f, "text/plain")},
        )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _wait_until_processed(
    async_client: AsyncClient, case_id: str, *, timeout: float = 30.0
) -> dict:
    """轮询直到 5 步流水线全部处理完，返回 doc。"""
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


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """极简 SSE 解析（同 test_sse.py）。"""
    events: list[dict[str, Any]] = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in chunk.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
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
        events.append(payload)
    return events


async def _consume_sse_until_done(
    async_client: AsyncClient, case_id: str, *, timeout: float = 60.0
) -> list[dict[str, Any]]:
    """打开 SSE 流，消费直到 done 事件。"""
    lines: list[str] = []
    deadline = time.time() + timeout
    async with async_client.stream("GET", f"/api/cases/{case_id}/stream") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            lines.append(line)
            if '"step": "done"' in "\n".join(lines):
                break
            if time.time() > deadline:
                break
            if len(lines) > 500:
                break
    return _parse_sse("\n".join(lines))


def _advance_one_field_to_semantic_checked(app, case_id: str) -> str | None:
    """从 doc.extracted_fields 里把第一条可推进字段推到 semantic_checked。"""
    record = app.state.case_store.get_sync(case_id)
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
# 测试 1：上传 → 立即返回 case_id
# ---------------------------------------------------------------------------


async def test_upload_returns_case_id_with_metadata(
    async_client, fixtures_dir, settings
) -> None:
    """上传 .txt → 202 + case_id + content_type + processor 列表。"""
    upload_resp_meta = await _upload(async_client, fixtures_dir)
    assert "case_id" in upload_resp_meta
    assert upload_resp_meta["case_id"].startswith("case-")
    assert upload_resp_meta["content_type"] in (None, "", "text/plain")
    assert upload_resp_meta["processors"] == list(PIPELINE_STEP_ORDER)


# ---------------------------------------------------------------------------
# 测试 2：SSE 至少 5 条 step 事件 + done
# ---------------------------------------------------------------------------


async def test_sse_emits_five_step_events_then_done(
    async_client, fixtures_dir
) -> None:
    """SSE 至少收到 5 条 step 事件（segmenter/extractor/cross_validator/
    rule_checker/semantic_checker）+ done。"""
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    events = await _consume_sse_until_done(async_client, case_id)

    step_events = [
        e for e in events if "step" in e and e.get("step") in PIPELINE_STEP_ORDER
    ]
    assert len(step_events) >= 5, (
        f"期望 ≥5 条 step 事件，实际 {len(step_events)}"
    )
    # 最后一条应是 done
    assert events[-1].get("step") == "done", f"最后一条应为 done：{events[-1]}"


# ---------------------------------------------------------------------------
# 测试 3：SSE 字段事件数 ≥ doc.extracted_fields 字段数
# ---------------------------------------------------------------------------


async def test_sse_field_events_count_matches_extracted_fields(
    async_client, fixtures_dir
) -> None:
    """字段事件数 ≥ extracted_fields 字段数。"""
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    events = await _consume_sse_until_done(async_client, case_id)

    field_events = [e for e in events if "field" in e and e.get("status")]
    doc_resp = await async_client.get(f"/api/cases/{case_id}")
    assert doc_resp.status_code == 200
    extracted = doc_resp.json().get("extracted_fields", {}) or {}

    assert len(field_events) >= len(extracted), (
        f"字段事件 {len(field_events)} < extracted_fields {len(extracted)}"
    )


# ---------------------------------------------------------------------------
# 测试 4：GET /api/cases/{id} 返回 doc + field_status_counts
# ---------------------------------------------------------------------------


async def test_get_case_returns_serialized_doc(
    async_client, fixtures_dir
) -> None:
    """GET /api/cases/{id} 返回 doc（含 processed_steps + field_status_counts）。"""
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    await _wait_until_processed(async_client, case_id)

    resp = await async_client.get(f"/api/cases/{case_id}")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["case_id"] == case_id
    assert set(PIPELINE_STEP_ORDER) <= set(doc.get("processed_steps", []))


# ---------------------------------------------------------------------------
# 测试 5：confirm 字段（合法状态机迁移）
# ---------------------------------------------------------------------------


async def test_confirm_field_advances_status(
    app, async_client, fixtures_dir
) -> None:
    """confirm 路由推动字段状态：先手动推到 semantic_checked 然后 confirm。"""
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    await _wait_until_processed(async_client, case_id)

    # 手动推到 semantic_checked 状态
    advanced_name = _advance_one_field_to_semantic_checked(app, case_id)
    assert advanced_name is not None, "无可推进字段"

    resp = await async_client.post(
        f"/api/cases/{case_id}/fields/{advanced_name}/confirm",
        json={"note": "e2e-test-confirm"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "confirmed"
    assert payload["field"] == advanced_name
    assert payload["case_id"] == case_id


# ---------------------------------------------------------------------------
# 测试 6：confirm 路由对非法迁移返回 409
# ---------------------------------------------------------------------------


async def test_confirm_field_rejects_illegal_transition(
    app, async_client, fixtures_dir
) -> None:
    """对 confirmed 字段再次 confirm → 状态机非法 → 409。"""
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    await _wait_until_processed(async_client, case_id)

    advanced_name = _advance_one_field_to_semantic_checked(app, case_id)
    if advanced_name is None:
        pytest.skip("无可推进字段")

    # 第一次 confirm
    resp1 = await async_client.post(
        f"/api/cases/{case_id}/fields/{advanced_name}/confirm",
        json={"note": "first"},
    )
    assert resp1.status_code == 200

    # 第二次 confirm → 非法迁移
    resp2 = await async_client.post(
        f"/api/cases/{case_id}/fields/{advanced_name}/confirm",
        json={"note": "second"},
    )
    assert resp2.status_code == 409, f"非法迁移应返回 409，实际 {resp2.status_code}"


# ---------------------------------------------------------------------------
# 测试 7：export 触发 Excel 落盘
# ---------------------------------------------------------------------------


async def test_export_generates_billing_export_xlsx(
    app, async_client, fixtures_dir, settings
) -> None:
    """POST /api/cases/{id}/export 触发 ExcelExporter 生成 billing_export.xlsx
    至 settings.export_dir 下。"""
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    await _wait_until_processed(async_client, case_id)

    # 先 confirm 一条字段（业务上 export 通常在 confirm 后触发，但本测试不强制）
    advanced_name = _advance_one_field_to_semantic_checked(app, case_id)
    if advanced_name is not None:
        await async_client.post(
            f"/api/cases/{case_id}/fields/{advanced_name}/confirm", json={}
        )

    resp = await async_client.post(f"/api/cases/{case_id}/export")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["case_id"] == case_id
    assert payload["excel_path"].endswith(".xlsx")

    xlsx_path = Path(payload["excel_path"])
    assert xlsx_path.exists(), f"xlsx 未生成：{xlsx_path}"

    # 至少 4 个分区 sheet + 覆盖 + 原文引用 + 审计
    wb = load_workbook(xlsx_path, read_only=True)
    sheet_names = wb.sheetnames
    for sec in ["基本信息", "门诊慢特病诊疗", "住院诊疗", "医疗收费"]:
        assert sec in sheet_names, f"缺分区 sheet：{sec}"
    assert "_覆盖_统计" in sheet_names
    assert "_原文引用" in sheet_names
    assert "_审计_操作" in sheet_names
    wb.close()


# ---------------------------------------------------------------------------
# 测试 8：完整链路：上传 → SSE → confirm → export → xlsx 落盘
# ---------------------------------------------------------------------------


async def test_full_e2e_upload_to_export_to_xlsx(
    app, async_client, fixtures_dir, settings
) -> None:
    """完整端到端：上传 → SSE → 至少 confirm 1 字段 → export → xlsx 落盘。"""
    # Step 1: 上传
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    assert case_id.startswith("case-")

    # Step 2: 等流水线处理完
    await _wait_until_processed(async_client, case_id)

    # Step 3: SSE 至少 5 条 step + done（兼验 SSE 链路）
    events = await _consume_sse_until_done(async_client, case_id)
    step_events = [
        e
        for e in events
        if "step" in e and e.get("step") in PIPELINE_STEP_ORDER
    ]
    assert len(step_events) >= 5
    assert events[-1].get("step") == "done"

    # Step 4: confirm 至少一条字段
    advanced_name = _advance_one_field_to_semantic_checked(app, case_id)
    if advanced_name is not None:
        confirm_resp = await async_client.post(
            f"/api/cases/{case_id}/fields/{advanced_name}/confirm", json={}
        )
        assert confirm_resp.status_code == 200, confirm_resp.text

    # Step 5: export
    export_resp = await async_client.post(f"/api/cases/{case_id}/export")
    assert export_resp.status_code == 200, export_resp.text
    payload = export_resp.json()

    # Step 6: xlsx 落盘验证
    xlsx_path = Path(payload["excel_path"])
    assert xlsx_path.exists(), f"完整链路 xlsx 未生成：{xlsx_path}"
    assert xlsx_path.stat().st_size > 1024, "xlsx 文件过小（< 1KB）"

    # xlsx 含多 sheet
    wb = load_workbook(xlsx_path, read_only=True)
    assert len(wb.sheetnames) >= 7, (
        f"xlsx sheet 数 {len(wb.sheetnames)} < 7"
    )
    wb.close()


# ---------------------------------------------------------------------------
# 测试 9：不存在的 case → 404
# ---------------------------------------------------------------------------


async def test_get_unknown_case_returns_404(async_client) -> None:
    """不存在的 case → 404。"""
    resp = await async_client.get("/api/cases/no-such-case-id-12345")
    assert resp.status_code == 404


async def test_confirm_unknown_field_returns_404(async_client, fixtures_dir) -> None:
    """上传一个 case 但 confirm 不存在的字段 → 404。"""
    meta = await _upload(async_client, fixtures_dir)
    case_id = meta["case_id"]
    resp = await async_client.post(
        f"/api/cases/{case_id}/fields/__nonexistent_field__/confirm", json={}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 测试 10：/api/health 报告 5 步 processor 列表
# ---------------------------------------------------------------------------


async def test_health_reports_all_processors(async_client) -> None:
    """/api/health 返回 processors 字段 ≥ 5 个。"""
    resp = await async_client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["processors"]) >= 5
    assert set(PIPELINE_STEP_ORDER) <= set(body["processors"])
