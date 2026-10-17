"""Task 25 — ModelGateway 审计元数据 + 调用追踪 (logs/audit/llm_calls.jsonl)。

覆盖：
- ``compute_prompt_hash`` / ``compute_response_hash`` —— sha256 hash 确定性 +
  已知值校验 + 顺序无关（sort_keys）+ bytes/str 双路径；
- ``normalize_metadata`` —— timestamp / downstream_status 兜底 + latency_ms
  圆整到 3 位；
- ``with_prompt_response_hashes`` —— 仅在已存在时不覆盖；
- ``has_required_fields`` —— 必备 9 字段检测；
- ``LLMCallAuditWriter.append`` —— 单行写入 + 文件复用 + threading.Lock
  并发安全 + append_safe 兜底失败；
- ``append_llm_call``（顶层）—— 与 ``persistence.audit.append_llm_call``
  兼容；
- 失败调用也能写入审计（downstream_status='unavailable' + error_class /
  error_message 落字段）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from case_bill_validation.persistence.llm_audit import (
    REQUIRED_FIELDS,
    LLMCallAuditWriter,
    append_llm_call,
    canonical_json,
    compute_prompt_hash,
    compute_response_hash,
    has_required_fields,
    normalize_metadata,
    now_iso,
    with_prompt_response_hashes,
)


# ---------------------------------------------------------------------------
# 1. compute_prompt_hash —— sha256 determinism + canonical_json 顺序无关
# ---------------------------------------------------------------------------


def test_compute_prompt_hash_deterministic_for_same_messages():
    msgs = [
        {"role": "user", "content": "你好"},
        {"role": "system", "content": "用中文回"},
    ]
    h1 = compute_prompt_hash(msgs)
    h2 = compute_prompt_hash(msgs)
    assert h1 == h2
    assert len(h1) == 64


def test_compute_prompt_hash_matches_hand_rolled_sha256():
    msgs = [{"role": "user", "content": "abc"}]
    expected = hashlib.sha256(
        canonical_json(msgs).encode("utf-8")
    ).hexdigest()
    assert compute_prompt_hash(msgs) == expected


def test_compute_prompt_hash_list_order_matters_but_dict_keys_dont():
    """list 顺序影响 hash（JSON 序列化保留 list 顺序）；dict 内 sort_keys=True。

    这条断言只验证：同一 messages list（顺序固定）的 hash 与重新构造但
    完全等价的 messages list 一致；同时验证 dict key 顺序不影响 hash。
    """
    # 完全等价的 list（顺序与 key 排序一致）→ 同一 hash
    a = [{"role": "user", "content": "x"}, {"role": "system", "content": "y"}]
    b = [{"role": "user", "content": "x"}, {"role": "system", "content": "y"}]
    assert compute_prompt_hash(a) == compute_prompt_hash(b)
    # dict 的 key 顺序不影响 hash
    c = [{"role": "user", "content": "x"}]
    d = [{"content": "x", "role": "user"}]
    assert compute_prompt_hash(c) == compute_prompt_hash(d)
    # 不同 list 顺序 → 不同 hash（JSON list 保序）
    reordered = list(reversed(a))
    assert compute_prompt_hash(a) != compute_prompt_hash(reordered)


def test_compute_prompt_hash_accepts_string_payload():
    raw = '[{"role":"user","content":"hi"}]'
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert compute_prompt_hash(raw) == expected


def test_compute_prompt_hash_chinese_and_unicode_safe():
    msgs = [{"role": "user", "content": "鬱靉靐"}]
    expected = hashlib.sha256(
        canonical_json(msgs).encode("utf-8")
    ).hexdigest()
    assert compute_prompt_hash(msgs) == expected


# ---------------------------------------------------------------------------
# 2. compute_response_hash —— bytes / str 双路径 + 已知 sha256 值
# ---------------------------------------------------------------------------


def test_compute_response_hash_matches_sha256_of_text():
    text = "hello world"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert compute_response_hash(text) == expected


def test_compute_response_hash_handles_bytes():
    text = "hello world"
    assert compute_response_hash(text.encode("utf-8")) == compute_response_hash(text)


def test_compute_response_hash_replaces_invalid_utf8():
    raw = b"\xff\xfeabc"
    # 替换为 U+FFFD 不抛异常
    h = compute_response_hash(raw)
    assert isinstance(h, str) and len(h) == 64


# ---------------------------------------------------------------------------
# 3. normalize_metadata —— 兜底字段 + latency 圆整
# ---------------------------------------------------------------------------


def test_normalize_fills_timestamp_and_downstream_status():
    out = normalize_metadata({"model": "gpt-4o-mini"})
    assert "timestamp" in out
    assert out["timestamp"].count("-") >= 2  # ISO-like
    assert out["downstream_status"] == "unknown"


def test_normalize_preserves_explicit_timestamp_and_status():
    out = normalize_metadata(
        {"timestamp": "2026-01-01T00:00:00Z", "downstream_status": "success"}
    )
    assert out["timestamp"] == "2026-01-01T00:00:00Z"
    assert out["downstream_status"] == "success"


def test_normalize_rounds_latency_ms_to_three_decimals():
    out = normalize_metadata({"latency_ms": 312.123456789})
    assert out["latency_ms"] == 312.123


def test_normalize_accepts_int_latency_without_rounding_error():
    out = normalize_metadata({"latency_ms": 200})
    assert out["latency_ms"] == 200.0


def test_now_iso_returns_iso_string_with_timezone():
    s = now_iso()
    # ISO 8601 contains T separator and a date component
    assert "T" in s
    assert len(s) >= 24


# ---------------------------------------------------------------------------
# 4. with_prompt_response_hashes —— 已存在不覆盖
# ---------------------------------------------------------------------------


def test_with_prompt_response_hashes_fills_missing():
    msgs = [{"role": "user", "content": "x"}]
    out = with_prompt_response_hashes(
        {}, messages=msgs, response_text="resp"
    )
    assert "prompt_hash" in out and "response_hash" in out
    assert out["prompt_hash"] == compute_prompt_hash(msgs)
    assert out["response_hash"] == compute_response_hash("resp")


def test_with_prompt_response_hashes_preserves_existing():
    out = with_prompt_response_hashes(
        {"prompt_hash": "preset-p", "response_hash": "preset-r"},
        messages=[{"role": "user", "content": "x"}],
        response_text="resp",
    )
    assert out["prompt_hash"] == "preset-p"
    assert out["response_hash"] == "preset-r"


def test_with_prompt_response_hashes_skips_when_omitted():
    out = with_prompt_response_hashes({})  # neither message nor response
    assert "prompt_hash" not in out
    assert "response_hash" not in out


# ---------------------------------------------------------------------------
# 5. has_required_fields —— 必备 9 字段检测
# ---------------------------------------------------------------------------


def test_has_required_fields_all_present():
    md = normalize_metadata(
        {
            "case_id": "c1",
            "field_name": "f",
            "model": "gpt-4o-mini",
            "prompt_hash": "p",
            "response_hash": "r",
            "latency_ms": 100.0,
            "http_code": 200,
            "downstream_status": "success",
        }
    )
    ok, missing = has_required_fields(md)
    assert ok is True
    assert missing == []


def test_has_required_fields_missing_case_id_and_field_name():
    md = normalize_metadata({"model": "x", "latency_ms": 1, "http_code": 200})
    ok, missing = has_required_fields(md)
    assert ok is False
    assert "case_id" in missing
    assert "field_name" in missing


def test_required_fields_tuple_is_immutable_spec():
    assert REQUIRED_FIELDS == (
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
# 6. LLMCallAuditWriter —— 单行写入 + 文件复用
# ---------------------------------------------------------------------------


def test_writer_appends_one_jsonl_line(tmp_audit_dir: Path):
    writer = LLMCallAuditWriter.open(audit_dir=tmp_audit_dir)
    md = {
        "case_id": "case-A",
        "field_name": "主要诊断代码",
        "model": "gpt-4o-mini",
        "prompt_hash": compute_prompt_hash([{"role": "user", "content": "x"}]),
        "response_hash": compute_response_hash("y"),
        "latency_ms": 123.456,
        "http_code": 200,
        "downstream_status": "success",
    }
    out = writer.append(md)
    log = tmp_audit_dir / "llm_calls.jsonl"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert text.count("\n") >= 1  # 至少 1 行 + 行尾换行
    parsed = [json.loads(ln) for ln in text.strip().split("\n")]
    assert len(parsed) == 1
    # 必需字段全部存在
    for f in REQUIRED_FIELDS:
        assert f in parsed[0]
    assert parsed[0]["latency_ms"] == 123.456  # 3 位圆整
    assert out["latency_ms"] == 123.456


def test_writer_multiple_appends_produce_multiple_lines(tmp_audit_dir: Path):
    writer = LLMCallAuditWriter.open(audit_dir=tmp_audit_dir)
    for i in range(5):
        writer.append(
            {
                "case_id": f"case-{i}",
                "field_name": "f",
                "model": "m",
                "prompt_hash": "p",
                "response_hash": "r",
                "latency_ms": 10 + i,
                "http_code": 200,
                "downstream_status": "success",
            }
        )
    log = tmp_audit_dir / "llm_calls.jsonl"
    parsed = [
        json.loads(ln) for ln in log.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert len(parsed) == 5
    assert [p["case_id"] for p in parsed] == [f"case-{i}" for i in range(5)]


def test_writer_context_manager(tmp_audit_dir: Path):
    with LLMCallAuditWriter.open(audit_dir=tmp_audit_dir) as writer:
        writer.append(
            {
                "case_id": "ctx", "field_name": "f", "model": "m",
                "prompt_hash": "p", "response_hash": "r",
                "latency_ms": 1, "http_code": 200, "downstream_status": "success",
            }
        )
    log = tmp_audit_dir / "llm_calls.jsonl"
    assert log.exists()
    parsed = [
        json.loads(ln) for ln in log.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert len(parsed) == 1


def test_writer_append_safe_returns_none_on_oserror(tmp_audit_dir):
    """append_safe 在 opener 抛 OSError 时返回 None，不抛。

    通过 ``opener=`` hook 注入抛错 opener 工厂（生产代码仍然走 ``Path.open``）。
    """
    def boom_opener(*args: Any, **kwargs: Any) -> Any:
        raise OSError("read-only filesystem (test forced)")

    writer = LLMCallAuditWriter.open(
        audit_dir=tmp_audit_dir, opener=boom_opener
    )
    result = writer.append_safe(
        {
            "case_id": "c", "field_name": "f", "model": "m",
            "prompt_hash": "p", "response_hash": "r",
            "latency_ms": 1, "http_code": 200,
            "downstream_status": "failure",
        }
    )
    assert result is None
    # 旁证：文件未真写入
    assert not (tmp_audit_dir / "llm_calls.jsonl").exists()


def test_writer_thread_safe_concurrent_append(tmp_audit_dir: Path):
    """多个线程并发 append，每条 metadata 都对应一行（无错位）。"""
    writer = LLMCallAuditWriter.open(audit_dir=tmp_audit_dir)
    n_threads = 8
    per_thread = 25
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            writer.append(
                {
                    "case_id": f"case-{tid:02d}",
                    "field_name": f"field-{tid:02d}-{i:02d}",
                    "model": "gpt-4o-mini",
                    "prompt_hash": f"p-{tid}-{i}",
                    "response_hash": f"r-{tid}-{i}",
                    "latency_ms": float(tid * 10 + i),
                    "http_code": 200,
                    "downstream_status": "success",
                }
            )

    threads = [
        threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log = tmp_audit_dir / "llm_calls.jsonl"
    parsed = [
        json.loads(ln) for ln in log.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert len(parsed) == n_threads * per_thread
    # 每行都是合法 JSON（已是事实）；case_id 集合匹配 expectation
    seen = {(p["case_id"], p["field_name"]) for p in parsed}
    expected = {
        (f"case-{tid:02d}", f"field-{tid:02d}-{i:02d}")
        for tid in range(n_threads)
        for i in range(per_thread)
    }
    assert seen == expected


# ---------------------------------------------------------------------------
# 7. 顶层 append_llm_call 兼容壳（与 persistence.audit.append_llm_call 行为一致）
# ---------------------------------------------------------------------------


def test_top_level_append_llm_call_writes_one_jsonl_line(tmp_audit_dir: Path):
    path = append_llm_call(
        {
            "case_id": "case-T",
            "field_name": "f",
            "model": "gpt-4o-mini",
            "prompt_hash": "p",
            "response_hash": "r",
            "latency_ms": 312.5,
            "http_code": 200,
            "downstream_status": "success",
        },
        audit_dir=tmp_audit_dir,
    )
    assert path == tmp_audit_dir / "llm_calls.jsonl"
    parsed = [
        json.loads(ln) for ln in path.read_text(encoding="utf-8").strip().split("\n")
    ]
    assert len(parsed) == 1
    assert parsed[0]["latency_ms"] == 312.5
    # normalize 兜底字段
    assert "timestamp" in parsed[0]
    assert parsed[0]["downstream_status"] == "success"


# ---------------------------------------------------------------------------
# 8. 失败调用（unavailable + 401 / 500）也能落审计
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_class, error_message",
    [
        ("GatewayError", "401 Unauthorized"),
        ("GatewayError", "500 Internal Server Error"),
        ("TimeoutException", "模型超时 (10s)"),
    ],
)
def test_failure_call_metadata_writes_to_audit_jsonl(
    tmp_audit_dir: Path, error_class: str, error_message: str
):
    """失败调用元数据 (http_code / downstream_status='unavailable' / 错误信息)
    也必须写到 ``llm_calls.jsonl``，便于审计追溯。"""
    writer = LLMCallAuditWriter.open(audit_dir=tmp_audit_dir)
    writer.append(
        {
            "case_id": "case-fail",
            "field_name": "主要诊断代码",
            "model": "gpt-4o-mini",
            "prompt_hash": compute_prompt_hash(
                [{"role": "user", "content": "diagnose"}]
            ),
            "response_hash": "",
            "latency_ms": 5321.0,
            "http_code": None,
            "downstream_status": "unavailable",
            "schema_name": "semantic_check",
            "attempts": 2,
            "base_url": "https://api.openai.com/v1",
            "error_class": error_class,
            "error_message": error_message,
        }
    )
    parsed = [
        json.loads(ln)
        for ln in (tmp_audit_dir / "llm_calls.jsonl").read_text(encoding="utf-8")
        .strip()
        .split("\n")
    ]
    assert len(parsed) == 1
    rec = parsed[0]
    assert rec["downstream_status"] == "unavailable"
    assert rec["error_class"] == error_class
    assert rec["error_message"] == error_message
    assert rec["attempts"] == 2
    assert rec["http_code"] is None
    assert rec["response_hash"] == ""  # 失败时无响应 body


# ---------------------------------------------------------------------------
# 9. 重试成功 (retried_success) —— 1 次 401 然后 200 应记录 attempts=2
# ---------------------------------------------------------------------------


def test_retry_success_records_attempts_two_and_status_retried(tmp_audit_dir: Path):
    """首次 401 → 重试 200 → 落审计 attempts=2 + downstream_status='retried_success'。"""
    writer = LLMCallAuditWriter.open(audit_dir=tmp_audit_dir)
    writer.append(
        {
            "case_id": "case-retry",
            "field_name": "f",
            "model": "gpt-4o-mini",
            "prompt_hash": "p",
            "response_hash": "r",
            "latency_ms": 800.0,
            "http_code": 200,
            "downstream_status": "retried_success",
            "attempts": 2,
        }
    )
    parsed = [
        json.loads(ln)
        for ln in (tmp_audit_dir / "llm_calls.jsonl").read_text(encoding="utf-8")
        .strip()
        .split("\n")
    ]
    rec = parsed[0]
    assert rec["downstream_status"] == "retried_success"
    assert rec["attempts"] == 2


# ---------------------------------------------------------------------------
# 10. Round-trip 验证：open → append → read (回到原始 dict)
# ---------------------------------------------------------------------------


def test_writer_round_trip_preserves_metadata(tmp_audit_dir: Path):
    """writer.append 出来的 dict 通过 jsonl 读回后字段全部保留。"""
    writer = LLMCallAuditWriter.open(audit_dir=tmp_audit_dir)
    md: dict[str, Any] = {
        "case_id": "case-rt",
        "field_name": "出院医嘱",
        "model": "gpt-4o-mini",
        "prompt_hash": "abc",
        "response_hash": "def",
        "latency_ms": 250.0,
        "http_code": 200,
        "downstream_status": "success",
        "schema_name": "semantic_check",
        "attempts": 1,
        "base_url": "https://api.openai.com/v1",
        "error_class": "",
        "error_message": "",
        "extra_field": "保留",  # 未知字段保留
    }
    writer.append(md)
    parsed = [
        json.loads(ln)
        for ln in (tmp_audit_dir / "llm_calls.jsonl").read_text(encoding="utf-8")
        .strip()
        .split("\n")
    ]
    rec = parsed[0]
    for key, val in md.items():
        assert rec.get(key) == val, f"字段 {key} 不一致: {rec.get(key)} != {val}"
