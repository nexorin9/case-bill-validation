"""错误码 + 业务异常体系自检（Task 17）。

覆盖：

1. 错误码唯一性 + 类别配额 + 区间合法；
2. 异常继承链 + HTTP 状态映射 + cause chain 透传；
3. ``to_dict()`` 序列化（JSON 兼容 + 非 JSON 值兜底）；
4. ``from_spec`` / ``for_spec`` 工厂用法；
5. 跨模块 ``exception_for_category`` 映射。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from case_bill_validation.error_codes import (
    ALL_CODES,
    ErrorCodeSpec,
    InputErrorCode,
    ModelGatewayErrorCode,
    PersistenceErrorCode,
    PipelineErrorCode,
    WritebackErrorCode,
    assert_codes_unique,
    codes_by_category,
    spec_for,
)
from case_bill_validation.errors import (
    CATEGORY_EXCEPTION_MAP,
    CaseBillError,
    InputError,
    ModelGatewayError,
    PersistenceError,
    PipelineError,
    WritebackError,
    exception_for_category,
)


# ---------------------------------------------------------------------------
# 1) 错误码唯一性 / 类别配额 / 区间合法
# ---------------------------------------------------------------------------


class TestErrorCodesUniqueness:
    def test_assert_codes_unique_passes(self) -> None:
        """全量码唯一；启动期自检通过。"""
        assert_codes_unique()

    def test_no_duplicate_codes(self) -> None:
        """``ALL_CODES`` 中无重复码。"""
        codes = [s.code for s in ALL_CODES]
        assert len(codes) == len(set(codes)), f"重复码: {codes}"

    def test_all_codes_match_cb_regex(self) -> None:
        """全部码匹配 ``CB\\d{3}`` 形式。"""
        import re
        pat = re.compile(r"^CB\d{3}$")
        for spec in ALL_CODES:
            assert pat.match(spec.code), f"非法码格式: {spec.code}"

    def test_all_codes_have_required_fields(self) -> None:
        """每个码有 ``code`` / ``http_status`` / ``default_message`` / ``category``。"""
        for spec in ALL_CODES:
            assert spec.code
            assert isinstance(spec.http_status, int)
            assert 400 <= spec.http_status <= 599, spec
            assert spec.default_message
            assert spec.category in {
                "input",
                "pipeline",
                "writeback",
                "llm",
                "persistence",
            }, spec

    def test_each_category_has_at_least_five_codes(self) -> None:
        """每个类别码数 ≥ 5。"""
        for cat in ("input", "pipeline", "writeback", "llm", "persistence"):
            n = len(codes_by_category(cat))
            assert n >= 5, f"{cat} 仅 {n} 条 (<5)"

    def test_codes_in_correct_range(self) -> None:
        """码前缀 → 类别区间对齐。"""
        ranges = {
            "input": ("CB001", "CB099"),
            "pipeline": ("CB101", "CB199"),
            "writeback": ("CB201", "CB299"),
            "llm": ("CB301", "CB399"),
            "persistence": ("CB401", "CB499"),
        }
        for spec in ALL_CODES:
            lo, hi = ranges[spec.category]
            assert lo <= spec.code <= hi, f"{spec.code} 不在 {lo}-{hi}"


class TestErrorCodeSpecLookup:
    def test_spec_for_known_code(self) -> None:
        spec = spec_for("CB001")
        assert spec.code == "CB001"
        assert spec.http_status == 400

    def test_spec_for_unknown_code_raises(self) -> None:
        with pytest.raises(KeyError):
            spec_for("CB999")

    def test_codes_by_category_returns_only_matching(self) -> None:
        input_codes = codes_by_category("input")
        assert all(s.category == "input" for s in input_codes)
        assert InputErrorCode.CB001 in input_codes


# ---------------------------------------------------------------------------
# 2) 异常继承链 + HTTP 状态映射
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    @pytest.mark.parametrize(
        "exc_cls,spec",
        [
            (InputError, InputErrorCode.CB001),
            (PipelineError, PipelineErrorCode.CB101),
            (WritebackError, WritebackErrorCode.CB201),
            (ModelGatewayError, ModelGatewayErrorCode.CB301),
            (PersistenceError, PersistenceErrorCode.CB401),
        ],
    )
    def test_subclass_of_casebillerror(
        self, exc_cls: type[CaseBillError], spec: ErrorCodeSpec
    ) -> None:
        assert issubclass(exc_cls, CaseBillError)
        assert issubclass(exc_cls, Exception)

    @pytest.mark.parametrize(
        "exc_cls,spec",
        [
            (InputError, InputErrorCode.CB001),
            (PipelineError, PipelineErrorCode.CB101),
            (WritebackError, WritebackErrorCode.CB201),
            (ModelGatewayError, ModelGatewayErrorCode.CB301),
            (PersistenceError, PersistenceErrorCode.CB401),
        ],
    )
    def test_default_class_attributes_match_spec(
        self, exc_cls: type[CaseBillError], spec: ErrorCodeSpec
    ) -> None:
        """类属性 ``error_code`` / ``http_status`` / ``message`` 与 spec 对齐。"""
        assert exc_cls.error_code == spec.code
        assert exc_cls.http_status == spec.http_status
        assert exc_cls.message == spec.default_message

    def test_raise_and_catch_by_base_class(self) -> None:
        """抛出 InputError 可被 CaseBillError 捕获。"""
        with pytest.raises(CaseBillError) as exc_info:
            raise InputError("bad input")
        assert exc_info.value.error_code == "CB001"

    def test_raise_and_catch_by_builtin_exception(self) -> None:
        """抛出 ModelGatewayError 仍是 Exception 子类。"""
        with pytest.raises(Exception):
            raise ModelGatewayError("LLM down")


class TestHttpStatusMapping:
    @pytest.mark.parametrize(
        "exc_cls,expected_status",
        [
            (InputError, 400),
            (PipelineError, 500),
            (WritebackError, 502),
            (ModelGatewayError, 502),
            (PersistenceError, 500),
        ],
    )
    def test_default_http_status_per_subclass(
        self, exc_cls: type[CaseBillError], expected_status: int
    ) -> None:
        e = exc_cls("test")
        assert e.http_status == expected_status

    def test_http_status_override_on_raise(self) -> None:
        e = WritebackError("本地占位", http_status=503)
        assert e.http_status == 503
        assert e.message == "本地占位"


# ---------------------------------------------------------------------------
# 3) cause chain 透传 / 序列化
# ---------------------------------------------------------------------------


class TestCauseChain:
    def test_cause_passed_through(self) -> None:
        root = ValueError("root")
        e = InputError("wrap", cause=root)
        assert e.cause is root

    def test_cause_serialized_as_dict(self) -> None:
        """单层 cause → ``{"message", "cause": None}`` 字典（统一格式）。"""
        e = InputError("wrap", cause=ValueError("root cause msg"))
        d = e.to_dict()
        assert d["cause"] == {"message": "root cause msg", "cause": None}

    def test_cause_none_serialized_as_none(self) -> None:
        e = PipelineError("no cause")
        d = e.to_dict()
        assert d["cause"] is None

    def test_nested_cause_serialized_recursively(self) -> None:
        """三层 cause 链 → 嵌套 dict；最深层 ``cause: None``。"""
        deepest = RuntimeError("level3")
        middle = ValueError("level2")
        middle.__cause__ = deepest
        top = InputError("level1", cause=middle)
        d = top.to_dict()
        assert isinstance(d["cause"], dict)
        assert d["cause"]["message"] == "level2"
        # 中间层 cause 是最深层的 dict，最深层 cause=None
        assert d["cause"]["cause"] == {"message": "level3", "cause": None}

    def test_cause_chain_cycle_does_not_loop(self) -> None:
        """自引用 cause 链：截断，不抛 RecursionError。"""
        a = ValueError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a  # 循环
        e = PipelineError("top", cause=a)
        d = e.to_dict()
        assert isinstance(d, dict)
        # 截断后最终仍有 dict 结构（深度受限 + cycle break）
        cur: Any = d["cause"]
        depth = 0
        while isinstance(cur, dict) and depth < 32:
            cur = cur.get("cause")
            depth += 1
        assert cur is None  # 最终落到 None（截断成功）


# ---------------------------------------------------------------------------
# 4) to_dict() / JSON round-trip / 字段兜底
# ---------------------------------------------------------------------------


class TestToDictSerialization:
    def test_to_dict_field_set(self) -> None:
        e = InputError("m", details={"a": 1, "b": "two"})
        d = e.to_dict()
        assert set(d.keys()) == {"error_code", "http_status", "message", "details", "cause"}

    def test_to_dict_is_json_compatible(self) -> None:
        e = WritebackError("m", details={"attempts": 3, "url": "http://x"})
        # 嵌套 dict / list / int / str / None 都应可序列化
        d = e.to_dict()
        s = json.dumps(d, ensure_ascii=False)
        parsed = json.loads(s)
        assert parsed["error_code"] == "CB201"
        assert parsed["details"]["attempts"] == 3

    def test_non_json_details_coerced_to_string(self) -> None:
        """非 JSON 兼容的 details 值（对象）兜底为字符串。"""
        class NotJson:
            def __str__(self) -> str:
                return "not_json_str"

        e = InputError("m", details={"obj": NotJson()})
        d = e.to_dict()
        assert d["details"]["obj"] == "not_json_str"

    def test_details_none_becomes_empty_dict(self) -> None:
        e = ModelGatewayError("m")
        assert e.to_dict()["details"] == {}

    def test_no_args_uses_class_defaults(self) -> None:
        e = PersistenceError()
        assert e.error_code == "CB401"
        assert e.http_status == 500
        assert e.message == "SQLite 写失败（billing_fields.db 锁或 IO 错）"

    def test_message_override_preserved(self) -> None:
        e = PipelineError("自定义中文描述")
        assert e.message == "自定义中文描述"
        assert e.to_dict()["message"] == "自定义中文描述"


# ---------------------------------------------------------------------------
# 5) for_spec 工厂 + exception_for_category
# ---------------------------------------------------------------------------


class TestForSpecFactory:
    def test_casebillerror_from_spec(self) -> None:
        spec = ModelGatewayErrorCode.CB303
        e = CaseBillError.from_spec(spec, details={"attempt": 1})
        assert e.error_code == "CB303"
        assert e.http_status == 504
        assert e.message == spec.default_message
        assert isinstance(e, ModelGatewayError)  # 仍是子类

    def test_input_error_for_spec(self) -> None:
        e = InputError.for_spec(InputErrorCode.CB006, message="span 越界")
        assert e.error_code == "CB006"
        assert e.http_status == 422
        assert e.message == "span 越界"

    def test_for_spec_with_http_status_override(self) -> None:
        e = WritebackError.for_spec(
            WritebackErrorCode.CB204, http_status=503
        )
        assert e.http_status == 503

    def test_for_spec_preserves_cause(self) -> None:
        root = OSError("disk full")
        e = PersistenceError.for_spec(
            PersistenceErrorCode.CB405, cause=root
        )
        assert e.cause is root
        assert e.to_dict()["cause"] == {"message": "disk full", "cause": None}


class TestCategoryMapping:
    @pytest.mark.parametrize(
        "category,expected_cls",
        [
            ("input", InputError),
            ("pipeline", PipelineError),
            ("writeback", WritebackError),
            ("llm", ModelGatewayError),
            ("persistence", PersistenceError),
        ],
    )
    def test_exception_for_category(
        self, category: str, expected_cls: type[CaseBillError]
    ) -> None:
        cls = exception_for_category(category)
        assert cls is expected_cls

    def test_unknown_category_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            exception_for_category("unknown_cat")

    def test_category_map_completeness(self) -> None:
        """``CATEGORY_EXCEPTION_MAP`` 含全部 5 个类别。"""
        assert set(CATEGORY_EXCEPTION_MAP.keys()) == {
            "input", "pipeline", "writeback", "llm", "persistence",
        }
        for cls in CATEGORY_EXCEPTION_MAP.values():
            assert issubclass(cls, CaseBillError)


# ---------------------------------------------------------------------------
# 6) raise + str + repr
# ---------------------------------------------------------------------------


class TestStrRepr:
    def test_str_is_message(self) -> None:
        e = InputError("中文描述")
        assert str(e) == "中文描述"

    def test_repr_contains_error_code(self) -> None:
        e = PipelineError("m", error_code="CB102")
        r = repr(e)
        assert "CB102" in r
        assert "PipelineError" in r

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(CaseBillError) as info:
            raise ModelGatewayError(
                "down",
                error_code="CB302",
                details={"latency_ms": 12000},
            )
        assert info.value.error_code == "CB302"
        assert info.value.details["latency_ms"] == 12000
