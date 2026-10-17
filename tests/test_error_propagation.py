"""业务异常跨模块传递测试（Task 18）。

与 :mod:`tests.test_errors` / :mod:`tests.test_error_serialization` 互补：
本文件聚焦异常从 **流水线 / 路由 / 解析器** 抛出后到 **HTTP 响应体** 的端到端
传递，重点验证：

1. **解析层失败传播**：上传空文件 / 类型不支持 → ``HTTPException`` 4xx；
2. **状态机非法迁移传播**：``FieldMark.confirm()`` 抛 ``InvalidTransitionError``
   → ``HTTPException`` 409 + 原始异常 message 进入 ``detail``；
3. **流水线抛 CaseBillError → 编排器降级不崩溃**：``pipeline.run(doc)`` 不让
   异常向上传播；``doc.needs_manual=True``；其它 processor 仍可完成；
4. **流水线抛 CaseBillError → SSE error 事件**：后端 ``_run_pipeline_and_publish``
   把异常以 ``step=error`` 事件推到 SSE 队列；前端可通过 ``/api/cases/{id}/stream``
   拿到原始错误信息；
5. **to_dict() 直作 HTTPException detail**：业务顶层 ``CaseBillError.to_dict()``
   序列化为与 ``{"error_code", "http_status", "message", "details", "cause"}``
   完全一致的结构，可被 FastAPI ``HTTPException(detail=...)`` 接受；
6. **错误码 → HTTP 状态码映射**：跨 ``error_codes.py`` 5 个类别抽样，断言
   ``spec.http_status`` 与 ``from_spec`` 落地一致。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from case_bill_validation.document import (
    BillingCaseDocument,
    DocumentState,
    FieldStatus,
)
from case_bill_validation.error_codes import (
    InputErrorCode,
    ModelGatewayErrorCode,
    PersistenceErrorCode,
    PipelineErrorCode,
    WritebackErrorCode,
)
from case_bill_validation.errors import (
    CaseBillError,
    InputError,
    ModelGatewayError,
    PersistenceError,
    PipelineError,
    WritebackError,
)
from case_bill_validation.ingest.parser import ParseResult, TextParser
from case_bill_validation.pipeline.core import (
    BillingValidationPipeline,
    Processor,
)
from case_bill_validation.pipeline.segmenter import SegmenterProcessor
from case_bill_validation.server import create_app


# ---------------------------------------------------------------------------
# 1) 解析层失败 → API 路由 HTTPException 4xx
# ---------------------------------------------------------------------------


class TestParserFailurePropagation:
    """``/api/cases`` POST 解析失败路径。"""

    @pytest.fixture
    def app(self):
        return create_app()

    @pytest.fixture
    async def async_client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    @pytest.mark.asyncio
    async def test_upload_empty_file_returns_http_400(self, async_client: AsyncClient) -> None:
        """空文件 → 400 + 错误消息。"""
        resp = await async_client.post(
            "/api/cases",
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert resp.status_code == 400
        assert "空文件" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_upload_unsupported_content_type_returns_http_4xx(
        self, async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """上传二进制非文本内容且 MIME 未声明 → 走 TextParser 失败 → 400。

        验证目标：解析层异常被路由层捕获并以 HTTPException 4xx 形式返回；
        服务器端把任何解析异常统一归 400，原始 message 进入 ``detail``。
        """
        # 注入一个会抛 InputError 的 parser（patch server 命名空间）
        class _BoomParser(TextParser):
            def parse(self, content: bytes, content_type: str) -> ParseResult:
                raise InputError(
                    "上传文件类型不支持（仅 PDF/Word/纯文本）",
                    error_code=InputErrorCode.CB002.code,
                    http_status=InputErrorCode.CB002.http_status,
                    details={"content_type": content_type, "size": len(content)},
                )

        import case_bill_validation.server as server_mod

        monkeypatch.setattr(server_mod, "get_parser", lambda ct: _BoomParser())

        resp = await async_client.post(
            "/api/cases",
            files={"file": ("foo.txt", b"hello", "application/octet-stream")},
        )
        # server.py 把任何解析异常统一归 400，原始异常 message 透传到 detail
        assert resp.status_code == 400
        body = resp.json()
        assert "文件类型不支持" in body["detail"]

    @pytest.mark.asyncio
    async def test_upload_no_text_returns_http_400(
        self, async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """解析后 raw_text 为空 → 400。"""
        class _EmptyParser(TextParser):
            def parse(self, content: bytes, content_type: str) -> ParseResult:
                return ParseResult(raw_text="   \n\t  ", confidence=1.0, page_count=1)

        import case_bill_validation.server as server_mod

        monkeypatch.setattr(server_mod, "get_parser", lambda ct: _EmptyParser())

        resp = await async_client.post(
            "/api/cases",
            files={"file": ("blank.txt", b"   \n", "text/plain")},
        )
        assert resp.status_code == 400
        assert "无可抽取文本" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2) 状态机非法迁移 → API 路由 HTTPException 409
# ---------------------------------------------------------------------------


class TestStateMachineFailurePropagation:
    """``/api/cases/{id}/fields/{field}/confirm`` 拒绝非法迁移。"""

    @pytest.fixture
    def app(self):
        return create_app()

    @pytest.fixture
    async def async_client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    @pytest.mark.asyncio
    async def test_confirm_field_unknown_case_returns_404(
        self, async_client: AsyncClient
    ) -> None:
        resp = await async_client.post(
            "/api/cases/case-unknown/fields/入院日期/confirm",
            json={"note": ""},
        )
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_confirm_unknown_field_returns_404(
        self, async_client: AsyncClient, fixtures_dir: Any
    ) -> None:
        """上传 → 等流水线完成 → confirm 不存在的字段 → 404。"""
        sample = fixtures_dir / "discharge_01.txt"
        with sample.open("rb") as f:
            upload = await async_client.post(
                "/api/cases",
                files={"file": ("discharge_01.txt", f, "text/plain")},
            )
        assert upload.status_code == 202
        case_id = upload.json()["case_id"]

        # 等流水线跑完
        for _ in range(60):
            r = await async_client.get(f"/api/cases/{case_id}")
            if r.status_code == 200 and set(
                r.json().get("processed_steps", [])
            ) >= {"segmenter", "extractor", "cross_validator", "rule_checker", "semantic_checker"}:
                break
            await asyncio.sleep(0.1)

        resp = await async_client.post(
            f"/api/cases/{case_id}/fields/__no_such_field__/confirm",
            json={"note": ""},
        )
        assert resp.status_code == 404
        assert "__no_such_field__" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_confirm_already_confirmed_returns_409(
        self, async_client: AsyncClient, fixtures_dir: Any
    ) -> None:
        """二次 confirm → 状态机非法迁移（confirmed → confirmed）→ 409。"""
        sample = fixtures_dir / "discharge_01.txt"
        with sample.open("rb") as f:
            upload = await async_client.post(
                "/api/cases",
                files={"file": ("discharge_01.txt", f, "text/plain")},
            )
        assert upload.status_code == 202
        case_id = upload.json()["case_id"]

        # 等流水线跑完 + 找第一个可推进的字段并推到 semantic_checked
        target_field: str | None = None
        for _ in range(60):
            r = await async_client.get(f"/api/cases/{case_id}")
            if r.status_code == 200:
                doc = r.json()
                if set(doc.get("processed_steps", [])) >= {
                    "segmenter", "extractor", "cross_validator",
                    "rule_checker", "semantic_checker",
                }:
                    store = async_client._transport.app.state.case_store  # type: ignore[attr-defined]
                    record = store.get_sync(case_id)
                    assert record is not None
                    for fname, mark in record.doc.extracted_fields.items():
                        # 流水线完成后字段停在 RULE_CHECKED（StubModelGateway 不抛错则推进到 SEMANTIC_CHECKED，
                        # 这里两种情况都覆盖），挑一条推进到 SEMANTIC_CHECKED
                        try:
                            if mark.status == FieldStatus.RULE_CHECKED:
                                mark.mark_semantic_checked()
                            elif mark.status == FieldStatus.SEMANTIC_CHECKED:
                                pass  # 已是目标态
                            else:
                                continue
                        except Exception:  # noqa: BLE001
                            continue
                        target_field = fname
                        break
                    break
            await asyncio.sleep(0.1)

        assert target_field is not None, "未找到可推进字段"

        # 第一次 confirm → 200
        r1 = await async_client.post(
            f"/api/cases/{case_id}/fields/{target_field}/confirm",
            json={"note": "first"},
        )
        assert r1.status_code == 200, r1.text
        # 第二次 confirm → 409
        r2 = await async_client.post(
            f"/api/cases/{case_id}/fields/{target_field}/confirm",
            json={"note": "second"},
        )
        assert r2.status_code == 409
        assert "状态机拒绝" in r2.json()["detail"]


# ---------------------------------------------------------------------------
# 3) 流水线抛 CaseBillError → 编排器降级不崩溃
# ---------------------------------------------------------------------------


class _BoomProcessor(Processor):
    """测试用：抛 CaseBillError 的 processor。"""

    step_name = "boom"
    REQUIRES_DEFAULT: set[str] = set()
    PROVIDES_DEFAULT: set[str] = set()

    def __init__(self, exc_to_raise: BaseException) -> None:
        super().__init__()
        self._exc = exc_to_raise

    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        raise self._exc

    def degradation_policy(
        self, doc: BillingCaseDocument, exc: BaseException
    ) -> BillingCaseDocument:
        """覆盖默认：把异常 message 写到 audit_log 而不静默改业务状态。"""
        from case_bill_validation.document import AuditEntry

        doc.append_audit(
            AuditEntry(
                actor="boom_processor",
                field=None,
                action="degrade_after_casebill_error",
                before_status=None,
                after_status=None,
                note=f"{type(exc).__name__}: {exc}",
            )
        )
        doc.needs_manual = True
        return doc


class TestPipelineGracefulDegradation:
    """流水线抛 ``CaseBillError`` → 编排器降级，``doc`` 不崩溃。"""

    def _make_doc(self) -> BillingCaseDocument:
        return BillingCaseDocument(
            case_id="case-degrade-test",
            raw_text="出院小结样例文本",
        )

    def test_pipeline_continues_after_casebillerror_thrown(self) -> None:
        """抛 InputError 的 processor 之后其它 processor 仍跑完。"""
        from case_bill_validation.pipeline.segmenter import SegmenterProcessor

        doc = self._make_doc()
        pipeline = BillingValidationPipeline(
            processors=[
                SegmenterProcessor(),
                _BoomProcessor(
                    InputError(
                        "字段名不在规范中",
                        error_code=InputErrorCode.CB006.code,
                        details={"field_name": "出院科别"},
                    )
                ),
                # 后续不再有 processor；仅验证 Segmenter 已完成 + BoomProcessor 触发降级
            ],
            strict_dag=False,  # BoomProcessor provides 空集 → 无 DAG 约束
        )

        # 不应抛异常
        result = pipeline.run(doc)

        assert result is doc
        assert doc.needs_manual is True
        assert "boom" in doc.processed_steps
        assert "segmenter" in doc.processed_steps
        # audit_log 至少含 boom_processor 的降级记录
        actors = [entry.actor for entry in doc.audit_log]
        assert "boom_processor" in actors
        # 错误 message 透传到 audit note
        last = doc.audit_log[-1]
        assert "InputError" in (last.note or "")
        assert "字段名不在规范中" in (last.note or "")

    def test_pipeline_degradation_preserves_doc_id_and_case_id(self) -> None:
        """降级后 doc 的 case_id / pipeline_state 不被偷偷推进。"""
        from case_bill_validation.document import AuditEntry

        doc = self._make_doc()
        original_state = doc.pipeline_state
        pipeline = BillingValidationPipeline(
            processors=[
                _BoomProcessor(
                    PipelineError(
                        "依赖图环",
                        error_code=PipelineErrorCode.CB101.code,
                    )
                )
            ],
            strict_dag=False,
        )
        pipeline.run(doc)

        assert doc.case_id == "case-degrade-test"
        assert doc.pipeline_state == original_state
        assert doc.needs_manual is True
        # 流水线的 processed_steps 仍含 boom
        assert "boom" in doc.processed_steps
        # AuditEntry 应至少 1 条
        assert any(
            isinstance(e, AuditEntry) for e in doc.audit_log
        )

    def test_pipeline_does_not_silently_advance_state_machine_on_error(self) -> None:
        """抛 CaseBillError 后流水线不会偷偷把单据从 draft 推到 exported。"""
        doc = self._make_doc()
        assert doc.pipeline_state == DocumentState.DRAFT

        pipeline = BillingValidationPipeline(
            processors=[
                _BoomProcessor(
                    WritebackError(
                        "回写失败",
                        error_code=WritebackErrorCode.CB201.code,
                    )
                )
            ],
            strict_dag=False,
        )
        pipeline.run(doc)

        # 单据状态保持 draft，不被推进
        assert doc.pipeline_state == DocumentState.DRAFT


# ---------------------------------------------------------------------------
# 4) 流水线抛 CaseBillError → SSE error 事件
# ---------------------------------------------------------------------------


class _EscalateProcessor(Processor):
    """测试用：抛 CaseBillError 后 ``degradation_policy`` 写完 audit 再 re-raise。

    ``BillingValidationPipeline.run()`` 仅捕获 ``process()`` 抛出的异常，
    ``degradation_policy()`` 内部若再抛错，会一路传到 ``run()`` 外 →
    ``_run_pipeline_and_publish`` 捕获并推 ``step=error`` SSE 事件。
    """

    step_name = "boom_escalate"
    REQUIRES_DEFAULT: set[str] = set()
    PROVIDES_DEFAULT: set[str] = set()

    def __init__(self, exc_to_raise: BaseException) -> None:
        super().__init__()
        self._exc = exc_to_raise

    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        raise self._exc

    def degradation_policy(
        self, doc: BillingCaseDocument, exc: BaseException
    ) -> BillingCaseDocument:
        """写 audit 后 re-raise，触发 SSE error 事件。"""
        from case_bill_validation.document import AuditEntry

        doc.append_audit(
            AuditEntry(
                actor="escalate_processor",
                field=None,
                action="degrade_then_re_raise",
                before_status=None,
                after_status=None,
                note=f"{type(exc).__name__}: {exc}",
            )
        )
        doc.needs_manual = True
        raise exc


class TestSSEErrorEvent:
    """流水线异常以 ``step=error`` 事件推到 SSE 队列；前端可消费。"""

    @pytest.mark.asyncio
    async def test_pipeline_step_error_event_published_via_sse(self) -> None:
        """注入抛 ``ModelGatewayError`` 的 pipeline → SSE 流出现 ``step=error``。

        验证目标：原始异常 message 进入 SSE 事件 payload，前端可读。
        """
        # 注入一个会抛 ModelGatewayError 的 pipeline（``_EscalateProcessor``
        # 让 ``degradation_policy`` 也抛出 → SSE error 事件触发）
        class _LLMBoomPipeline(BillingValidationPipeline):
            def __init__(self) -> None:  # noqa: D401
                processors = [
                    SegmenterProcessor(),
                    _EscalateProcessor(
                        ModelGatewayError(
                            "ModelGateway 凭据已失效（401）",
                            error_code=ModelGatewayErrorCode.CB301.code,
                            http_status=ModelGatewayErrorCode.CB301.http_status,
                            details={"http_code": 401},
                        )
                    ),
                ]
                super().__init__(processors, strict_dag=False)

        app = create_app(pipeline=_LLMBoomPipeline())

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            upload = await client.post(
                "/api/cases",
                files={"file": ("x.txt", b"test content for pipeline propagation", "text/plain")},
            )
            assert upload.status_code == 202
            case_id = upload.json()["case_id"]

            record = app.state.case_store.get_sync(case_id)
            assert record is not None

            assert record.pipeline_task is not None
            # pipeline_task 是 asyncio.Task；它会因为 degradation_policy 抛出而结束
            try:
                await asyncio.wait_for(
                    asyncio.shield(record.pipeline_task), timeout=10.0
                )
            except (asyncio.TimeoutError, BaseException):  # noqa: BLE001
                pass  # 异常被 server._run_pipeline_and_publish 捕获

            # 消费 queue 中的所有事件
            events: list[dict[str, Any]] = []
            while not record.queue.empty():
                events.append(record.queue.get_nowait())

            error_events = [e for e in events if e.get("step") == "error"]
            assert error_events, f"未发现 step=error 事件；实际事件：{events}"
            err = error_events[-1]
            assert "ModelGateway" in err["error"] or "凭据已失效" in err["error"]
            assert err["case_id"] == case_id


# ---------------------------------------------------------------------------
# 5) to_dict() 直作 HTTPException(detail=...)
# ---------------------------------------------------------------------------


class TestToDictAsHTTPExceptionDetail:
    """``CaseBillError.to_dict()`` 必须能被 FastAPI ``HTTPException(detail=...)`` 接受。"""

    def test_to_dict_passes_through_http_exception_detail(self) -> None:
        """FastAPI 把 detail 序列化为 JSON；to_dict() 顶层字段齐备。"""
        e = InputError(
            "字段值越界（span 不在原文边界内）",
            error_code=InputErrorCode.CB006.code,
            http_status=InputErrorCode.CB006.http_status,
            details={"field": "入院日期", "span_start": 999, "raw_len": 100},
            cause=ValueError("原始 span 解析失败"),
        )
        # FastAPI 的 HTTPException(detail=...) 接受 dict / list / str；
        # 把 to_dict() 当 detail 不会出错。
        exc = HTTPException(status_code=e.http_status, detail=e.to_dict())
        assert exc.status_code == 422
        assert isinstance(exc.detail, dict)
        # FastAPI 序列化 HTTPException 时会再走 json.dumps
        body = json.dumps({"detail": exc.detail}, ensure_ascii=False)
        loaded = json.loads(body)
        assert loaded["detail"]["error_code"] == "CB006"
        assert loaded["detail"]["message"].startswith("字段值越界")
        assert loaded["detail"]["details"]["field"] == "入院日期"

    def test_each_category_to_dict_is_valid_detail(self) -> None:
        """5 个子类逐个验证。"""
        for cls, code, http_status in [
            (InputError, "CB001", 400),
            (PipelineError, "CB101", 500),
            (WritebackError, "CB201", 502),
            (ModelGatewayError, "CB301", 502),
            (PersistenceError, "CB401", 500),
        ]:
            e = cls("msg", error_code=code, http_status=http_status, details={"k": "v"})
            exc = HTTPException(status_code=e.http_status, detail=e.to_dict())
            assert exc.status_code == http_status
            assert exc.detail["error_code"] == code


# ---------------------------------------------------------------------------
# 6) 错误码 → HTTP 状态码一致性（跨 5 类别抽样）
# ---------------------------------------------------------------------------


class TestErrorCodeToHTTPStatusMapping:
    """``spec.http_status`` 与 ``from_spec`` 落地一致；端到端不漂移。"""

    @pytest.mark.parametrize(
        "spec_cls,attr,expected_status",
        [
            (InputErrorCode, "CB001", 400),
            (InputErrorCode, "CB002", 415),
            (InputErrorCode, "CB003", 422),
            (PipelineErrorCode, "CB101", 500),
            (PipelineErrorCode, "CB102", 422),
            (WritebackErrorCode, "CB201", 502),
            (WritebackErrorCode, "CB203", 504),  # 实际: 病案系统回写超时
            (ModelGatewayErrorCode, "CB301", 502),
            (ModelGatewayErrorCode, "CB303", 504),
            (PersistenceErrorCode, "CB401", 500),
            (PersistenceErrorCode, "CB403", 500),  # 实际: 审计 hash 链断点
        ],
    )
    def test_from_spec_http_status_matches_spec(
        self,
        spec_cls: type,
        attr: str,
        expected_status: int,
    ) -> None:
        spec = getattr(spec_cls, attr)
        e = CaseBillError.from_spec(spec)
        assert e.http_status == expected_status == spec.http_status

    @pytest.mark.parametrize(
        "spec_cls,attr,expected_subclass",
        [
            (InputErrorCode, "CB001", InputError),
            (PipelineErrorCode, "CB101", PipelineError),
            (WritebackErrorCode, "CB201", WritebackError),
            (ModelGatewayErrorCode, "CB301", ModelGatewayError),
            (PersistenceErrorCode, "CB401", PersistenceError),
        ],
    )
    def test_from_spec_returns_correct_subclass(
        self,
        spec_cls: type,
        attr: str,
        expected_subclass: type[CaseBillError],
    ) -> None:
        spec = getattr(spec_cls, attr)
        e = CaseBillError.from_spec(spec)
        assert isinstance(e, expected_subclass)
