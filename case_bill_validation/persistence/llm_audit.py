"""LLM 调用审计持久化层（Task 25 实装）。

设计要点
--------

- :func:`compute_prompt_hash` / :func:`compute_response_hash` 是 spec §5 审计维度
  的两个核心计算字段（``prompt_hash = sha256(canonical_json(messages))`` 、
  ``response_hash = sha256(response_text)``）；被 :mod:`llm.gateway` 直接调用，
  避免业务层与网关重复实现 sha256；
- :class:`LLMCallAuditWriter` 提供线程安全的 ``logs/audit/llm_calls.jsonl`` 写
  入口（每写一行加锁 + ``with open`` 即时落盘 + 异常吞掉不抛业务层）；
- :func:`append_llm_call` 是与 :mod:`persistence.audit` 的兼容壳，新增的
  :func:`normalize_metadata` / :func:`REQUIRED_FIELDS` 保证业务层传入的
  metadata 都经过字段兜底（timestamp / downstream_status / latency 圆整）。

参考
----
- ``spec.md`` §5（"安全审计"维度 → logs/audit/llm_calls.jsonl）；
- ``plan.md`` §6（ModelGateway 审计元数据：model / prompt_hash /
  response_hash / latency_ms / http_code / downstream_status / timestamp）；
- ``audit.py::append_llm_call``（兼容壳，行为一致）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from case_bill_validation.persistence.audit import (
    append_llm_call as _audit_append_llm_call,
)
from case_bill_validation.persistence.audit import (
    default_audit_dir as _audit_default_audit_dir,
)
from case_bill_validation.persistence.audit import (
    llm_calls_path as _audit_llm_calls_path,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# spec §5 审计维度 · 必备 7 项 + 1 项 timestamp
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: tuple[str, ...] = (
    "case_id",
    "field_name",
    "model",
    "prompt_hash",
    "response_hash",
    "latency_ms",
    "http_code",
    "downstream_status",
    "timestamp",
)


# ---------------------------------------------------------------------------
# sha256 计算 helper（gateway.chat_structured 与测试套件共用）
# ---------------------------------------------------------------------------


def hash_text(text: str) -> str:
    """sha256 hex digest of ``text`` encoded as UTF-8。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    """``json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)``。

    Gateway 与测试都用同一口径计算 prompt hash，避免序列化顺序差异。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def compute_prompt_hash(messages: Iterable[Mapping[str, Any]] | str) -> str:
    """sha256 of canonical JSON of OpenAI-style messages。

    参数
    ----
    messages: ``[{"role": "...", "content": "..."}, ...]``；已序列化的 str
      也可直接传入（用于测试期望值校验）。
    """
    if isinstance(messages, str):
        return hash_text(messages)
    return hash_text(canonical_json(list(messages)))


def compute_response_hash(response_text: str | bytes) -> str:
    """sha256 of response body (str or bytes decoded utf-8)。"""
    if isinstance(response_text, bytes):
        response_text = response_text.decode("utf-8", errors="replace")
    return hash_text(response_text)


def now_iso() -> str:
    """UTC ISO-8601 timestamp，精确到秒。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# metadata 字段标准化（与 LLMCallMetadata.to_dict() 口径一致）
# ---------------------------------------------------------------------------


def normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """补全 + 数值化 + 字段规范化。

    行为
    ----
    - ``timestamp`` 缺省 → :func:`now_iso`；
    - ``downstream_status`` 缺省 → ``"unknown"``；
    - ``latency_ms`` 是 number → 圆整到 3 位小数；
    - 其余字段（model / prompt_hash / response_hash / http_code 等）由调用方负责。
    """
    payload = dict(metadata)
    payload.setdefault("timestamp", now_iso())
    payload.setdefault("downstream_status", "unknown")
    if "latency_ms" in payload and isinstance(payload["latency_ms"], (int, float)):
        payload["latency_ms"] = round(float(payload["latency_ms"]), 3)
    return payload


def with_prompt_response_hashes(
    metadata: Mapping[str, Any],
    *,
    messages: Iterable[Mapping[str, Any]] | None = None,
    response_text: str | None = None,
) -> dict[str, Any]:
    """按需回填 ``prompt_hash`` / ``response_hash``（已填则保留）。"""
    payload = dict(metadata)
    if messages is not None and not payload.get("prompt_hash"):
        payload["prompt_hash"] = compute_prompt_hash(messages)
    if response_text is not None and not payload.get("response_hash"):
        payload["response_hash"] = compute_response_hash(response_text)
    return payload


def has_required_fields(metadata: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """返回 ``(all_present, missing_field_names)``。"""
    missing = [f for f in REQUIRED_FIELDS if f not in metadata]
    return (not missing, missing)


# ---------------------------------------------------------------------------
# LLMCallAuditWriter：JSON Lines 复用 writer
# ---------------------------------------------------------------------------


class LLMCallAuditWriter:
    """线程安全的 ``logs/audit/llm_calls.jsonl`` writer。

    用法::

        writer = LLMCallAuditWriter.open(audit_dir=Path("logs/audit"))
        writer.append({
            "case_id": "case-001",
            "field_name": "主要诊断代码",
            "model": "gpt-4o-mini",
            "prompt_hash": "...",
            "response_hash": "...",
            "latency_ms": 312.0,
            "http_code": 200,
            "downstream_status": "success",
        })
        writer.close()

    设计要点
    --------
    - **线程安全**：实例级 :class:`threading.Lock`，多线程并发追加不会错位。
    - **每写一行开一次文件**：避免长持有句柄；进程退出也没有未落盘的残留。
    - **失败吞掉**：磁盘写入失败只打 warning，不抛回业务层；业务层已有
      ``semantic_checker`` 兜底逻辑，写审计不应当反过来炸流水线。
    - **路径来源**：默认 :func:`default_audit_dir` 取得，跟 ``audit.py``
      完全一致；测试可显式 ``audit_dir=tmp_path / 'audit'``。
    """

    def __init__(
        self,
        path: Path,
        *,
        opener: Any | None = None,
    ) -> None:
        self.path = path
        self._lock = threading.Lock()
        # 允许测试注入 opener（默认走 pathlib.Path.open）。生产代码无需关心。
        self._opener: Any = opener if opener is not None else path.open

    # ----- factory ----------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        audit_dir: Path | None = None,
        case_id: str | None = None,  # noqa: ARG003 —— 仅用于可读性
        opener: Any | None = None,
    ) -> "LLMCallAuditWriter":
        """打开或创建 ``llm_calls.jsonl`` writer。

        ``case_id`` 仅作为审计调用站点的可读参数存在，不影响文件路径（单文件）。
        ``opener`` 允许测试注入文件句柄工厂（默认 ``Path.open``）。
        """
        d = audit_dir or _audit_default_audit_dir()
        return cls(_audit_llm_calls_path(d), opener=opener)

    # ----- 写 ----------------------------------------------------------

    def append(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        """追加一行 metadata；返回 normalize 后的 dict。

        抛出
        ----
        :class:`OSError`：磁盘错误（Permission / No space 等）由
        :meth:`append_safe` 吞掉。
        """
        payload = normalize_metadata(metadata)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._opener("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        return payload

    def append_safe(self, metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        """失败只打日志、返回 None；不把异常抛回业务层。"""
        try:
            return self.append(metadata)
        except OSError as exc:
            logger.warning(
                "LLMCallAuditWriter.append 失败 path=%s err=%s", self.path, exc
            )
            return None

    # ----- 资源 --------------------------------------------------------

    def close(self) -> None:
        """当前实现无持有句柄；保留方法用于对称性 + 后续资源持有扩展。"""
        return None

    def __enter__(self) -> "LLMCallAuditWriter":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # 便于调试
        return f"LLMCallAuditWriter(path={self.path!r})"


# ---------------------------------------------------------------------------
# 顶层便捷入口（兼容 persistence.audit.append_llm_call，保留 normalize 兜底）
# ---------------------------------------------------------------------------


def append_llm_call(
    metadata: Mapping[str, Any],
    *,
    audit_dir: Path | None = None,
) -> Path:
    """一层壳：写入 ``logs/audit/llm_calls.jsonl``，保留 :func:`normalize_metadata`。

    返回
    ----
    写入文件的 :class:`pathlib.Path`（与 ``persistence.audit.append_llm_call``
    一致）。
    """
    payload = normalize_metadata(metadata)
    return _audit_append_llm_call(payload, audit_dir=audit_dir)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


__all__ = [
    "REQUIRED_FIELDS",
    "canonical_json",
    "compute_prompt_hash",
    "compute_response_hash",
    "now_iso",
    "normalize_metadata",
    "with_prompt_response_hashes",
    "has_required_fields",
    "LLMCallAuditWriter",
    "append_llm_call",
]
