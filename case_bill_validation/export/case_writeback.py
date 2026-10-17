"""病案系统字段备注回写（Task 11 + Task 31 深化）。

设计要点
--------

- **协议**：HTTP POST JSON。请求体::

    {
      "case_id": "case-abc123",
      "field_name": "入院日期" 或 "_summary",
      "note": "字段备注/审查结论",
      "timestamp": "2026-10-17T07:30:00Z",
      "actor": "settlement-clerk"
    }

  响应体期望::

    {"status": "ok" | "error", "remote_id": "...", "error": "..."}

- **凭据**：``Bearer`` Token 从环境变量 ``CASE_SYSTEM_WRITE_TOKEN``
  注入；构造时显式传，**不** 读全局配置（便于测试）；
- **重试**：默认 3 次；4xx（除 408 / 429）与 5xx 都重试；
  408 / 429 / 5xx 用**指数退避**（默认 1s / 2s / 4s，
  公式 ``backoff_base * 2 ** (attempt_no - 1)``）；全部失败 →
  死信目录 ``logs/case_system_dead/{case_id}-{field_name}-{ts}.json``；
- **审计**：每次尝试（含失败）+ 收尾 summary 写到
  ``logs/audit/writebacks.jsonl``（Task 31 抽出到
  ``persistence.writeback_audit`` 模块，线程安全）；
- **死信结构**：顶层 ``summary`` + ``attempts`` 数组（每次尝试
  完整 snapshot：request_headers / request_body / response_body /
  latency_ms / status_code / error），便于下游复原失败链路；
- **测试替身**：通过 ``transport`` 注入 ``httpx.MockTransport`` 或
  自定义 async callable；生产环境默认 ``httpx.AsyncClient``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

import httpx

from case_bill_validation.document import BillingCaseDocument, DocumentState
from case_bill_validation.persistence import writeback_audit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 错误 & 数据类
# ---------------------------------------------------------------------------


class WritebackError(RuntimeError):
    """病案系统回写失败（重试全部用完后抛出）。"""


@dataclass
class WritebackResult:
    """单次回写结果。"""

    success: bool
    attempts: int = 0
    error: str | None = None
    status_code: int | None = None
    remote_id: str | None = None
    latency_ms: float = 0.0


@dataclass
class WritebackAttempt:
    """一次尝试的快照（用于审计 + 死信）。"""

    case_id: str
    field_name: str
    note: str
    timestamp: str
    actor: str
    attempt_no: int
    status_code: int | None
    error: str | None
    latency_ms: float
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: dict[str, Any] = field(default_factory=dict)
    response_body: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class CaseSystemWriteback:
    """病案系统字段备注回写客户端。

    参数
    ----
    url: 回写端点完整 URL；
    token: Bearer Token（从 ``CASE_SYSTEM_WRITE_TOKEN`` 环境变量读）；
    max_attempts: 总尝试次数（默认 3，含首次）；
    backoff_base_seconds: 指数退避基础秒数（默认 1.0 → 1s/2s/4s）；
    dead_letter_dir: 死信目录（默认 ``logs/case_system_dead``）；
    audit_log_path: 审计 jsonl 路径（默认 ``logs/audit/writebacks.jsonl``）；
    transport: 自定义 httpx transport，便于测试替身；
    actor: 操作者标识（默认 ``settlement-clerk``）。
    """

    RETRYABLE_5XX: tuple[int, ...] = tuple(range(500, 600))
    RETRYABLE_4XX: tuple[int, ...] = (408, 425, 429)
    NON_RETRYABLE_4XX: tuple[int, ...] = tuple(
        code for code in range(400, 500) if code not in (408, 425, 429)
    )

    def __init__(
        self,
        *,
        url: str,
        token: str,
        max_attempts: int = 3,
        backoff_base_seconds: float = 1.0,
        dead_letter_dir: Path | None = None,
        audit_log_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        actor: str = "settlement-clerk",
    ) -> None:
        if not url:
            raise ValueError("url 不能为空")
        self._url = url
        self._token = token or ""
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = max(0.1, backoff_base_seconds)
        self._dead_letter_dir = dead_letter_dir or Path("logs/case_system_dead")
        self._audit_log_path = audit_log_path or Path("logs/audit/writebacks.jsonl")
        self._transport = transport
        self._actor = actor

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def post_field_note(
        self,
        *,
        case_id: str,
        field_name: str,
        note: str,
        actor: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> WritebackResult:
        """回写一条字段备注。失败时（含 4xx/5xx/超时/网络错）自动重试。"""
        body = {
            "case_id": case_id,
            "field_name": field_name,
            "note": note,
            "timestamp": _now_iso(),
            "actor": actor or self._actor,
        }
        if extra:
            body.update(dict(extra))

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        attempts: list[WritebackAttempt] = []
        last_error: str | None = None
        last_status: int | None = None
        total_latency = 0.0

        for attempt_no in range(1, self._max_attempts + 1):
            attempt_start = _now()
            req_headers = dict(headers)
            try:
                resp = await self._send(headers=req_headers, body=body)
            except (httpx.TimeoutException, httpx.TransportError, httpx.RequestError) as exc:
                latency = (_now() - attempt_start) * 1000.0
                total_latency += latency
                last_error = f"{type(exc).__name__}: {exc}"
                attempts.append(
                    WritebackAttempt(
                        case_id=case_id,
                        field_name=field_name,
                        note=note,
                        timestamp=_now_iso(),
                        actor=actor or self._actor,
                        attempt_no=attempt_no,
                        status_code=None,
                        error=last_error,
                        latency_ms=latency,
                        request_headers=req_headers,
                        request_body=body,
                        response_body=None,
                    )
                )
                await self._maybe_sleep(attempt_no, retryable=True)
                continue

            latency = (_now() - attempt_start) * 1000.0
            total_latency += latency
            status = resp.status_code
            last_status = status
            try:
                payload = resp.json()
            except json.JSONDecodeError:
                payload = {"raw": resp.text[:500]}

            ok = 200 <= status < 300
            retryable = self._is_retryable(status)
            attempts.append(
                WritebackAttempt(
                    case_id=case_id,
                    field_name=field_name,
                    note=note,
                    timestamp=_now_iso(),
                    actor=actor or self._actor,
                    attempt_no=attempt_no,
                    status_code=status,
                    error=None if ok else payload.get("error") or payload.get("message"),
                    latency_ms=latency,
                    request_headers=req_headers,
                    request_body=body,
                    response_body=payload if isinstance(payload, dict) else None,
                )
            )

            if ok:
                # 成功
                self._audit(attempts, success=True)
                return WritebackResult(
                    success=True,
                    attempts=attempt_no,
                    error=None,
                    status_code=status,
                    remote_id=str(payload.get("remote_id")) if isinstance(payload, dict) else None,
                    latency_ms=total_latency,
                )

            last_error = payload.get("error") if isinstance(payload, dict) else None
            if not retryable:
                break
            await self._maybe_sleep(attempt_no, retryable=True)

        # 全部尝试失败 → 死信 + 审计
        self._dead_letter(attempts)
        self._audit(attempts, success=False)
        return WritebackResult(
            success=False,
            attempts=len(attempts),
            error=last_error or f"全部 {len(attempts)} 次重试失败 (last_status={last_status})",
            status_code=last_status,
            latency_ms=total_latency,
        )

    # ------------------------------------------------------------------
    # 整单回写（用 extracted_fields 中 status==confirmed 的字段做批注）
    # ------------------------------------------------------------------

    async def post_case_summary(
        self,
        doc: BillingCaseDocument,
        *,
        excel_path: Path | None = None,
    ) -> WritebackResult:
        """回写整单摘要（field_name="_summary"）。"""
        confirmed = [
            (fname, mark)
            for fname, mark in doc.extracted_fields.items()
            if mark.status == DocumentState.CONFIRMED
        ]
        summary_lines = [
            f"case_id={doc.case_id}",
            f"pipeline_state={doc.pipeline_state.value}",
            f"processed_steps={','.join(doc.processed_steps)}",
            f"confirmed_fields={len(confirmed)}/{len(doc.extracted_fields)}",
            f"flagged_fields={','.join(doc.flagged_field_names()) or '(none)'}",
        ]
        if excel_path is not None:
            summary_lines.append(f"excel_export={excel_path.name}")
        note = " | ".join(summary_lines)
        return await self.post_field_note(
            case_id=doc.case_id,
            field_name="_summary",
            note=note,
        )

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------

    async def _send(
        self,
        *,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> httpx.Response:
        if self._transport is not None:
            # 通过 transport 注入：手工包装一个 httpx Request
            request = httpx.Request(
                "POST",
                self._url,
                headers=dict(headers),
                content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            )
            response = await self._transport.handle_async_request(request)
            return response

        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            return await client.post(self._url, headers=dict(headers), json=dict(body))

    async def _maybe_sleep(self, attempt_no: int, *, retryable: bool) -> None:
        if attempt_no >= self._max_attempts:
            return
        delay = self._backoff_base * (2 ** (attempt_no - 1))
        await asyncio.sleep(delay)

    def _is_retryable(self, status_code: int) -> bool:
        if status_code in self.RETRYABLE_5XX:
            return True
        if status_code in self.RETRYABLE_4XX:
            return True
        return False

    def _dead_letter(self, attempts: list[WritebackAttempt]) -> Path:
        """全部重试用完后落盘结构化死信文件（Task 31 深化）。

        死信 JSON 结构::

            {
              "case_id": "...",
              "field_name": "...",
              "dead_lettered_at": "2026-10-17T07:30:00+00:00",
              "reason": "all_retries_exhausted",
              "summary": {
                "success": false,
                "total_attempts": 3,
                "total_latency_ms": 123.4,
                "last_status_code": 503,
                "last_error": "downstream_unavailable"
              },
              "attempts": [
                {
                  "case_id": "...",
                  "field_name": "...",
                  "note": "...",
                  "timestamp": "...",
                  "actor": "...",
                  "attempt_no": 1,
                  "status_code": 500,
                  "error": "...",
                  "latency_ms": 41.2,
                  "request_headers": {...},
                  "request_body": {...},
                  "response_body": {...}
                },
                ...
              ]
            }
        """
        if not attempts:
            return Path()
        last = attempts[-1]
        self._dead_letter_dir.mkdir(parents=True, exist_ok=True)
        ts_safe = last.timestamp.replace(":", "-").replace(".", "-")
        out = self._dead_letter_dir / f"{last.case_id}-{last.field_name}-{ts_safe}.json"
        attempt_dicts = [_attempt_to_dict(a) for a in attempts]
        total_latency_ms = round(sum(a.latency_ms for a in attempts), 3)
        last_status = last.status_code
        payload = {
            "case_id": last.case_id,
            "field_name": last.field_name,
            "dead_lettered_at": _now_iso(),
            "reason": "all_retries_exhausted",
            "summary": {
                "success": False,
                "total_attempts": len(attempts),
                "total_latency_ms": total_latency_ms,
                "last_status_code": last_status,
                "last_error": last.error,
                "first_timestamp": attempts[0].timestamp,
                "last_timestamp": last.timestamp,
            },
            "attempts": attempt_dicts,
        }
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return out

    def _audit(
        self,
        attempts: list[WritebackAttempt],
        *,
        success: bool | None = None,
    ) -> None:
        """审计写入：每次尝试一行 + 收尾 summary 一行（Task 31 深化）。

        行格式（attempt 1..N）::

            {"timestamp":"...", "case_id":"...", "field_name":"...",
             "attempt_no":1, "status_code":500, "latency_ms":40.1,
             "success":false, "error":"...", ...}

        summary 行（attempt_no=0）::

            {"timestamp":"...", "case_id":"...", "field_name":"...",
             "attempt_no":0, "summary":true, "total_attempts":3,
             "total_latency_ms":123.4, "last_status_code":503,
             "last_error":"...", "success":false}
        """
        if not attempts:
            return
        last = attempts[-1]
        # 优先采用显式 success 参数（避免 4xx 提前退出时的状态混淆）
        if success is None:
            success = bool(
                last.status_code is not None and 200 <= last.status_code < 300
            )
        try:
            writer = writeback_audit.writeback_audit_writer(self._audit_log_path)
            for a in attempts:
                record = _attempt_to_dict(a)
                record["success"] = bool(
                    a.status_code is not None and 200 <= a.status_code < 300
                )
                writer.append(record)
            summary = writeback_audit.build_summary_record(
                case_id=last.case_id,
                field_name=last.field_name,
                attempts=len(attempts),
                total_latency_ms=sum(a.latency_ms for a in attempts),
                last_status_code=last.status_code,
                last_error=last.error,
                success=success,
                actor=last.actor,
            )
            writer.append(summary)
        except writeback_audit.WritebackAuditError as exc:
            # 审计失败不能阻断主链路；记录到 logger
            logger.warning("writeback 审计写入失败: %s", exc)


# ---------------------------------------------------------------------------
# 函数级便捷入口
# ---------------------------------------------------------------------------


def case_system_writeback_from_env(
    *,
    url_env: str = "CASE_SYSTEM_WRITE_URL",
    token_env: str = "CASE_SYSTEM_WRITE_TOKEN",
    **kwargs: Any,
) -> CaseSystemWriteback:
    """从环境变量构造 ``CaseSystemWriteback``。"""
    url = os.environ.get(url_env, "")
    token = os.environ.get(token_env, "")
    if not url:
        raise ValueError(f"环境变量 {url_env} 未设置，无法构造回写客户端")
    return CaseSystemWriteback(url=url, token=token, **kwargs)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    """单调时钟（秒）。"""
    import time

    return time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _attempt_to_dict(a: WritebackAttempt) -> dict[str, Any]:
    return {
        "case_id": a.case_id,
        "field_name": a.field_name,
        "note": a.note,
        "timestamp": a.timestamp,
        "actor": a.actor,
        "attempt_no": a.attempt_no,
        "status_code": a.status_code,
        "error": a.error,
        "latency_ms": a.latency_ms,
        "request_headers": a.request_headers,
        "request_body": a.request_body,
        "response_body": a.response_body,
    }


# 别名供 server.py import
writeback = case_system_writeback_from_env


__all__ = [
    "CaseSystemWriteback",
    "WritebackResult",
    "WritebackAttempt",
    "WritebackError",
    "case_system_writeback_from_env",
]
