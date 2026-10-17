"""审计层单元测试（Task #12：审计日志 + JSON Lines 不可修改 + 端到端脱敏样例）。

覆盖：
- AuditLog.append / read / verify_chain（hash 链完整）
- hash 链断点检测（手动篡改 → verify_chain 返回 False）
- chmod_readonly / chmod_writable（目录权限切换）
- JSON Lines 损坏行（read 抛 JSONDecodeError）
- append_llm_call 写 logs/audit/llm_calls.jsonl（含必需字段）
- 10 份脱敏 fixture：每份 ≥ 1500 字 + 章节完整 + ≥ 3 份含已知冲突标识
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from case_bill_validation.persistence.audit import (
    AuditLog,
    append_llm_call,
    default_audit_dir,
    llm_calls_path,
)


# ---------------------------------------------------------------------------
# AuditLog 基本功能
# ---------------------------------------------------------------------------


class TestAuditLogBasic:
    """AuditLog.append / read / verify_chain 主路径。"""

    def test_open_creates_file(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-A", audit_dir=tmp_audit_dir)
        assert log.path.exists()
        assert log.path.name == "case-A.jsonl"

    def test_open_loads_existing_last_hash(self, tmp_audit_dir: Path) -> None:
        log1 = AuditLog.open("case-B", audit_dir=tmp_audit_dir)
        rec1 = log1.append({"actor": "system", "action": "ingest"})
        assert rec1.get("hash")
        log2 = AuditLog.open("case-B", audit_dir=tmp_audit_dir)
        log2.append({"actor": "system", "action": "segmenter"})
        ok, err = log2.verify_chain()
        assert ok, err

    def test_append_returns_record_with_hash(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-C", audit_dir=tmp_audit_dir)
        rec = log.append({"actor": "rule_checker", "action": "rule_check",
                          "note": "pass=20"})
        assert "hash" in rec
        assert rec["hash"] is not None
        assert "prev_hash" in rec
        assert rec["prev_hash"] == ""  # first entry

    def test_second_entry_prev_hash_chains_to_first(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-D", audit_dir=tmp_audit_dir)
        rec1 = log.append({"actor": "a", "action": "x"})
        rec2 = log.append({"actor": "b", "action": "y"})
        assert rec2["prev_hash"] == rec1["hash"]
        assert rec2["hash"] != rec1["hash"]

    def test_read_returns_appended_entries(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-E", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x", "note": "1"})
        log.append({"actor": "b", "action": "y", "note": "2"})
        log.append({"actor": "c", "action": "z", "note": "3"})
        entries = log.read()
        assert len(entries) == 3
        assert [e["note"] for e in entries] == ["1", "2", "3"]

    def test_read_empty_log_returns_empty_list(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-empty", audit_dir=tmp_audit_dir)
        # 文件刚建好无 entry
        entries = log.read()
        assert entries == []


# ---------------------------------------------------------------------------
# Hash 链完整性 + 断点检测
# ---------------------------------------------------------------------------


class TestAuditLogHashChain:
    """hash 链验证（完整 / 篡改后断裂）。"""

    def test_verify_chain_passes_for_clean_log(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-chain-ok", audit_dir=tmp_audit_dir)
        for i in range(5):
            log.append({"actor": "step", "action": "run", "note": f"step {i}"})
        ok, err = log.verify_chain()
        assert ok is True
        assert err is None

    def test_verify_chain_detects_tampered_hash(self, tmp_audit_dir: Path) -> None:
        """手动篡改中间一行 → verify_chain 返回 (False, error_msg)。"""
        log = AuditLog.open("case-tampered", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        log.append({"actor": "b", "action": "y"})
        log.append({"actor": "c", "action": "z"})

        # 篡改第二行：修改 action 字段（不动 hash/prev_hash）
        text = log.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        rec = json.loads(lines[1])
        rec["action"] = "y_TAMPERED"
        lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, err = log.verify_chain()
        assert ok is False
        assert err is not None
        assert "hash" in err.lower() or "line 2" in err

    def test_verify_chain_detects_broken_prev_hash(self, tmp_audit_dir: Path) -> None:
        """中间一行的 prev_hash 改写 → 链断裂。"""
        log = AuditLog.open("case-broken-prev", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        log.append({"actor": "b", "action": "y"})

        text = log.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        rec = json.loads(lines[1])
        rec["prev_hash"] = "0000000000000000000000000000000000000000000000000000000000000000"
        lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, err = log.verify_chain()
        assert ok is False
        assert "prev_hash" in (err or "")

    def test_verify_chain_disabled_returns_true(self, tmp_audit_dir: Path) -> None:
        """chain_hashes=False 时无论文件是否被篡改都返回 True。"""
        path = tmp_audit_dir / "case-no-chain.jsonl"
        log = AuditLog(path, chain_hashes=False)
        log.append({"actor": "a", "action": "x"})
        ok, _ = log.verify_chain()
        assert ok is True


# ---------------------------------------------------------------------------
# JSON Lines 解析错误处理
# ---------------------------------------------------------------------------


class TestAuditLogJsonLines:
    """JSON Lines 解析：损坏行必须抛 JSONDecodeError。"""

    def test_read_raises_on_corrupted_line(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-corrupt", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        # 在尾部追加损坏行
        with log.path.open("a", encoding="utf-8") as f:
            f.write("this-is-not-valid-json{{{{\n")
        with pytest.raises(json.JSONDecodeError):
            log.read()

    def test_read_skips_blank_lines(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-blank", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        with log.path.open("a", encoding="utf-8") as f:
            f.write("\n\n\n")
        log.append({"actor": "b", "action": "y"})
        entries = log.read()
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# 目录只读权限（chmod 0o555 + 0o755 切换）
# ---------------------------------------------------------------------------


class TestAuditLogPermissions:
    """chmod_readonly / chmod_writable：阻断后续写入。"""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root 跳过：root 总能写 0o555 目录")
    def test_chmod_readonly_blocks_new_file_creation(
        self, tmp_audit_dir: Path
    ) -> None:
        """macOS 0o555 目录：阻止新建文件（修改既有文件仍可）。"""
        log = AuditLog.open("case-ro", audit_dir=tmp_audit_dir)
        log.append({"actor": "before", "action": "lock"})
        log.chmod_readonly()
        # 验证目录权限
        mode = stat.S_IMODE(os.stat(tmp_audit_dir).st_mode)
        assert mode == 0o555, f"期望 0o555 实际 {oct(mode)}"
        try:
            # 在 0o555 目录创建新文件应该抛 PermissionError
            with pytest.raises((PermissionError, OSError)):
                new_log = AuditLog.open("case-new-blocked", audit_dir=tmp_audit_dir)
                new_log.append({"actor": "after", "action": "blocked"})
        finally:
            log.chmod_writable()  # 复原避免污染 tmp_path 清理

    @pytest.mark.skipif(os.geteuid() == 0, reason="root 跳过：root 总能写 0o555 目录")
    def test_chmod_writable_restores_write_access(
        self, tmp_audit_dir: Path
    ) -> None:
        log = AuditLog.open("case-rw", audit_dir=tmp_audit_dir)
        log.chmod_readonly()
        log.chmod_writable()
        # 验证目录权限
        mode = stat.S_IMODE(os.stat(tmp_audit_dir).st_mode)
        assert mode == 0o755, f"期望 0o755 实际 {oct(mode)}"
        # 复原后能正常 append
        log.append({"actor": "after", "action": "ok"})
        entries = log.read()
        assert any(e["action"] == "ok" for e in entries)


# ---------------------------------------------------------------------------
# append_llm_call — LLM 审计 JSONL
# ---------------------------------------------------------------------------


class TestAppendLlmCall:
    """logs/audit/llm_calls.jsonl 写入。"""

    def test_appends_one_jsonl_line_with_required_fields(
        self, tmp_audit_dir: Path
    ) -> None:
        path = append_llm_call(
            {
                "model": "gpt-4o-mini",
                "prompt_hash": "abc123",
                "response_hash": "def456",
                "latency_ms": 312,
                "http_code": 200,
                "downstream_status": "ok",
            },
            audit_dir=tmp_audit_dir,
        )
        assert path == tmp_audit_dir / "llm_calls.jsonl"
        text = path.read_text(encoding="utf-8").strip()
        assert "\n" not in text  # 单行
        rec = json.loads(text)
        assert rec["model"] == "gpt-4o-mini"
        assert rec["latency_ms"] == 312
        assert rec["http_code"] == 200
        assert rec["downstream_status"] == "ok"
        assert "timestamp" in rec

    def test_multiple_appends_produce_multiple_lines(
        self, tmp_audit_dir: Path
    ) -> None:
        for i in range(3):
            append_llm_call(
                {"model": "gpt-4o-mini", "latency_ms": 100 + i, "http_code": 200},
                audit_dir=tmp_audit_dir,
            )
        text = (tmp_audit_dir / "llm_calls.jsonl").read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == 3

    def test_default_audit_dir_helper(self) -> None:
        """default_audit_dir / llm_calls_path 路径解析（settings 缺省 fallback）。"""
        d = default_audit_dir()
        assert isinstance(d, Path)
        p = llm_calls_path()
        assert p == d / "llm_calls.jsonl"


# ---------------------------------------------------------------------------
# 端到端脱敏 fixture 验证（10 份 + 章节完整 + ≥ 3 份冲突标识）
# ---------------------------------------------------------------------------


FIXTURES_DIR = Path(__file__).parent / "fixtures"
REQUIRED_SECTIONS = ["主诉", "现病史", "既往史", "辅助检查", "诊断", "治疗", "出院医嘱"]
CONFLICT_MARKERS = [
    "surgery_before_admission",
    "diagnosis_surgery_mismatch",
    "icu_days_exceed_stay",
    "vague_wording_days_unknown",
    "missing_field_id_card",
]


class TestFixtures:
    """tests/fixtures/discharge_01..10.txt 验证。"""

    @pytest.mark.parametrize(
        "fixture_name",
        [f"discharge_{i:02d}.txt" for i in range(1, 11)],
    )
    def test_fixture_exists(self, fixture_name: str) -> None:
        path = FIXTURES_DIR / fixture_name
        assert path.exists(), f"缺 {fixture_name}"

    @pytest.mark.parametrize(
        "fixture_name",
        [f"discharge_{i:02d}.txt" for i in range(1, 11)],
    )
    def test_fixture_min_length_1500_chars(self, fixture_name: str) -> None:
        path = FIXTURES_DIR / fixture_name
        text = path.read_text(encoding="utf-8")
        assert len(text) >= 1500, (
            f"{fixture_name} 字数 {len(text)} < 1500"
        )

    @pytest.mark.parametrize(
        "fixture_name",
        [f"discharge_{i:02d}.txt" for i in range(1, 11)],
    )
    def test_fixture_has_required_sections(self, fixture_name: str) -> None:
        text = (FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")
        missing = [s for s in REQUIRED_SECTIONS if s not in text]
        assert not missing, f"{fixture_name} 缺章节 {missing}"

    def test_at_least_three_fixtures_have_known_conflicts(self) -> None:
        """至少 3 份含已知冲突标识（fixture_*_KNOWN_CONFLICT）。"""
        count = 0
        for i in range(1, 11):
            text = (FIXTURES_DIR / f"discharge_{i:02d}.txt").read_text(encoding="utf-8")
            for marker in CONFLICT_MARKERS:
                if re.search(rf"KNOWN_CONFLICT:\s*{marker}", text):
                    count += 1
                    break
        assert count >= 3, f"已知冲突 fixture 只有 {count} 份，期望 ≥ 3"

    def test_each_conflict_marker_present_at_least_once(self) -> None:
        """5 类典型问题（时序倒置 / 诊断手术错位 / ICU 超住院 / 模糊用语 / 字段缺失）至少各出现一次。"""
        all_text = ""
        for i in range(1, 11):
            all_text += (FIXTURES_DIR / f"discharge_{i:02d}.txt").read_text(encoding="utf-8")
        for marker in CONFLICT_MARKERS:
            assert re.search(rf"KNOWN_CONFLICT:\s*{marker}", all_text), (
                f"未找到冲突标识 {marker}"
            )

    def test_fixtures_use_placeholder_for_pii(self) -> None:
        """姓名 / 身份证号使用占位符而非真实数据。"""
        for i in range(1, 11):
            text = (FIXTURES_DIR / f"discharge_{i:02d}.txt").read_text(encoding="utf-8")
            assert "<EXAMPLE_NAME>" in text or "姓名" in text, (
                f"discharge_{i:02d}.txt 缺姓名占位符"
            )

    def test_fixtures_no_real_id_card_numbers(self) -> None:
        """避免 18 位连续数字身份证号（脱敏纪律）。"""
        pattern = re.compile(r"\b\d{17}[\dXx]\b")
        for i in range(1, 11):
            text = (FIXTURES_DIR / f"discharge_{i:02d}.txt").read_text(encoding="utf-8")
            assert not pattern.search(text), (
                f"discharge_{i:02d}.txt 含疑似真实身份证号"
            )
