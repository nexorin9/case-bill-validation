"""审计层（JSON Lines + 写锁 + hash 链 + 权限 / 断点检测）。

Task 8 + 12 + 32 联合实装
-----------------------

- :func:`append_llm_call` —— 写 ``logs/audit/llm_calls.jsonl`` 一行 JSON；
  含 prompt_hash / response_hash / latency_ms / http_code /
  downstream_status / timestamp；
- :class:`AuditLog` —— 通用的"追加一行 JSON Lines + hash 链"容器（Task 12
  用；Task 32 加深权限 / 断点检测 / 重放保护）；
- :class:`InvalidHashChainError` —— hash 链校验失败异常（Task 32）；
- :func:`verify_chain` —— 模块级 hash 链校验（Task 32）。

参考
----
- ``spec.md`` §5（closure_dimensions 中"安全审计"维度）；
- ``plan.md`` §6（ModelGateway 审计元数据 → logs/audit/llm_calls.jsonl）；
- ``github_ref/stanza/stanza/pipeline/core.py`` —— pipeline 日志接入形态。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# 默认审计目录解析
# ---------------------------------------------------------------------------


def default_audit_dir() -> Path:
    """从 settings 拿 ``AUDIT_LOG_DIR``；缺省 ``./logs/audit``。"""
    try:
        from case_bill_validation.config import get_settings

        return Path(get_settings().audit_log_dir)
    except Exception:  # noqa: BLE001 —— settings 缺失时（如单元测试）兜底
        return Path("./logs/audit")


def llm_calls_path(audit_dir: Path | None = None) -> Path:
    """``llm_calls.jsonl`` 完整路径。"""
    d = audit_dir or default_audit_dir()
    return d / "llm_calls.jsonl"


# ---------------------------------------------------------------------------
# 单条审计元数据写入
# ---------------------------------------------------------------------------


def append_llm_call(
    metadata: Mapping[str, Any],
    *,
    audit_dir: Path | None = None,
) -> Path:
    """追加一条 LLM 调用审计记录到 ``logs/audit/llm_calls.jsonl``。

    参数
    ----
    metadata: :class:`LLMCallMetadata.to_dict()` 或等价 dict；
    audit_dir: 自定义目录；缺省走 :func:`default_audit_dir`；
    """
    target_dir = audit_dir or default_audit_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / "llm_calls.jsonl"

    payload = dict(metadata)
    # 兜底：timestamp / latency_ms / status
    payload.setdefault("timestamp", _now_iso())
    payload.setdefault("downstream_status", "unknown")

    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    # 追加写入；行尾强制 \n
    with target_path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")
    return target_path


# ---------------------------------------------------------------------------
# 通用 JSON Lines 容器（带 hash 链；Task 12 / 32 扩展）
# ---------------------------------------------------------------------------


class AuditLog:
    """通用 JSON Lines 审计日志容器。

    字段::

        audit_dir     —— 日志目录（每个 case_id 一个 .jsonl）；
        case_id       —— 关联单据；
        chain_hashes  —— 是否启用 hash 链（每条 entry 含 prev_hash + hash）。

    用法::

        log = AuditLog.open(case_id="case-001")
        log.append({"actor": "rule_checker", "action": "rule_check",
                    "note": "passed=20"})
        log.read()  # → list[dict]
        log.verify_chain()  # → bool
    """

    _LOCK = threading.Lock()  # 类级：同进程多线程下避免并发追加错位

    def __init__(
        self,
        path: Path,
        *,
        chain_hashes: bool = True,
        read_only_dir: bool = False,
    ) -> None:
        self.path = path
        self.chain_hashes = chain_hashes
        self.read_only_dir = read_only_dir
        self._last_hash: str | None = None
        if path.exists():
            self._last_hash = _tail_hash(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    # ------------------------------------------------------------------
    # 工厂
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        case_id: str,
        *,
        audit_dir: Path | None = None,
        chain_hashes: bool = True,
    ) -> "AuditLog":
        d = audit_dir or default_audit_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{case_id}.jsonl"
        return cls(path, chain_hashes=chain_hashes)

    # ------------------------------------------------------------------
    # 追加 / 读取
    # ------------------------------------------------------------------

    def append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """追加一条 entry；启用 hash 链时自动填 prev_hash + hash。

        重放保护（Task 32）
        ------------------
        启用 ``chain_hashes`` 时，先在写入前用 :func:`verify_chain` 校验既有
        文件的链完整性；若发现人为篡改或断链，抛
        :class:`InvalidHashChainError` **拒绝本次写入**，避免攻击者通过篡改
        历史记录后再追加新条目来覆盖证据链。
        """
        if self.chain_hashes:
            ok, err = verify_chain(self.path)
            if not ok:
                raise InvalidHashChainError(
                    f"hash 链校验失败，拒绝追加写入: {err}"
                )
        record = dict(entry)
        record.setdefault("timestamp", _now_iso())
        prev_hash = self._last_hash
        if self.chain_hashes:
            record["prev_hash"] = prev_hash or ""
            # hash 输入：prev + body（body 不含 hash + prev_hash，
            # 与 verify 端 pop 规则一致）
            hash_body = {
                k: v
                for k, v in record.items()
                if k not in ("hash", "prev_hash")
            }
            body_str = json.dumps(hash_body, sort_keys=True, default=str)
            record["hash"] = _sha256((prev_hash or "") + body_str)
            self._last_hash = record["hash"]
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self.__class__._LOCK:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
        return record

    def read(self) -> list[dict[str, Any]]:
        """读取全部条目（按行 JSON 解析）；损坏行抛 JSONDecodeError。"""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                out.append(json.loads(ln))
        return out

    def verify_chain(self) -> tuple[bool, str | None]:
        """校验 hash 链完整性；返回 ``(ok, error_msg)``。"""
        if not self.chain_hashes:
            return True, None
        prev: str | None = None
        for i, record in enumerate(self.read()):
            record_prev_raw = record.get("prev_hash", "") or ""
            if record_prev_raw != (prev or ""):
                return False, f"line {i + 1}: prev_hash 不连续"
            # 与 append 端一致：body 不含 hash + prev_hash
            body = {
                k: v
                for k, v in record.items()
                if k not in ("hash", "prev_hash")
            }
            expected = _sha256(
                (prev or "") + json.dumps(body, sort_keys=True, default=str)
            )
            if expected != record.get("hash"):
                return False, f"line {i + 1}: hash 不匹配"
            prev = record.get("hash")
        return True, None

    # ------------------------------------------------------------------
    # 权限 / 目录只读（Task 32 加深）
    # ------------------------------------------------------------------

    def chmod_readonly(self) -> None:
        """将日志目录置为只读（0o555）；后续 append 会失败。"""
        self.read_only_dir = True
        os.chmod(self.path.parent, 0o555)

    def chmod_writable(self) -> None:
        os.chmod(self.path.parent, 0o755)
        self.read_only_dir = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    last_hash: str | None = None
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            h = rec.get("hash")
            if h:
                last_hash = h
    return last_hash


# ---------------------------------------------------------------------------
# Task 32 — InvalidHashChainError + 模块级 verify_chain + 重放保护
# ---------------------------------------------------------------------------


class InvalidHashChainError(Exception):
    """hash 链校验失败（人为篡改 / 断链 / 重放攻击）。

    由 :meth:`AuditLog.append` 在写入前检测到既有链被破坏时抛出。
    字段 ``line_no`` / ``expected`` / ``actual`` 提供定位信息，便于
    审计人员定位断点位置。
    """

    def __init__(
        self,
        message: str,
        *,
        line_no: int | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        super().__init__(message)
        self.line_no = line_no
        self.expected = expected
        self.actual = actual

    def __repr__(self) -> str:  # pragma: no cover —— 调试便利
        return (
            f"InvalidHashChainError(message={self.args[0]!r}, "
            f"line_no={self.line_no!r}, expected={self.expected!r}, "
            f"actual={self.actual!r})"
        )


def verify_chain(path: Path | str) -> tuple[bool, str | None]:
    """模块级 hash 链校验（直接接受路径，不依赖 AuditLog 实例）。

    返回 ``(ok, error_msg)``。``error_msg`` 含定位信息（line N + 期望值/实际值）。
    文件不存在或为空视为通过；损坏行（非 JSON）也归入失败路径并报告行号。
    """
    p = Path(path)
    if not p.exists():
        return True, None
    text = p.read_text(encoding="utf-8")
    prev: str | None = None
    for i, raw in enumerate(text.splitlines()):
        ln = raw.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError as e:
            return False, f"line {i + 1}: JSON 解析失败: {e}"
        if not isinstance(rec, dict):
            return False, f"line {i + 1}: 期望 dict，收到 {type(rec).__name__}"
        record_prev_raw = rec.get("prev_hash", "") or ""
        if record_prev_raw != (prev or ""):
            return (
                False,
                f"line {i + 1}: prev_hash 不连续 (expected '{prev or ''}', "
                f"got '{record_prev_raw}')",
            )
        body = {
            k: v
            for k, v in rec.items()
            if k not in ("hash", "prev_hash")
        }
        expected_hash = _sha256(
            (prev or "") + json.dumps(body, sort_keys=True, default=str)
        )
        actual_hash = rec.get("hash") or ""
        if expected_hash != actual_hash:
            short = 16
            return (
                False,
                f"line {i + 1}: hash 不匹配 (expected "
                f"'{expected_hash[:short]}…', got '{actual_hash[:short]}…')",
            )
        prev = actual_hash
    return True, None


__all__ = [
    "append_llm_call",
    "AuditLog",
    "InvalidHashChainError",
    "default_audit_dir",
    "llm_calls_path",
    "verify_chain",
]
