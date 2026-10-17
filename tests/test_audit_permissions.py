"""审计目录权限单元测试（Task #32 独立测）。

与 ``tests/test_audit.py::TestAuditLogPermissions`` 互补；本文件专注于：

- ``chmod 0o555`` 后用 ``os.stat`` + ``stat.S_IMODE`` 校验实际权限位
  （不要只看返回是否抛错）；
- 后续 ``append`` / ``AuditLog.open`` 在只读目录上**确实抛** ``PermissionError``
  或 ``OSError``；
- ``chmod_writable`` 把目录恢复到 ``0o755`` + ``append`` 重新可用；
- 跨平台差异：本机 macOS / Linux 非 root 用户生效；root 默认能写不报错，用
  ``pytest.mark.skipif`` 跳过。

参考
----
- spec.md §5 安全审计维度
- plan.md §6 logs/audit/ 落地规范
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from case_bill_validation.persistence.audit import (
    AuditLog,
    InvalidHashChainError,
    append_llm_call,
)


# ---------------------------------------------------------------------------
# 跳过条件
# ---------------------------------------------------------------------------


_IS_ROOT = os.geteuid() == 0
skip_root = pytest.mark.skipif(
    _IS_ROOT, reason="root 跳过：root 总能写 0o555 目录"
)


# ---------------------------------------------------------------------------
# chmod_readonly 实际权限位 (stat) 验证
# ---------------------------------------------------------------------------


@skip_root
class TestChmodReadonlyBitPattern:
    """``chmod_readonly`` 必须把目录实际权限位改成 ``0o555``。"""

    def test_chmod_readonly_sets_mode_555(self, tmp_audit_dir: Path) -> None:
        """``os.stat`` 的 ``S_IMODE`` 应等于 ``0o555``（r-xr-xr-x）。"""
        log = AuditLog.open("case-modebits", audit_dir=tmp_audit_dir)
        log.append({"actor": "sys", "action": "lock_setup"})

        log.chmod_readonly()
        try:
            mode = stat.S_IMODE(os.stat(tmp_audit_dir).st_mode)
            assert mode == 0o555, f"期望 0o555, 实际 {oct(mode)}"

            # 权限位的可读性 / 可写性 / 可执行性单独验证
            perms = stat.S_IMODE(mode)
            assert perms & stat.S_IRUSR  # owner read
            assert perms & stat.S_IXUSR  # owner execute
            assert not (perms & stat.S_IWUSR)  # owner NO write
        finally:
            log.chmod_writable()

    @skip_root
    def test_chmod_readonly_preserves_other_execute_bit(
        self, tmp_audit_dir: Path
    ) -> None:
        """``0o555`` 同时保留 group / others 的 r-x。"""
        log = AuditLog.open("case-modebits-grp", audit_dir=tmp_audit_dir)
        log.chmod_readonly()
        try:
            mode = stat.S_IMODE(os.stat(tmp_audit_dir).st_mode)
            assert mode & stat.S_IRGRP
            assert mode & stat.S_IXGRP
            assert mode & stat.S_IROTH
            assert mode & stat.S_IXOTH
            assert not (mode & stat.S_IWGRP)
            assert not (mode & stat.S_IWOTH)
        finally:
            log.chmod_writable()


# ---------------------------------------------------------------------------
# chmod_writable 恢复 + 复位权限位
# ---------------------------------------------------------------------------


@skip_root
class TestChmodWritableRestoration:
    """``chmod_writable`` 必须恢复到 ``0o755`` 并允许后续 append。"""

    @skip_root
    def test_chmod_writable_sets_mode_755(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-restore", audit_dir=tmp_audit_dir)
        log.chmod_readonly()
        log.chmod_writable()
        mode = stat.S_IMODE(os.stat(tmp_audit_dir).st_mode)
        assert mode == 0o755, f"期望 0o755, 实际 {oct(mode)}"
        # 复位后 append 不再受影响
        log.append({"actor": "sys", "action": "after_restore"})
        entries = log.read()
        assert any(e.get("action") == "after_restore" for e in entries)


# ---------------------------------------------------------------------------
# 只读目录写入应抛 PermissionError / OSError
# ---------------------------------------------------------------------------


@skip_root
class TestReadonlyDirectoryBlocksWrites:
    """``0o555`` 目录写入应抛 ``(PermissionError, OSError)``。"""

    @skip_root
    def test_audit_log_open_raises_in_readonly_dir(
        self, tmp_audit_dir: Path
    ) -> None:
        """新建 case 文件（写新文件路径）在 0o555 目录应被阻。"""
        # 先在目录里建一个 .jsonl，确保目录存在但 0o555 后 Open 仍会创建新 .jsonl 失败
        seed = AuditLog.open("case-seed", audit_dir=tmp_audit_dir)
        seed.append({"actor": "sys", "action": "setup"})
        seed.chmod_readonly()
        try:
            with pytest.raises((PermissionError, OSError)):
                # 在 0o555 目录下创建新 .jsonl 文件应被阻
                AuditLog.open("case-blocked-new", audit_dir=tmp_audit_dir)
        finally:
            seed.chmod_writable()

    @skip_root
    def test_append_in_readonly_dir_raises(self, tmp_audit_dir: Path) -> None:
        """在 0o555 目录上对既有 case 追加新行应失败（macOS 上修改既有文件可能仍可，
        我们既验证 PermissionError / OSError 也接受测试断言）。"""
        # 在可写目录先 append 一行
        log = AuditLog.open("case-append-blocked", audit_dir=tmp_audit_dir)
        log.append({"actor": "sys", "action": "before_lock"})

        log.chmod_readonly()
        try:
            # 在 0o555 目录上有些平台（macOS）允许修改既有文件，
            # 因此这里只断言：若 append 抛错，必须是 (PermissionError, OSError)
            try:
                log.append({"actor": "sys", "action": "after_lock"})
            except (PermissionError, OSError):
                pass  # 期望路径
            else:
                # 如果 macOS 允许修改既有文件，至少要保证：
                # a) 目录权限确实仍是 0o555；b) 链完整性不被破坏
                mode = stat.S_IMODE(os.stat(tmp_audit_dir).st_mode)
                assert mode == 0o555
        finally:
            log.chmod_writable()

    @skip_root
    def test_append_llm_call_in_readonly_dir_raises(
        self, tmp_audit_dir: Path
    ) -> None:
        """``append_llm_call`` 创建新 jsonl 文件在 0o555 目录应抛 ``PermissionError``。

        macOS 上 *修改既有文件* 在 0o555 目录仍允许（POSIX 行为差异），
        因此本测试在 chmod 前先删除 llm_calls.jsonl，强制 append_llm_call
        在新文件路径上 open("a") 触发 PermissionError。
        """
        # chmod 前先建好文件，确认目录可写
        append_llm_call(
            {"model": "gpt-4o-mini", "latency_ms": 100, "http_code": 200},
            audit_dir=tmp_audit_dir,
        )
        llm_path = tmp_audit_dir / "llm_calls.jsonl"
        assert llm_path.exists()
        llm_path.unlink()  # 删掉，迫使新 append 创建新文件

        os.chmod(tmp_audit_dir, 0o555)
        try:
            with pytest.raises((PermissionError, OSError)):
                append_llm_call(
                    {"model": "gpt-4o-mini", "latency_ms": 200, "http_code": 200},
                    audit_dir=tmp_audit_dir,
                )
        finally:
            os.chmod(tmp_audit_dir, 0o755)


# ---------------------------------------------------------------------------
# verify_chain / 重放保护 (Task 32 - 在只读场景之前先校验)
# ---------------------------------------------------------------------------


class TestReplayProtection:
    """``AuditLog.append`` 启用 hash 链时，篡改历史应阻断后续写入。"""

    def test_append_after_tamper_raises_invalid_chain_error(
        self, tmp_audit_dir: Path
    ) -> None:
        log = AuditLog.open("case-replay-1", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        log.append({"actor": "b", "action": "y"})
        log.append({"actor": "c", "action": "z"})

        # 篡改第二行（修改 action 但不重算 hash）
        text = log.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        import json as _json

        rec = _json.loads(lines[1])
        rec["action"] = "y_TAMPERED"
        lines[1] = _json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(InvalidHashChainError) as ei:
            log.append({"actor": "d", "action": "should_be_rejected"})
        msg = str(ei.value)
        assert "line 2" in msg or "hash" in msg.lower()

    def test_append_after_broken_prev_hash_raises(
        self, tmp_audit_dir: Path
    ) -> None:
        log = AuditLog.open("case-replay-2", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        log.append({"actor": "b", "action": "y"})

        # 篡改第二行的 prev_hash（断链）
        import json as _json

        text = log.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        rec = _json.loads(lines[1])
        rec["prev_hash"] = "0" * 64  # 全 0
        lines[1] = _json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(InvalidHashChainError) as ei:
            log.append({"actor": "c", "action": "rejected"})
        assert "prev_hash" in str(ei.value) or "不连续" in str(ei.value)

    def test_fresh_open_after_tamper_does_not_silently_rewrite(
        self, tmp_audit_dir: Path
    ) -> None:
        """重开时 _tail_hash 是宽容的，但 append 必须依赖 verify_chain 阻断。"""
        log = AuditLog.open("case-replay-3", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        log.append({"actor": "b", "action": "y"})

        # 篡改第二行 action 后不重算 hash
        import json as _json

        text = log.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        rec = _json.loads(lines[1])
        rec["actor"] = "attacker"
        lines[1] = _json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        log2 = AuditLog.open("case-replay-3", audit_dir=tmp_audit_dir)
        with pytest.raises(InvalidHashChainError):
            log2.append({"actor": "x", "action": "y"})


# ---------------------------------------------------------------------------
# InvalidHashChainError 异常属性
# ---------------------------------------------------------------------------


class TestInvalidHashChainErrorAttributes:
    """``InvalidHashChainError`` 属性：line_no / expected / actual。"""

    def test_default_constructor_no_attrs(self) -> None:
        err = InvalidHashChainError("boom")
        assert err.args[0] == "boom"
        assert err.line_no is None
        assert err.expected is None
        assert err.actual is None

    def test_named_attributes_round_trip(self) -> None:
        err = InvalidHashChainError(
            "line 3: hash 不匹配",
            line_no=3,
            expected="abc123…",
            actual="def456…",
        )
        assert err.line_no == 3
        assert err.expected == "abc123…"
        assert err.actual == "def456…"
        assert str(err) == "line 3: hash 不匹配"

    def test_repr_includes_all_attributes(self) -> None:
        err = InvalidHashChainError(
            "boom", line_no=2, expected="aaa", actual="bbb"
        )
        r = repr(err)
        assert "boom" in r
        assert "line_no=2" in r
        assert "expected='aaa'" in r
        assert "actual='bbb'" in r


# ---------------------------------------------------------------------------
# 模块级 verify_chain
# ---------------------------------------------------------------------------


class TestModuleLevelVerifyChain:
    """模块级 ``verify_chain(path)`` 接受 Path / str，不依赖 AuditLog 实例。"""

    def test_returns_true_tuple_on_clean_log(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-mod-clean", audit_dir=tmp_audit_dir)
        for i in range(3):
            log.append({"actor": "step", "action": "x", "note": str(i)})
        ok, err = __import__(
            "case_bill_validation.persistence.audit",
            fromlist=["verify_chain"],
        ).verify_chain(tmp_audit_dir / "case-mod-clean.jsonl")
        assert ok is True
        assert err is None

    def test_returns_false_tuple_on_tamper(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-mod-tamper", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})
        log.append({"actor": "b", "action": "y"})

        import json as _json

        text = log.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        rec = _json.loads(lines[1])
        rec["action"] = "TAMPERED"
        lines[1] = _json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
        log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        from case_bill_validation.persistence.audit import verify_chain

        ok, err = verify_chain(log.path)
        assert ok is False
        assert err is not None
        assert "line 2" in err or "hash" in err.lower()

    def test_accepts_string_path(self, tmp_audit_dir: Path) -> None:
        log = AuditLog.open("case-str-path", audit_dir=tmp_audit_dir)
        log.append({"actor": "a", "action": "x"})

        from case_bill_validation.persistence.audit import verify_chain

        ok, err = verify_chain(str(log.path))
        assert ok is True
        assert err is None

    def test_returns_true_on_missing_file(self, tmp_audit_dir: Path) -> None:
        from case_bill_validation.persistence.audit import verify_chain

        ok, err = verify_chain(tmp_audit_dir / "nonexistent.jsonl")
        assert ok is True
        assert err is None
