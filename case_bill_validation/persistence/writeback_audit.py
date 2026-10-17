"""Writeback 重试结果审计（Task 31 · 深化）。

把病案系统回写每次尝试（含失败）与最终 outcome 写到
``logs/audit/writebacks.jsonl``，供后续审计 / SIEM / 医保飞检核查。

设计要点
--------

- **每行一条 JSON**（JSON Lines）；保留时间序；
- **必填字段**：``timestamp`` / ``case_id`` / ``field_name`` /
  ``attempt_no`` / ``status_code`` / ``latency_ms`` / ``success``；
- **两层记录**：(1) 每次尝试一行（``attempt_no`` 从 1 开始）；
  (2) 收尾一行 ``summary`` 记录（``attempt_no=0``，含总 attempts /
  total_latency_ms / last_status_code / last_error）；
- **并发安全**：``threading.Lock`` 串行写；
- **健壮兜底**：缺字段抛 ``WritebackAuditError``（业务层应 catch
  避免污染主链路，但默认会让问题早暴露）；
- **机器可读**：``canonical_json`` 用 ``sort_keys=True + default=str``
  保证稳定序列化，便于下游 grep / diff / hash 校验。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 错误 & 常量
# ---------------------------------------------------------------------------


class WritebackAuditError(RuntimeError):
    """Writeback 审计写入失败（业务层可选择 catch）。"""


REQUIRED_FIELDS: tuple[str, ...] = (
    "timestamp",
    "case_id",
    "field_name",
    "attempt_no",
    "status_code",
    "latency_ms",
    "success",
)

SUMMARY_ATTEMPT_NO: int = 0  # 收尾汇总行的 attempt_no 标记


# ---------------------------------------------------------------------------
# 时间 / 序列化 helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """当前 UTC ISO8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def canonical_json(record: Mapping[str, Any]) -> str:
    """稳定序列化：sort_keys=True + ensure_ascii=False + default=str。"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """兜底字段：缺 timestamp 补 ISO；缺 attempt_no 视作 1。"""
    out: dict[str, Any] = dict(record)
    out.setdefault("timestamp", now_iso())
    out.setdefault("attempt_no", 1)
    return out


def has_required_fields(record: Mapping[str, Any]) -> bool:
    return all(k in record for k in REQUIRED_FIELDS)


def missing_required_fields(record: Mapping[str, Any]) -> list[str]:
    return [f for f in REQUIRED_FIELDS if f not in record]


# ---------------------------------------------------------------------------
# 顶层入口（无锁，单条追加）
# ---------------------------------------------------------------------------


def append_writeback(
    record: Mapping[str, Any],
    *,
    audit_log_path: Path | str | None = None,
) -> Path:
    """追加一条 writeback 审计记录到 JSONL 文件。

    参数
    ----
    record: 审计记录 dict；必须含 ``REQUIRED_FIELDS`` 全部 7 项
    audit_log_path: 目标文件，默认 ``logs/audit/writebacks.jsonl``

    返回
    ----
    Path: 实际写入的文件路径

    异常
    ----
    WritebackAuditError: 缺必填字段
    OSError: 文件 IO 失败
    """
    path = Path(audit_log_path) if audit_log_path is not None else Path(
        "logs/audit/writebacks.jsonl"
    )
    payload = normalize_record(record)
    missing = missing_required_fields(payload)
    if missing:
        raise WritebackAuditError(
            f"writeback audit record 缺必填字段: {missing}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json(payload)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")
    return path


def append_writebacks(
    records: Iterable[Mapping[str, Any]],
    *,
    audit_log_path: Path | str | None = None,
) -> Path:
    """批量追加；任一记录失败抛出（已写入的部分保留）。"""
    last_path: Path | None = None
    for rec in records:
        last_path = append_writeback(rec, audit_log_path=audit_log_path)
    if last_path is None:
        return (
            Path(audit_log_path)
            if audit_log_path is not None
            else Path("logs/audit/writebacks.jsonl")
        )
    return last_path


# ---------------------------------------------------------------------------
# 并发安全包装
# ---------------------------------------------------------------------------


@dataclass
class WritebackAuditWriter:
    """线程安全的 writeback 审计写入器。

    用 ``threading.Lock`` 串行化多线程追加；适合与
    ``CaseSystemWriteback`` 共用进程内同一实例。
    """

    audit_log_path: Path

    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self.audit_log_path = Path(self.audit_log_path)

    @property
    def path(self) -> Path:
        return self.audit_log_path

    def append(self, record: Mapping[str, Any]) -> Path:
        """线程安全追加。"""
        with self._lock:
            return append_writeback(record, audit_log_path=self.audit_log_path)

    def append_many(self, records: Iterable[Mapping[str, Any]]) -> Path:
        """线程安全批量追加。"""
        with self._lock:
            return append_writebacks(records, audit_log_path=self.audit_log_path)

    def read_all(self) -> list[dict[str, Any]]:
        """读取全量审计行（按顺序）；文件不存在返回空列表。"""
        if not self.audit_log_path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.audit_log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("writeback_audit: 跳过无法解析的行: %r", line[:200])
        return out


def writeback_audit_writer(audit_log_path: Path | str) -> WritebackAuditWriter:
    """工厂函数。"""
    return WritebackAuditWriter(audit_log_path=Path(audit_log_path))


# ---------------------------------------------------------------------------
# summary 记录构造器（供 CaseSystemWriteback 调用）
# ---------------------------------------------------------------------------


def build_summary_record(
    *,
    case_id: str,
    field_name: str,
    attempts: int,
    total_latency_ms: float,
    last_status_code: int | None,
    last_error: str | None,
    success: bool,
    actor: str = "settlement-clerk",
) -> dict[str, Any]:
    """构造收尾 summary 行（``attempt_no=0``）。"""
    return {
        "timestamp": now_iso(),
        "case_id": case_id,
        "field_name": field_name,
        "attempt_no": SUMMARY_ATTEMPT_NO,
        "status_code": last_status_code,
        "latency_ms": round(float(total_latency_ms), 3),
        "success": bool(success),
        "summary": True,
        "total_attempts": attempts,
        "last_error": last_error,
        "actor": actor,
    }


__all__ = [
    "WritebackAuditError",
    "WritebackAuditWriter",
    "REQUIRED_FIELDS",
    "SUMMARY_ATTEMPT_NO",
    "now_iso",
    "canonical_json",
    "normalize_record",
    "has_required_fields",
    "missing_required_fields",
    "append_writeback",
    "append_writebacks",
    "writeback_audit_writer",
    "build_summary_record",
]