"""OpenAI-compatible ModelGateway（Task 8 实装）。

设计要点
--------

- **协议**：单一 OpenAI-compatible HTTP 协议（``POST {base_url}/chat/completions``），
  业务层零硬编码厂商 SDK；本地替身（``StubModelGateway``）由 tests 注入；
- **结构化输出**：请求体带 ``response_format={"type": "json_object"}`` + ``json_schema``
  字段；响应解析为 dict，结构化校验由 ``llm/schemas.py`` 走 jsonschema；
- **错误语义**：
    - 401 / 403 / 500 / 502 / 503 / 超时 / 非 JSON / 结构化校验失败 → 重试 1 次 →
      仍失败抛 :class:`GatewayError`；
    - ``GatewayError`` 携带可审计 ``audit_metadata()`` dict，落到
      ``logs/audit/llm_calls.jsonl`` 一行 JSON；
- **审计嵌入**（Task 25）：``chat_structured`` 内嵌通过
  :mod:`case_bill_validation.persistence.llm_audit` 计算
  ``prompt_hash = sha256(canonical_json(messages))`` 与
  ``response_hash = sha256(response_text)``，归一化 ``latency_ms`` /
  ``timestamp``，业务层与 gateway 共享同一口径；
- **配置**：base_url / api_key / model / timeout 全部来自
  :class:`case_bill_validation.config.CaseBillValidationSettings`（环境变量驱动）；
- **可替换性**：（子类 / 注入）业务层只依赖 ``chat_structured`` 协议，不直接依赖
  httpx client；测试用 ``StubModelGateway`` 即可整套覆盖。

参考
----
- ``spec.md`` §5（审计维度）；§6（开源对照：OpenAI-compatible ModelGateway）；
- ``plan.md`` §6（ModelGateway 计划 + 审计元数据 7 项字段）；
- ``github_ref/stanza/stanza/server/main.py:42`` —— 流式 JSON 输出形态参考；
- ``stanza/pipeline/core.py:176-513`` —— pipeline processor 错误降级模式；
- ``stanza/pipeline/constituency_processor.py:108-115`` —— PROVIDES/REQUIRES 注册模式。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import httpx

from case_bill_validation.llm.schemas import validate_against_dict
from case_bill_validation.persistence.llm_audit import (
    compute_prompt_hash,
    compute_response_hash,
    now_iso as _now_iso,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常与可审计元数据
# ---------------------------------------------------------------------------


class GatewayError(RuntimeError):
    """ModelGateway 调用失败（含 401 / 500 / 超时 / JSON 解析 / Schema 校验）。

    携带 ``audit_metadata`` 让业务层（``SemanticCheckerProcessor`` / pipeline）写到
    ``logs/audit/llm_calls.jsonl``。
    """

    def __init__(
        self,
        message: str,
        *,
        audit_metadata: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.audit_metadata: dict[str, Any] = dict(audit_metadata or {})
        self.cause = cause

    def to_audit_dict(self) -> dict[str, Any]:
        """标准化的审计 dict；由 ``persistence.audit.append_llm_call`` 序列化。"""
        out = dict(self.audit_metadata)
        out.setdefault("error_class", type(self.cause).__name__ if self.cause else "")
        out.setdefault("error_message", str(self.cause) if self.cause else str(self))
        return out


@dataclass
class LLMCallMetadata:
    """一次 LLM 调用的可审计元数据。

    字段全部落到 ``logs/audit/llm_calls.jsonl`` 一行 JSON（含模型 / prompt hash /
    响应 hash / 时延 / HTTP 状态 / 下游业务语义状态 / 错误信息）。
    """

    case_id: str
    field_name: str
    model: str
    prompt_hash: str
    response_hash: str
    latency_ms: float
    http_code: int | None
    downstream_status: str  # success / retried / failed / unavailable
    timestamp: str  # ISO-8601
    schema_name: str | None = None
    attempts: int = 1
    error_class: str = ""
    error_message: str = ""
    base_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "field_name": self.field_name,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "latency_ms": round(self.latency_ms, 3),
            "http_code": self.http_code,
            "downstream_status": self.downstream_status,
            "timestamp": self.timestamp,
            "schema_name": self.schema_name,
            "attempts": self.attempts,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "base_url": self.base_url,
        }


# ---------------------------------------------------------------------------
# ModelGateway 主类
# ---------------------------------------------------------------------------


class ModelGateway:
    """OpenAI-compatible ModelGateway（httpx 异步 client）。

    使用方式
    --------
    >>> gw = ModelGateway.from_settings()
    >>> response = await gw.chat_structured(
    ...     case_id="case-001",
    ...     field_name="主要诊断代码",
    ...     messages=[{"role": "user", "content": "..."}],
    ...     schema_name="semantic_check",
    ...     json_schema={...},
    ... )

    :class:`GatewayError` 抛出时仍带 ``audit_metadata``，由上层决定是否降级。
    """

    #: 默认重试次数（含首次）。spec §6 要求"重试 1 次" = 最多 2 次调用。
    DEFAULT_MAX_ATTEMPTS = 2

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 30,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 不能为空")
        if not api_key:
            raise ValueError("api_key 不能为空（凭据一律从环境变量读）")
        if not model:
            raise ValueError("model 不能为空")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self._client = client  # 允许测试注入自定义 client
        self._owns_client = client is None

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls) -> "ModelGateway":
        """从环境变量 + .env 加载构造。"""
        from case_bill_validation.config import get_settings

        s = get_settings()
        return cls(
            base_url=s.model_gateway_base_url,
            api_key=s.model_gateway_api_key,
            model=s.model_gateway_model,
            timeout=s.model_gateway_timeout,
        )

    # ------------------------------------------------------------------
    # 异步 client 生命周期
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # 主路径
    # ------------------------------------------------------------------

    async def chat_structured(
        self,
        *,
        case_id: str,
        field_name: str,
        messages: list[Mapping[str, str]],
        schema_name: str,
        json_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """调用 LLM 拿结构化 JSON 响应。

        参数
        ----
        case_id: 审计追溯键；
        field_name: 审计追溯键；
        messages: OpenAI chat 格式 ``[{"role":..., "content":...}, ...]``；
        schema_name: schema 名（audit 用）；如 ``"semantic_check"`` /
          ``"extraction"``；
        json_schema: 要被作为 ``response_format`` 的一部分发给 LLM 的 JSON
          Schema dict（按 OpenAI-compatible 约定塞进 body）。

        返回
        ----
        ``{"_raw": <完整响应>, "_metadata": LLMCallMetadata.to_dict()}``。

        抛出
        ----
        :class:`GatewayError` —— 401/500/超时/非 JSON/Schema 校验失败重试
        ``max_attempts`` 次后仍失败；``GatewayError.audit_metadata`` 含完整
        审计信息。
        """
        if not messages:
            raise ValueError("messages 不能为空")
        # 1) prompt hash：走 persistence.llm_audit.compute_prompt_hash
        #    （sha256(canonical_json(messages))），保证业务层与 gateway 口径一致；
        #    Task 25 把审计 hash 计算统一收到 llm_audit 模块。
        prompt_hash = compute_prompt_hash(list(messages))

        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": list(messages),
            "response_format": {"type": "json_object"},
            "json_schema": dict(json_schema),
        }

        client = await self._get_client()
        attempts_used = 0
        last_error: GatewayError | None = None
        for attempt in range(1, self.max_attempts + 1):
            attempts_used = attempt
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            t0 = time.perf_counter()
            try:
                resp = await client.post(url, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                last_error = GatewayError(
                    f"ModelGateway 超时（attempt={attempt}）",
                    audit_metadata={
                        "case_id": case_id,
                        "field_name": field_name,
                        "model": self.model,
                        "prompt_hash": prompt_hash,
                        "response_hash": "",
                        "latency_ms": (time.perf_counter() - t0) * 1000.0,
                        "http_code": None,
                        "downstream_status": (
                            "failed" if attempt < self.max_attempts else "unavailable"
                        ),
                        "timestamp": _now_iso(),
                        "schema_name": schema_name,
                        "attempts": attempt,
                        "base_url": self.base_url,
                    },
                    cause=exc,
                )
                logger.warning(
                    "ModelGateway 超时 attempt=%d/%d field=%s",
                    attempt,
                    self.max_attempts,
                    field_name,
                )
                continue
            except httpx.HTTPError as exc:
                last_error = GatewayError(
                    f"ModelGateway HTTP 错误（attempt={attempt}）：{exc}",
                    audit_metadata={
                        "case_id": case_id,
                        "field_name": field_name,
                        "model": self.model,
                        "prompt_hash": prompt_hash,
                        "response_hash": "",
                        "latency_ms": (time.perf_counter() - t0) * 1000.0,
                        "http_code": None,
                        "downstream_status": "failed",
                        "timestamp": _now_iso(),
                        "schema_name": schema_name,
                        "attempts": attempt,
                        "base_url": self.base_url,
                    },
                    cause=exc,
                )
                logger.warning(
                    "ModelGateway HTTP 错误 attempt=%d/%d field=%s: %s",
                    attempt,
                    self.max_attempts,
                    field_name,
                    exc,
                )
                continue

            latency_ms = (time.perf_counter() - t0) * 1000.0
            http_code = resp.status_code

            if http_code in (401, 403, 500, 502, 503, 504):
                err = GatewayError(
                    f"ModelGateway HTTP {http_code}（attempt={attempt}）",
                    audit_metadata={
                        "case_id": case_id,
                        "field_name": field_name,
                        "model": self.model,
                        "prompt_hash": prompt_hash,
                        "response_hash": "",
                        "latency_ms": latency_ms,
                        "http_code": http_code,
                        "downstream_status": (
                            "failed" if attempt < self.max_attempts else "unavailable"
                        ),
                        "timestamp": _now_iso(),
                        "schema_name": schema_name,
                        "attempts": attempt,
                        "base_url": self.base_url,
                    },
                )
                # 把响应体前 200 字符拼到错误信息便于审计
                err.audit_metadata["error_message"] = (
                    resp.text[:200] if resp.text else ""
                )
                last_error = err
                logger.warning(
                    "ModelGateway HTTP %s attempt=%d/%d field=%s",
                    http_code,
                    attempt,
                    self.max_attempts,
                    field_name,
                )
                continue

            # 200 / 2xx：解析 JSON + 校验
            raw_text = resp.text
            # response hash：走 persistence.llm_audit.compute_response_hash
            # （sha256(raw_text)）—— 与 prompt_hash 计算口径一致（Task 25）
            response_hash = compute_response_hash(raw_text)
            try:
                payload = resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = GatewayError(
                    f"ModelGateway 响应非 JSON（attempt={attempt}）",
                    audit_metadata={
                        "case_id": case_id,
                        "field_name": field_name,
                        "model": self.model,
                        "prompt_hash": prompt_hash,
                        "response_hash": response_hash,
                        "latency_ms": latency_ms,
                        "http_code": http_code,
                        "downstream_status": (
                            "failed" if attempt < self.max_attempts else "unavailable"
                        ),
                        "timestamp": _now_iso(),
                        "schema_name": schema_name,
                        "attempts": attempt,
                        "base_url": self.base_url,
                    },
                    cause=exc,
                )
                last_error.audit_metadata["error_message"] = raw_text[:200]
                continue

            # 提取 message.content
            try:
                content = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                last_error = GatewayError(
                    f"ModelGateway 响应缺 choices[0].message.content（attempt={attempt}）",
                    audit_metadata={
                        "case_id": case_id,
                        "field_name": field_name,
                        "model": self.model,
                        "prompt_hash": prompt_hash,
                        "response_hash": response_hash,
                        "latency_ms": latency_ms,
                        "http_code": http_code,
                        "downstream_status": (
                            "failed" if attempt < self.max_attempts else "unavailable"
                        ),
                        "timestamp": _now_iso(),
                        "schema_name": schema_name,
                        "attempts": attempt,
                        "base_url": self.base_url,
                    },
                    cause=exc,
                )
                continue

            # content 可能是 str（json_object 模式）或已经是 dict
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except (ValueError, json.JSONDecodeError) as exc:
                    last_error = GatewayError(
                        f"ModelGateway content 非合法 JSON（attempt={attempt}）",
                        audit_metadata={
                            "case_id": case_id,
                            "field_name": field_name,
                            "model": self.model,
                            "prompt_hash": prompt_hash,
                            "response_hash": response_hash,
                            "latency_ms": latency_ms,
                            "http_code": http_code,
                            "downstream_status": (
                                "failed"
                                if attempt < self.max_attempts
                                else "unavailable"
                            ),
                            "timestamp": _now_iso(),
                            "schema_name": schema_name,
                            "attempts": attempt,
                            "base_url": self.base_url,
                        },
                        cause=exc,
                    )
                    last_error.audit_metadata["error_message"] = content[:200]
                    continue
            elif isinstance(content, dict):
                parsed = content
            else:
                last_error = GatewayError(
                    f"ModelGateway content 类型异常 {type(content).__name__}（attempt={attempt}）",
                    audit_metadata={
                        "case_id": case_id,
                        "field_name": field_name,
                        "model": self.model,
                        "prompt_hash": prompt_hash,
                        "response_hash": response_hash,
                        "latency_ms": latency_ms,
                        "http_code": http_code,
                        "downstream_status": (
                            "failed"
                            if attempt < self.max_attempts
                            else "unavailable"
                        ),
                        "timestamp": _now_iso(),
                        "schema_name": schema_name,
                        "attempts": attempt,
                        "base_url": self.base_url,
                    },
                )
                continue

            # Schema 校验（jsonschema Draft-07；通过 llm.schemas 严格校验）
            schema_ok, schema_err = validate_against_dict(parsed, json_schema)
            if not schema_ok:
                last_error = GatewayError(
                    f"ModelGateway 结构化校验失败（attempt={attempt}）：{schema_err}",
                    audit_metadata={
                        "case_id": case_id,
                        "field_name": field_name,
                        "model": self.model,
                        "prompt_hash": prompt_hash,
                        "response_hash": response_hash,
                        "latency_ms": latency_ms,
                        "http_code": http_code,
                        "downstream_status": (
                            "failed"
                            if attempt < self.max_attempts
                            else "unavailable"
                        ),
                        "timestamp": _now_iso(),
                        "schema_name": schema_name,
                        "attempts": attempt,
                        "base_url": self.base_url,
                    },
                )
                last_error.audit_metadata["error_message"] = schema_err
                continue

            # 成功
            meta = LLMCallMetadata(
                case_id=case_id,
                field_name=field_name,
                model=self.model,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
                latency_ms=latency_ms,
                http_code=http_code,
                downstream_status=(
                    "success" if attempt == 1 else "retried_success"
                ),
                timestamp=_now_iso(),
                schema_name=schema_name,
                attempts=attempt,
                base_url=self.base_url,
            ).to_dict()
            return {"_raw": parsed, "_metadata": meta}

        # 全部失败 → 抛最后一条 GatewayError（带完整审计元数据）
        assert last_error is not None
        # 更新 attempts
        last_error.audit_metadata["attempts"] = attempts_used
        last_error.audit_metadata["downstream_status"] = "unavailable"
        raise last_error

    # ------------------------------------------------------------------
    # 同步 helper（业务层偶尔需要；测试也用得上）
    # ------------------------------------------------------------------

    def chat_structured_sync(
        self,
        *,
        case_id: str,
        field_name: str,
        messages: list[Mapping[str, str]],
        schema_name: str,
        json_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        """同步入口；内部用 ``asyncio.run`` 包一层。便于 CLI / 调试。"""
        import asyncio

        return asyncio.run(
            self.chat_structured(
                case_id=case_id,
                field_name=field_name,
                messages=messages,
                schema_name=schema_name,
                json_schema=json_schema,
            )
        )

    # ------------------------------------------------------------------
    # 审计元数据助手（供业务层直接落盘）
    # ------------------------------------------------------------------

    def audit_metadata(self, error: GatewayError) -> dict[str, Any]:
        """把 :class:`GatewayError` 携带的 metadata 标准化（兼容 None 字段）。"""
        return error.to_audit_dict()


# ---------------------------------------------------------------------------
# Stub / 本地替身（测试用）
# ---------------------------------------------------------------------------


class StubModelGateway:
    """测试用本地协议替身。

    行为
    ----
    - ``set_response(field_name, payload)`` —— 设定某字段返回的 dict；
    - ``set_error(field_name, exc)`` —— 设定某字段抛的 GatewayError；
    - 默认未设定 → 抛 :class:`GatewayError`（"not configured"），强制
      测试用例显式声明期望；
    - 暴露 ``call_log`` 列表，审计测试用。
    """

    def __init__(
        self,
        *,
        responses: Mapping[str, dict[str, Any]] | None = None,
        errors: Mapping[str, GatewayError] | None = None,
    ) -> None:
        self._responses: dict[str, dict[str, Any]] = dict(responses or {})
        self._errors: dict[str, GatewayError] = dict(errors or {})
        self.call_log: list[dict[str, Any]] = []

    async def chat_structured(
        self,
        *,
        case_id: str,
        field_name: str,
        messages: Iterable[Mapping[str, str]],
        schema_name: str,
        json_schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.call_log.append(
            {
                "case_id": case_id,
                "field_name": field_name,
                "schema_name": schema_name,
                "messages": list(messages),
                "schema": dict(json_schema),
            }
        )
        if field_name in self._errors:
            raise self._errors[field_name]
        if field_name in self._responses:
            payload = self._responses[field_name]
            return {"_raw": payload, "_metadata": {
                "case_id": case_id,
                "field_name": field_name,
                "model": "stub",
                "prompt_hash": "",
                "response_hash": "",
                "latency_ms": 0.1,
                "http_code": 200,
                "downstream_status": "success",
                "timestamp": _now_iso(),
                "schema_name": schema_name,
                "attempts": 1,
                "base_url": "stub://local",
            }}
        raise GatewayError(
            f"StubModelGateway 未配置 field_name={field_name!r}",
            audit_metadata={
                "case_id": case_id,
                "field_name": field_name,
                "model": "stub",
                "prompt_hash": "",
                "response_hash": "",
                "latency_ms": 0.0,
                "http_code": None,
                "downstream_status": "unavailable",
                "timestamp": _now_iso(),
                "schema_name": schema_name,
                "attempts": 1,
                "base_url": "stub://local",
            },
        )

    def set_response(self, field_name: str, payload: dict[str, Any]) -> None:
        self._responses[field_name] = payload

    def set_error(self, field_name: str, exc: GatewayError) -> None:
        self._errors[field_name] = exc

    def extract_field(
        self, *, raw_text: str, field_def: Any
    ) -> dict[str, Any] | None:
        """兼容 ``pipeline/extractor.py::extract_by_llm`` 的 duck-typed 调用。"""
        payload = self._responses.get(field_def.name)
        if payload is None:
            return None
        return payload


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# expose
# ---------------------------------------------------------------------------


__all__ = [
    "ModelGateway",
    "StubModelGateway",
    "GatewayError",
    "LLMCallMetadata",
]
