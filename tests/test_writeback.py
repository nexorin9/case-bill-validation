"""CaseSystemWriteback 单元测试（Task 11）。

用 httpx.MockTransport 模拟三类响应（200 / 4xx / 5xx）：
- 200 → 立即成功；
- 4xx → 失败 + 死信；
- 5xx → 全部重试 + 死信；
- 5xx 后再 200 → 中途恢复；
- 模拟 TransportError → 网络错误重试。

覆盖：
- 成功路径：WritebackResult.success=True，attempts=1；
- 5xx 全部失败：死信文件生成，attempts=max_attempts；
- 5xx 中途恢复：成功，attempts < max_attempts；
- 4xx 不重试：立即死信；
- 网络错重试：TransportError 触发 backoff；
- 审计 jsonl 写入；
- post_case_summary 用 extracted_fields 生成摘要。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from case_bill_validation.document import (
    BillingCaseDocument,
    DocumentState,
    FieldMark,
    FieldStatus,
)
from case_bill_validation.export.case_writeback import (
    CaseSystemWriteback,
    WritebackAttempt,
    WritebackResult,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_handler(responses: list[httpx.Response | Exception]):
    """构造 async handler 列表，按顺序消费；最后一条会重复。"""
    state = {"idx": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        idx = state["idx"]
        if idx < len(responses):
            item = responses[idx]
            state["idx"] += 1
        else:
            item = responses[-1]
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def _ok(payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload or {"status": "ok", "remote_id": "remote-001"},
        request=httpx.Request("POST", "http://test/"),
    )


def _server_error(status: int = 500) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": "downstream_unavailable"},
        request=httpx.Request("POST", "http://test/"),
    )


def _client_error(status: int = 400) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": "bad_request", "message": "missing field"},
        request=httpx.Request("POST", "http://test/"),
    )


def _connect_error() -> Exception:
    return httpx.ConnectError("connection refused")


# ---------------------------------------------------------------------------
# 基本构造
# ---------------------------------------------------------------------------


class TestCaseSystemWritebackConstruction:
    def test_construction_requires_url(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            CaseSystemWriteback(
                url="",
                token="t",
                dead_letter_dir=tmp_path / "dl",
                audit_log_path=tmp_path / "audit.jsonl",
            )

    def test_max_attempts_below_one_clamped(self, tmp_path: Path) -> None:
        wb = CaseSystemWriteback(
            url="http://test/",
            token="t",
            max_attempts=0,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        assert wb._max_attempts == 1


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


class TestCaseSystemWritebackSuccess:
    @pytest.mark.asyncio
    async def test_success_on_first_try(self, tmp_path: Path) -> None:
        transport = httpx.MockTransport(_make_handler([_ok()]))
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok-123",
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        result = await wb.post_field_note(
            case_id="case-1",
            field_name="入院日期",
            note="复核通过",
        )
        assert isinstance(result, WritebackResult)
        assert result.success is True
        assert result.attempts == 1
        assert result.status_code == 200
        assert result.remote_id == "remote-001"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_success_mid_retry(self, tmp_path: Path) -> None:
        """500 → 500 → 200 应在 3 次尝试内成功。"""
        transport = httpx.MockTransport(
            _make_handler([_server_error(500), _server_error(500), _ok()])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,  # 测试加速
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        result = await wb.post_field_note(
            case_id="case-1",
            field_name="入院日期",
            note="ok",
        )
        assert result.success is True
        assert result.attempts == 3

    @pytest.mark.asyncio
    async def test_request_body_contains_expected_fields(
        self, tmp_path: Path
    ) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _ok()

        transport = httpx.MockTransport(handler)
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="BearerTok",
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        await wb.post_field_note(
            case_id="case-xyz",
            field_name="诊断ICD",
            note="J18.901 复核通过",
        )
        assert captured, "request 未被记录"
        req = captured[0]
        body = json.loads(req.content.decode("utf-8"))
        assert body["case_id"] == "case-xyz"
        assert body["field_name"] == "诊断ICD"
        assert body["note"] == "J18.901 复核通过"
        # 头部含 Bearer token
        assert req.headers.get("authorization") == "Bearer BearerTok"


# ---------------------------------------------------------------------------
# 重试 + 死信
# ---------------------------------------------------------------------------


class TestCaseSystemWritebackRetryAndDeadLetter:
    @pytest.mark.asyncio
    async def test_5xx_exhausts_retries_then_dead_letters(
        self, tmp_path: Path
    ) -> None:
        dl_dir = tmp_path / "dl"
        audit_path = tmp_path / "audit.jsonl"
        transport = httpx.MockTransport(
            _make_handler([_server_error(500), _server_error(502), _server_error(503)])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=dl_dir,
            audit_log_path=audit_path,
        )
        result = await wb.post_field_note(
            case_id="case-2",
            field_name="手术日期",
            note="待复核",
        )
        assert result.success is False
        assert result.attempts == 3
        assert result.status_code == 503

        # 死信文件存在
        assert dl_dir.exists()
        dl_files = list(dl_dir.glob("case-2-手术日期-*.json"))
        assert len(dl_files) == 1, f"死信文件数={len(dl_files)}"
        dl = json.loads(dl_files[0].read_text(encoding="utf-8"))
        assert dl["case_id"] == "case-2"
        assert dl["field_name"] == "手术日期"
        assert dl["reason"] == "all_retries_exhausted"
        assert len(dl["attempts"]) == 3

        # 审计 jsonl：每次尝试一行 + 收尾 summary 一行（Task 31 深化）
        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 4  # 3 attempts + 1 summary
        records = [json.loads(ln) for ln in lines]
        attempt_records = [r for r in records if r.get("attempt_no", 0) > 0]
        summary_records = [r for r in records if r.get("summary") is True]
        assert len(attempt_records) == 3
        assert len(summary_records) == 1
        assert all(r["success"] is False for r in attempt_records)
        assert all(r["status_code"] in (500, 502, 503) for r in attempt_records)
        summary = summary_records[0]
        assert summary["attempt_no"] == 0
        assert summary["total_attempts"] == 3
        assert summary["status_code"] == 503
        assert summary["success"] is False

    @pytest.mark.asyncio
    async def test_4xx_no_retry_immediate_dead_letter(
        self, tmp_path: Path
    ) -> None:
        dl_dir = tmp_path / "dl"
        transport = httpx.MockTransport(_make_handler([_client_error(400)]))
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=dl_dir,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        result = await wb.post_field_note(
            case_id="case-3",
            field_name="性别",
            note="n/a",
        )
        assert result.success is False
        assert result.attempts == 1, "4xx 不应重试"
        assert result.status_code == 400
        dl_files = list(dl_dir.glob("case-3-性别-*.json"))
        assert len(dl_files) == 1

    @pytest.mark.asyncio
    async def test_429_retryable(self, tmp_path: Path) -> None:
        """429 Too Many Requests 应重试。"""
        transport = httpx.MockTransport(
            _make_handler([
                httpx.Response(429, json={"error": "rate_limited"}, request=httpx.Request("POST", "http://test/")),
                _ok(),
            ])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        result = await wb.post_field_note(
            case_id="case-4",
            field_name="诊断ICD",
            note="J18.901",
        )
        assert result.success is True
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_connect_error_retries_then_fails(
        self, tmp_path: Path
    ) -> None:
        """网络层 ConnectError 视为可重试。"""
        transport = httpx.MockTransport(
            _make_handler([_connect_error(), _connect_error(), _connect_error()])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        result = await wb.post_field_note(
            case_id="case-5",
            field_name="X",
            note="y",
        )
        assert result.success is False
        assert result.attempts == 3
        assert "ConnectError" in (result.error or "")


# ---------------------------------------------------------------------------
# 整单摘要回写
# ---------------------------------------------------------------------------


class TestCaseSystemWritebackCaseSummary:
    @pytest.mark.asyncio
    async def test_post_case_summary_includes_confirmed_count(
        self, tmp_path: Path
    ) -> None:
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return _ok()

        transport = httpx.MockTransport(handler)
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        # 构造 doc
        doc = BillingCaseDocument(case_id="case-sum-1")
        doc.upsert_field(
            FieldMark(field_name="性别", value="男", status=FieldStatus.CONFIRMED)
        )
        doc.upsert_field(
            FieldMark(field_name="诊断ICD", value="J18.901", status=FieldStatus.CONFIRMED)
        )
        doc.upsert_field(
            FieldMark(field_name="手术日期", value="2026-01-15", status=FieldStatus.FLAGGED)
        )
        doc.record_step("extractor")
        result = await wb.post_case_summary(doc, excel_path=tmp_path / "x.xlsx")
        assert result.success is True
        assert captured
        body = json.loads(captured[0].content.decode("utf-8"))
        assert body["case_id"] == "case-sum-1"
        assert body["field_name"] == "_summary"
        assert "confirmed_fields=2/3" in body["note"]
        assert "flagged_fields=手术日期" in body["note"]


# ---------------------------------------------------------------------------
# 环境变量便捷入口
# ---------------------------------------------------------------------------


class TestCaseSystemWritebackFromEnv:
    def test_missing_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CASE_SYSTEM_WRITE_URL", raising=False)
        monkeypatch.delenv("CASE_SYSTEM_WRITE_TOKEN", raising=False)
        from case_bill_validation.export.case_writeback import (
            case_system_writeback_from_env,
        )

        with pytest.raises(ValueError):
            case_system_writeback_from_env()

    def test_from_env_reads_url_and_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CASE_SYSTEM_WRITE_URL", "http://test/api")
        monkeypatch.setenv("CASE_SYSTEM_WRITE_TOKEN", "tok-xyz")
        from case_bill_validation.export.case_writeback import (
            case_system_writeback_from_env,
        )

        wb = case_system_writeback_from_env(
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        assert wb._url == "http://test/api"
        assert wb._token == "tok-xyz"


# ---------------------------------------------------------------------------
# Task 31 深化：指数退避 + 死信结构化 + 审计
# ---------------------------------------------------------------------------


class TestExponentialBackoffDelayPattern:
    """指数退避延迟模式断言（asyncio.sleep monkeypatch）。"""

    @pytest.mark.asyncio
    async def test_backoff_delays_follow_powers_of_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 次 5xx → 退避延迟应为 base * 2^0 / 2^1 = 1s / 2s（第三次后不再 sleep）。"""
        delays: list[float] = []
        real_sleep = asyncio_sleep_capture(monkeypatch, delays)
        try:
            transport = httpx.MockTransport(
                _make_handler([
                    _server_error(500),
                    _server_error(502),
                    _ok(),
                ])
            )
            wb = CaseSystemWriteback(
                url="http://test/api",
                token="tok",
                max_attempts=3,
                backoff_base_seconds=1.0,
                transport=transport,
                dead_letter_dir=tmp_path / "dl",
                audit_log_path=tmp_path / "audit.jsonl",
            )
            result = await wb.post_field_note(
                case_id="case-bk-1",
                field_name="入院日期",
                note="ok",
            )
            assert result.success is True
            assert result.attempts == 3
            # 2 次 sleep（attempt 1 后 → 1s；attempt 2 后 → 2s）
            assert len(delays) == 2, f"退避 sleep 次数={len(delays)}"
            assert delays[0] == 1.0, f"第 1 次退避应为 1s={delays[0]}"
            assert delays[1] == 2.0, f"第 2 次退避应为 2s={delays[1]}"
        finally:
            monkeypatch.undo()

    @pytest.mark.asyncio
    async def test_backoff_no_sleep_on_last_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """最后一次失败后不应再 sleep（避免无意义的等待）。"""
        delays: list[float] = []
        monkeypatch.setattr(asyncio, "sleep", _capture_sleep(delays))
        try:
            transport = httpx.MockTransport(
                _make_handler([
                    _server_error(500),
                    _server_error(500),
                    _server_error(500),
                ])
            )
            wb = CaseSystemWriteback(
                url="http://test/api",
                token="tok",
                max_attempts=3,
                backoff_base_seconds=0.5,
                transport=transport,
                dead_letter_dir=tmp_path / "dl",
                audit_log_path=tmp_path / "audit.jsonl",
            )
            result = await wb.post_field_note(
                case_id="case-bk-2",
                field_name="X",
                note="y",
            )
            assert result.success is False
            # 2 次 sleep（attempt 1 后 → 0.5s；attempt 2 后 → 1.0s；第 3 次不再 sleep）
            assert len(delays) == 2
            assert delays == [0.5, 1.0]
        finally:
            monkeypatch.undo()

    @pytest.mark.asyncio
    async def test_backoff_strictly_increasing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """多次重试时退避延迟严格递增（base * 2^k）。"""
        delays: list[float] = []
        monkeypatch.setattr(asyncio, "sleep", _capture_sleep(delays))
        try:
            transport = httpx.MockTransport(
                _make_handler([
                    httpx.Response(429, json={"error": "rl"}, request=httpx.Request("POST", "http://test/")),
                    httpx.Response(503, json={"error": "x"}, request=httpx.Request("POST", "http://test/")),
                    httpx.Response(500, json={"error": "x"}, request=httpx.Request("POST", "http://test/")),
                    httpx.Response(500, json={"error": "x"}, request=httpx.Request("POST", "http://test/")),
                    _ok(),
                ])
            )
            wb = CaseSystemWriteback(
                url="http://test/api",
                token="tok",
                max_attempts=5,
                backoff_base_seconds=0.1,
                transport=transport,
                dead_letter_dir=tmp_path / "dl",
                audit_log_path=tmp_path / "audit.jsonl",
            )
            await wb.post_field_note(
                case_id="case-bk-3",
                field_name="A",
                note="b",
            )
            assert len(delays) == 4  # 5 次尝试间 4 次 sleep
            # 严格递增：0.1 / 0.2 / 0.4 / 0.8
            assert delays[0] < delays[1] < delays[2] < delays[3]
            assert delays[0] == 0.1
            assert delays[1] == 0.2
            assert delays[2] == 0.4
            assert delays[3] == 0.8
        finally:
            monkeypatch.undo()


class TestDeadLetterStructuredFields:
    """死信文件结构化字段（Task 31：summary 块 + attempts 数组）。"""

    @pytest.mark.asyncio
    async def test_dead_letter_has_summary_block(
        self, tmp_path: Path
    ) -> None:
        dl_dir = tmp_path / "dl"
        transport = httpx.MockTransport(
            _make_handler([_server_error(500), _server_error(502), _server_error(503)])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=dl_dir,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        result = await wb.post_field_note(
            case_id="case-dl-1",
            field_name="手术日期",
            note="待复核",
        )
        assert result.success is False

        dl_files = list(dl_dir.glob("case-dl-1-*.json"))
        assert len(dl_files) == 1
        dl = json.loads(dl_files[0].read_text(encoding="utf-8"))

        # 顶层字段
        assert dl["case_id"] == "case-dl-1"
        assert dl["field_name"] == "手术日期"
        assert dl["reason"] == "all_retries_exhausted"
        assert "dead_lettered_at" in dl
        assert isinstance(dl["attempts"], list) and len(dl["attempts"]) == 3

        # summary 块（Task 31 新增）
        summary = dl["summary"]
        assert summary["success"] is False
        assert summary["total_attempts"] == 3
        assert summary["last_status_code"] == 503
        assert isinstance(summary["total_latency_ms"], (int, float))
        assert summary["total_latency_ms"] >= 0
        assert "first_timestamp" in summary
        assert "last_timestamp" in summary
        assert summary["first_timestamp"] <= summary["last_timestamp"]

    @pytest.mark.asyncio
    async def test_dead_letter_attempts_have_full_snapshot(
        self, tmp_path: Path
    ) -> None:
        """每次 attempts[] 元素必须含请求头/请求体/响应体三件套。"""
        dl_dir = tmp_path / "dl"
        transport = httpx.MockTransport(
            _make_handler([_server_error(500), _ok()])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="BearerSecret",
            max_attempts=2,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=dl_dir,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        # 用 mock 让第 2 次失败以触发死信（直接调用 _dead_letter）
        attempts = [
            WritebackAttempt(
                case_id="case-dl-2",
                field_name="ICU天数",
                note="5",
                timestamp="2026-10-17T07:00:00+00:00",
                actor="settlement-clerk",
                attempt_no=1,
                status_code=500,
                error="downstream_unavailable",
                latency_ms=42.5,
                request_headers={"Authorization": "Bearer BearerSecret", "Content-Type": "application/json"},
                request_body={"case_id": "case-dl-2", "field_name": "ICU天数", "note": "5"},
                response_body={"error": "downstream_unavailable"},
            ),
        ]
        dl_path = wb._dead_letter(attempts)
        assert dl_path.exists()
        dl = json.loads(dl_path.read_text(encoding="utf-8"))
        assert dl["case_id"] == "case-dl-2"
        assert dl["field_name"] == "ICU天数"
        assert len(dl["attempts"]) == 1
        a0 = dl["attempts"][0]
        # 关键字段齐全
        for k in ("case_id", "field_name", "note", "timestamp", "actor", "attempt_no",
                  "status_code", "error", "latency_ms",
                  "request_headers", "request_body", "response_body"):
            assert k in a0, f"缺字段 {k}"
        assert a0["request_headers"]["Authorization"] == "Bearer BearerSecret"
        assert a0["request_body"]["case_id"] == "case-dl-2"
        assert a0["response_body"] == {"error": "downstream_unavailable"}
        assert a0["latency_ms"] == 42.5

    @pytest.mark.asyncio
    async def test_dead_letter_safe_for_unicode_field_names(
        self, tmp_path: Path
    ) -> None:
        """中文字段名 + 中文 note 落盘后 JSON 解析无丢失。"""
        dl_dir = tmp_path / "dl"
        transport = httpx.MockTransport(
            _make_handler([_server_error(500), _server_error(502), _server_error(503)])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=dl_dir,
            audit_log_path=tmp_path / "audit.jsonl",
        )
        await wb.post_field_note(
            case_id="case-dl-3",
            field_name="主要诊断代码",
            note="I50.905 心力衰竭，建议核对",
        )
        dl_files = list(dl_dir.glob("case-dl-3-*.json"))
        assert len(dl_files) == 1
        # 文件名编码安全（中文保留）
        assert "主要诊断代码" in dl_files[0].name
        dl = json.loads(dl_files[0].read_text(encoding="utf-8"))
        assert dl["field_name"] == "主要诊断代码"
        for a in dl["attempts"]:
            assert "主要诊断代码" in a["field_name"]
            assert a["request_body"]["field_name"] == "主要诊断代码"
            assert "心力衰竭" in a["request_body"]["note"]


class TestAuditLogStructuredFields:
    """审计 jsonl 字段（Task 31：每次尝试 + 收尾 summary）。"""

    @pytest.mark.asyncio
    async def test_audit_log_has_summary_row_on_success(
        self, tmp_path: Path
    ) -> None:
        audit_path = tmp_path / "audit.jsonl"
        transport = httpx.MockTransport(_make_handler([_ok()]))
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=audit_path,
        )
        result = await wb.post_field_note(
            case_id="case-au-1",
            field_name="入院日期",
            note="ok",
        )
        assert result.success is True

        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        # 1 次尝试 + 1 个 summary = 2 行
        assert len(lines) == 2
        records = [json.loads(ln) for ln in lines]
        attempt = next(r for r in records if r.get("attempt_no") == 1)
        summary = next(r for r in records if r.get("summary") is True)

        # attempt 行 必填字段齐
        for k in ("timestamp", "case_id", "field_name", "attempt_no",
                  "status_code", "latency_ms", "success"):
            assert k in attempt, f"attempt 缺字段 {k}"
        assert attempt["case_id"] == "case-au-1"
        assert attempt["field_name"] == "入院日期"
        assert attempt["status_code"] == 200
        assert attempt["success"] is True
        assert attempt["latency_ms"] >= 0

        # summary 行 必填字段齐
        for k in ("timestamp", "case_id", "field_name", "attempt_no",
                  "status_code", "latency_ms", "success", "total_attempts"):
            assert k in summary, f"summary 缺字段 {k}"
        assert summary["attempt_no"] == 0
        assert summary["summary"] is True
        assert summary["total_attempts"] == 1
        assert summary["success"] is True
        assert summary["status_code"] == 200
        assert summary["last_error"] is None

    @pytest.mark.asyncio
    async def test_audit_log_attempts_preserve_request_payload(
        self, tmp_path: Path
    ) -> None:
        """每次尝试的审计行必须含请求头与请求体快照（便于事后审计）。"""
        audit_path = tmp_path / "audit.jsonl"
        transport = httpx.MockTransport(
            _make_handler([_server_error(500), _server_error(502), _ok()])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="BearerAudited",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=audit_path,
        )
        result = await wb.post_field_note(
            case_id="case-au-2",
            field_name="手术日期",
            note="2026-01-15",
        )
        assert result.success is True

        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(ln) for ln in lines]
        attempt_records = [r for r in records if r.get("attempt_no", 0) > 0]
        assert len(attempt_records) == 3
        for idx, r in enumerate(attempt_records, start=1):
            assert r["attempt_no"] == idx
            assert "request_headers" in r
            assert "request_body" in r
            assert r["request_headers"]["Authorization"] == "Bearer BearerAudited"
            assert r["request_body"]["case_id"] == "case-au-2"
            assert r["request_body"]["field_name"] == "手术日期"
            assert r["request_body"]["note"] == "2026-01-15"
            assert "timestamp" in r["request_body"]

    @pytest.mark.asyncio
    async def test_audit_summary_total_latency_matches_attempt_sum(
        self, tmp_path: Path
    ) -> None:
        """summary.total_latency_ms 应等于各 attempt.latency_ms 之和（±0.05 容差）。"""
        audit_path = tmp_path / "audit.jsonl"
        transport = httpx.MockTransport(
            _make_handler([_server_error(500), _server_error(502), _server_error(503)])
        )
        wb = CaseSystemWriteback(
            url="http://test/api",
            token="tok",
            max_attempts=3,
            backoff_base_seconds=0.01,
            transport=transport,
            dead_letter_dir=tmp_path / "dl",
            audit_log_path=audit_path,
        )
        await wb.post_field_note(
            case_id="case-au-3",
            field_name="诊断ICD",
            note="J18.901",
        )
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(ln) for ln in lines]
        attempts = [r for r in records if r.get("attempt_no", 0) > 0]
        summary = next(r for r in records if r.get("summary") is True)

        sum_latency = sum(r["latency_ms"] for r in attempts)
        assert abs(summary["latency_ms"] - sum_latency) < 0.05


# ---------------------------------------------------------------------------
# helpers（asyncio.sleep 捕获）
# ---------------------------------------------------------------------------


def _capture_sleep(sink: list[float]):
    """构造 asyncio.sleep 的 monkeypatch 替身：捕获每次调用秒数。"""

    async def fake_sleep(seconds: float) -> None:
        sink.append(float(seconds))

    return fake_sleep


def asyncio_sleep_capture(
    monkeypatch: pytest.MonkeyPatch, sink: list[float]
):
    """monkeypatch.setattr(asyncio, 'sleep', ...) 的便捷封装。"""
    fake = _capture_sleep(sink)
    monkeypatch.setattr(asyncio, "sleep", fake)
    return fake
