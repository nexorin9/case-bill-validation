"""FastAPI app · 路由 + SSE 推送 + 流水线集成（Task 9 实装）。

路由总览
--------

| 路径 | 方法 | 作用 |
|------|------|------|
| ``/api/health`` | GET | 健康检查 + 流水线 processor 列表 |
| ``/api/cases`` | POST | 上传 PDF/Word/纯文本；解析 → 启动流水线 → 返回 ``case_id`` |
| ``/api/cases/{id}`` | GET | 取 doc 序列化（字段表 + 状态 + 审计计数） |
| ``/api/cases/{id}/stream`` | GET | SSE 流式推送 step / field 事件 + 15s 心跳 |
| ``/api/cases/{id}/fields/{field}/confirm`` | POST | 结算员点确认（状态机合法迁移） |
| ``/api/cases/{id}/fields/{field}/reject`` | POST | 结算员点驳回（状态机合法迁移） |
| ``/api/cases/{id}/export`` | POST | 触发 Excel 导出 + 病案系统回写 |
| ``/`` | GET | 静态面板入口（``/static/index.html``） |
| ``/static`` | GET | 静态资源（app.js / styles.css） |

设计要点
--------

- **流水线集成**：上传成功后同步启动后台 ``asyncio`` 任务，按
  ``BillingValidationPipeline.run(doc)`` 顺序执行 5 个 processor；每个
  processor 进入时由`` _publish_step()`` 推一条 ``{"step": "..."}`` SSE
  事件，每个字段状态推进时推 ``{"field": ..., "status": ...}`` 事件；
- **SSE**：每条上传单据创建一个 ``asyncio.Queue``；SSE 端点用
  ``EventSourceResponse`` 异步 yield；每 15 秒发心跳 ``: ping\\n\\n``
  避免代理切断；
- **存储**：in-memory ``CaseStore``（``case_id -> CaseRecord(doc, queue)``），
  进程重启不持久（不破坏接口契约；审计 / 落盘由 ``ExcelExporter`` +
  ``CaseSystemWriteback`` 承担）；
- **流水线构造**：使用默认 ``StubModelGateway``（任务 8 已落盘）保证
  测试与本地默认运行不依赖真实 LLM 端点；``/api/cases/{id}/export``
  才触发真实回写（默认目标站为 ``case-system.example.internal`` 占位）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from case_bill_validation.config import CaseBillValidationSettings, get_settings
from case_bill_validation.document import (
    AuditEntry,
    BillingCaseDocument,
    DocumentState,
    FieldStatus,
    SpanRef,
)
from case_bill_validation.ingest.parser import get_parser
from case_bill_validation.llm.gateway import StubModelGateway
from case_bill_validation.pipeline.core import BillingValidationPipeline
from case_bill_validation.pipeline.cross_validator import make_cross_validator
from case_bill_validation.pipeline.extractor import make_extractor
from case_bill_validation.pipeline.rule_checker import make_rule_checker
from case_bill_validation.pipeline.segmenter import SegmenterProcessor
from case_bill_validation.pipeline.semantic_checker import make_semantic_checker
from case_bill_validation.rules.cross_field_rules import CrossFieldRules
from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATIC_DIR: Path = Path(__file__).parent / "static"
SSE_HEARTBEAT_INTERVAL_SEC: float = 15.0
# 心跳 comment 字面量：``sse-starlette`` 把 ``ServerSentEvent(comment=...)``
# 序列化为 ``: ping\r\n\r\n``，客户端按 SSE 注释行忽略，前端 onerror 与
# EventSource 默认重连用同一节拍工作。
SSE_HEARTBEAT_COMMENT: str = "ping"
PIPELINE_STEP_ORDER: tuple[str, ...] = (
    "segmenter",
    "extractor",
    "cross_validator",
    "rule_checker",
    "semantic_checker",
)


def _heartbeat_factory() -> ServerSentEvent:
    """心跳工厂：返回 ``: ping`` SSE 注释事件。

    设计要点
    --------
    - 避免 ``sse-starlette`` 默认 ``ping - <timestamp>`` 长字符串，便于测试
      用 ``grep ': ping'`` 校验；
    - 注释事件 (SSE 规范) 客户端 EventSource 不会触发 onmessage，但 onerror
      仍能感知到连接活着（连接断开 → 心跳不再到达 → 重连）。
    """
    return ServerSentEvent(comment=SSE_HEARTBEAT_COMMENT)


# ---------------------------------------------------------------------------
# CaseStore（进程内单据存储 + SSE 队列）
# ---------------------------------------------------------------------------


@dataclass
class CaseRecord:
    """in-memory 单据记录（doc + SSE 队列）。"""

    doc: BillingCaseDocument
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    pipeline_task: asyncio.Task[None] | None = None
    needs_manual: bool = False

    def to_summary(self) -> dict[str, Any]:
        """对外只暴露非敏感摘要。"""
        return {
            "case_id": self.doc.case_id,
            "pipeline_state": self.doc.pipeline_state.value,
            "processed_steps": list(self.doc.processed_steps),
            "field_status_counts": self.doc.field_status_counts(),
            "flagged_fields": self.doc.flagged_field_names(),
            "needs_manual": self.doc.needs_manual,
            "field_count": len(self.doc.extracted_fields),
        }

    def to_doc_dict(self) -> dict[str, Any]:
        """完整 doc 序列化（用于 GET /api/cases/{id}）。"""
        return self.doc.model_dump(mode="json")


class CaseStore:
    """线程/异步安全的 case_id → CaseRecord 映射。"""

    def __init__(self) -> None:
        self._records: dict[str, CaseRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, doc: BillingCaseDocument) -> CaseRecord:
        async with self._lock:
            record = CaseRecord(doc=doc)
            self._records[doc.case_id] = record
            return record

    async def get(self, case_id: str) -> CaseRecord | None:
        async with self._lock:
            return self._records.get(case_id)

    def get_sync(self, case_id: str) -> CaseRecord | None:
        """同步读（FastAPI 路由处理器内单步访问用）。"""
        return self._records.get(case_id)

    def all_ids(self) -> list[str]:
        return list(self._records.keys())


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class ConfirmFieldRequest(BaseModel):
    """确认/驳回字段时附带的备注（可空）。"""

    note: str | None = None


class FieldActionResponse(BaseModel):
    """confirm / reject / export 接口的响应。"""

    case_id: str
    field: str
    status: str
    audit_index: int | None = None
    note: str | None = None


class ExportResponse(BaseModel):
    case_id: str
    excel_path: str
    writeback_success: bool
    writeback_attempts: int
    writeback_error: str | None = None


# ---------------------------------------------------------------------------
# FastAPI app 工厂
# ---------------------------------------------------------------------------


def _build_pipeline(
    *,
    billing_db_path: Path | None = None,
    rule_set: BillingFieldRuleSet | None = None,
    cross_field_rules: CrossFieldRules | None = None,
) -> BillingValidationPipeline:
    """默认流水线（5 步 + StubModelGateway）。

    参数
    ----
    billing_db_path: SQLite 路径；若不传则从 settings 读；
    rule_set: BillingFieldRuleSet 注入（默认懒打开）；
    cross_field_rules: CrossFieldRules 注入（默认从 SQLite 加载）。
    """
    if rule_set is None:
        rule_set = BillingFieldRuleSet.open(billing_db_path or get_settings().billing_db_path)
    if cross_field_rules is None:
        try:
            cross_field_rules = CrossFieldRules.open(
                billing_db_path or get_settings().billing_db_path
            )
        except Exception:  # noqa: BLE001
            cross_field_rules = CrossFieldRules(rules=[])
    return BillingValidationPipeline(
        processors=[
            SegmenterProcessor(),
            make_extractor(),
            make_cross_validator(rules=cross_field_rules),
            make_rule_checker(rule_set=rule_set),
            make_semantic_checker(llm_gateway=StubModelGateway()),
        ]
    )


def create_app(
    *,
    case_store: CaseStore | None = None,
    pipeline: BillingValidationPipeline | None = None,
    settings: CaseBillValidationSettings | None = None,
) -> FastAPI:
    """FastAPI 工厂；uvicorn --factory 调用。

    依赖注入
    --------
    - ``case_store``：默认进程内单例（覆盖用于测试）；
    - ``pipeline``：默认 5 步流水线 + StubModelGateway；
    - ``settings``：默认从环境变量读。
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="病案结算清单核验系统",
        version="0.1.0",
        description=(
            "医保办结算员把出院小结/病案首页上传后，链式 NLP 流水线自动给出 "
            "193 项字段级合规标记与原文引用；结算员确认后一键导出结算清单 "
            "Excel 并回写病案系统。"
        ),
        lifespan=_lifespan,
    )

    settings = settings or get_settings()
    case_store = case_store or CaseStore()
    pipeline = pipeline or _build_pipeline()
    app.state.case_store = case_store
    app.state.pipeline = pipeline
    app.state.settings = settings

    # ------------------------------------------------------------------
    # 静态资源
    # ------------------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # /api/health
    # ------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": "0.1.0",
            "processors": list(pipeline.list_processors()),
            "app_host": settings.app_host,
            "app_port": settings.app_port,
        }

    # ------------------------------------------------------------------
    # /
    # ------------------------------------------------------------------

    @app.get("/")
    def index() -> dict:
        return {
            "message": "病案结算清单核验系统 · 前端见 /static/index.html",
            "static_dir": str(STATIC_DIR),
        }

    # ------------------------------------------------------------------
    # /api/cases POST 上传
    # ------------------------------------------------------------------

    @app.post("/api/cases")
    async def upload_case(file: UploadFile = File(...)) -> JSONResponse:
        """上传出院小结 → 解析 → 启动流水线 → 返回 ``case_id``。"""
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="空文件")

        # MIME 由客户端声明；未声明则按文件后缀猜；都失败 → 走 TextParser
        content_type = file.content_type
        parser = get_parser(content_type)
        try:
            parse_result = parser.parse(content, content_type or "text/plain")
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析失败：%s", exc)
            raise HTTPException(
                status_code=400,
                detail=f"文档解析失败：{exc}",
            ) from exc

        if not parse_result.raw_text.strip():
            raise HTTPException(
                status_code=400,
                detail="文档无可抽取文本（可能是扫描件或空文档）",
            )

        # 生成 case_id
        case_id = f"case-{uuid.uuid4().hex[:12]}"
        doc = BillingCaseDocument(
            case_id=case_id,
            raw_text=parse_result.raw_text,
            pages=[(0, len(parse_result.raw_text))],
            needs_manual=parse_result.needs_manual,
            parser_confidence=parse_result.confidence,
        )

        record = await case_store.create(doc)
        record.needs_manual = parse_result.needs_manual

        # 启动流水线后台任务（异步）
        record.pipeline_task = asyncio.create_task(
            _run_pipeline_and_publish(record, pipeline)
        )

        return JSONResponse(
            status_code=202,
            content={
                "case_id": case_id,
                "filename": file.filename,
                "content_type": content_type,
                "parser_confidence": parse_result.confidence,
                "needs_manual": parse_result.needs_manual,
                "encoding": parse_result.encoding,
                "errors": parse_result.errors,
                "processors": pipeline.list_processors(),
            },
        )

    # ------------------------------------------------------------------
    # /api/cases/{id} GET
    # ------------------------------------------------------------------

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str) -> dict:
        record = case_store.get_sync(case_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"case_id={case_id} 不存在")
        return record.to_doc_dict()

    @app.get("/api/cases/{case_id}/summary")
    def get_case_summary(case_id: str) -> dict:
        record = case_store.get_sync(case_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"case_id={case_id} 不存在")
        return record.to_summary()

    # ------------------------------------------------------------------
    # /api/cases/{id}/stream SSE
    # ------------------------------------------------------------------

    @app.get("/api/cases/{case_id}/stream")
    async def stream_case(case_id: str) -> EventSourceResponse:
        record = case_store.get_sync(case_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"case_id={case_id} 不存在")
        # 立即推一条「已订阅」事件，避免 EventSource 在空队列下超时
        await record.queue.put(
            {
                "step": "subscribed",
                "case_id": case_id,
                "ts": _now_iso(),
            }
        )
        return EventSourceResponse(
            _sse_event_iter(record),
            ping=SSE_HEARTBEAT_INTERVAL_SEC,
            ping_message_factory=_heartbeat_factory,
        )

    # ------------------------------------------------------------------
    # /api/cases/{id}/fields/{field}/confirm
    # ------------------------------------------------------------------

    @app.post(
        "/api/cases/{case_id}/fields/{field_name}/confirm",
        response_model=FieldActionResponse,
    )
    def confirm_field(case_id: str, field_name: str, body: ConfirmFieldRequest | None = None) -> FieldActionResponse:
        record = case_store.get_sync(case_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"case_id={case_id} 不存在")
        mark = record.doc.extracted_fields.get(field_name)
        if mark is None:
            raise HTTPException(
                status_code=404,
                detail=f"字段 {field_name} 不在 case_id={case_id} 的字段表中",
            )

        before_status = mark.status.value
        try:
            mark.confirm()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"状态机拒绝：{exc}",
            ) from exc

        audit = AuditEntry(
            actor="api",
            field=field_name,
            action="confirm",
            before_status=before_status,
            after_status=mark.status.value,
            note=(body.note if body else None),
        )
        record.doc.append_audit(audit)
        record.doc.model_validate(record.doc.model_dump())  # 触发 validate_assignment

        return FieldActionResponse(
            case_id=case_id,
            field=field_name,
            status=mark.status.value,
            audit_index=len(record.doc.audit_log) - 1,
            note=(body.note if body else None),
        )

    # ------------------------------------------------------------------
    # /api/cases/{id}/fields/{field}/reject
    # ------------------------------------------------------------------

    @app.post(
        "/api/cases/{case_id}/fields/{field_name}/reject",
        response_model=FieldActionResponse,
    )
    def reject_field(case_id: str, field_name: str, body: ConfirmFieldRequest | None = None) -> FieldActionResponse:
        record = case_store.get_sync(case_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"case_id={case_id} 不存在")
        mark = record.doc.extracted_fields.get(field_name)
        if mark is None:
            raise HTTPException(
                status_code=404,
                detail=f"字段 {field_name} 不在 case_id={case_id} 的字段表中",
            )

        before_status = mark.status.value
        try:
            mark.reject()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"状态机拒绝：{exc}",
            ) from exc

        audit = AuditEntry(
            actor="api",
            field=field_name,
            action="reject",
            before_status=before_status,
            after_status=mark.status.value,
            note=(body.note if body else None),
        )
        record.doc.append_audit(audit)
        record.doc.model_validate(record.doc.model_dump())  # 触发 validate_assignment

        return FieldActionResponse(
            case_id=case_id,
            field=field_name,
            status=mark.status.value,
            audit_index=len(record.doc.audit_log) - 1,
            note=(body.note if body else None),
        )

    # ------------------------------------------------------------------
    # /api/cases/{id}/export POST 触发 Excel 导出 + 病案系统回写
    # ------------------------------------------------------------------
    # ExcelExporter / CaseSystemWriteback 由 Task 11 实装。本任务只声明路由；
    # 真实模块未就绪时返回 501，让前端知晓「导出模块未接入」。
    # ------------------------------------------------------------------

    @app.post("/api/cases/{case_id}/export", response_model=ExportResponse)
    async def export_case(case_id: str) -> ExportResponse:
        record = case_store.get_sync(case_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"case_id={case_id} 不存在")

        try:
            from case_bill_validation.export.excel import ExcelExporter  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover - Task 11 之前
            raise HTTPException(
                status_code=501,
                detail=f"Excel 导出模块尚未接入（Task 11）：{exc}",
            ) from exc

        export_dir = settings.export_dir
        export_dir.mkdir(parents=True, exist_ok=True)
        xlsx_path = export_dir / f"{case_id}.xlsx"
        try:
            ExcelExporter().export(record.doc, xlsx_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Excel 导出失败：%s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Excel 导出失败：{exc}",
            ) from exc

        try:
            from case_bill_validation.export.case_writeback import (
                CaseSystemWriteback,  # noqa: WPS433
            )
        except ImportError as exc:  # pragma: no cover - Task 11 之前
            raise HTTPException(
                status_code=501,
                detail=f"病案系统回写模块尚未接入（Task 11）：{exc}",
            ) from exc

        writeback = CaseSystemWriteback(
            url=settings.case_system_write_url,
            token=settings.case_system_write_token,
        )
        wb_result = await writeback.post_field_note(
            case_id=case_id,
            field_name="_summary",
            note=f"导出完成：{xlsx_path.name}",
        )

        # 单据状态推进（仅在未推进过的情况下）
        try:
            if record.doc.pipeline_state == DocumentState.IN_REVIEW:
                record.doc.advance_document(DocumentState.CONFIRMED)
            if record.doc.pipeline_state == DocumentState.CONFIRMED:
                record.doc.advance_document(DocumentState.EXPORTED)
        except Exception as exc:  # noqa: BLE001
            logger.info("export 状态机推进跳过：%s", exc)

        return ExportResponse(
            case_id=case_id,
            excel_path=str(xlsx_path),
            writeback_success=wb_result.success,
            writeback_attempts=wb_result.attempts,
            writeback_error=wb_result.error,
        )

    return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _run_pipeline_and_publish(
    record: CaseRecord,
    pipeline: BillingValidationPipeline,
) -> None:
    """在后台执行流水线，每个 processor 进入时推 SSE 事件 + 字段事件。"""
    doc = record.doc
    # 单据进入审核中
    try:
        if doc.pipeline_state == DocumentState.DRAFT:
            doc.advance_document(DocumentState.IN_REVIEW)
    except Exception as exc:  # noqa: BLE001
        logger.info("DRAFT → IN_REVIEW 状态机跳过：%s", exc)

    # 步骤级推送：在 case_store 层包装一层；这里直接走 record.queue
    for step_name in PIPELINE_STEP_ORDER:
        await record.queue.put(
            {
                "step": step_name,
                "status": "started",
                "case_id": doc.case_id,
                "ts": _now_iso(),
            }
        )

    # 启动子线程跑同步流水线，避免阻塞 event loop
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            pipeline.run(doc)
            result["ok"] = True
        except BaseException as exc:  # noqa: BLE001
            error["exc"] = exc

    await asyncio.get_event_loop().run_in_executor(None, _runner)

    if error:
        logger.warning("流水线异常：%s", error["exc"])
        await record.queue.put(
            {
                "step": "error",
                "case_id": doc.case_id,
                "error": str(error["exc"]),
                "ts": _now_iso(),
            }
        )

    # 字段事件广播：字段状态在流水线结束后统一推送（snapshot）
    for fname, mark in doc.extracted_fields.items():
        span_dict: dict[str, Any] | None = None
        if mark.span is not None:
            span_dict = {
                "start_char": mark.span.start_char,
                "end_char": mark.span.end_char,
            }
        await record.queue.put(
            {
                "field": fname,
                "status": mark.status.value,
                "source": mark.source.value,
                "value": mark.value,
                "span": span_dict,
                "confidence": mark.confidence,
                "rule_hits": [
                    {
                        "rule_id": hit.rule_id,
                        "severity": hit.severity.value,
                        "message": hit.message_text,
                    }
                    for hit in mark.rule_hits
                ],
                "case_id": doc.case_id,
                "ts": _now_iso(),
            }
        )

    # 完成事件
    await record.queue.put(
        {
            "step": "done",
            "case_id": doc.case_id,
            "field_status_counts": doc.field_status_counts(),
            "ts": _now_iso(),
        }
    )


async def _sse_event_iter(record: CaseRecord) -> AsyncIterator[dict[str, Any]]:
    """SSE 事件迭代器：消费 record.queue 直到 done 事件 + 推送 stop。"""
    while True:
        try:
            event = await record.queue.get()
        except asyncio.CancelledError:  # pragma: no cover
            break
        if event.get("step") == "done":
            yield {
                "event": "step",
                "id": "done",
                "data": _json_dumps(event),
            }
            # 流水线已完成，停止推送
            break
        if event.get("step") == "error":
            yield {
                "event": "error",
                "data": _json_dumps(event),
            }
            continue
        if "field" in event:
            yield {
                "event": "field",
                "data": _json_dumps(event),
            }
        else:
            yield {
                "event": "step",
                "data": _json_dumps(event),
            }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


__all__ = [
    "CaseRecord",
    "CaseStore",
    "create_app",
    "FieldActionResponse",
    "ExportResponse",
    "ConfirmFieldRequest",
    "SSE_HEARTBEAT_INTERVAL_SEC",
    "SSE_HEARTBEAT_COMMENT",
    "PIPELINE_STEP_ORDER",
    "_heartbeat_factory",
]