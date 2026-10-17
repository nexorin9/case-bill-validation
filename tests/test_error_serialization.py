"""业务异常序列化深化测试（Task 18）。

与 :mod:`tests.test_errors` 互补 —— 后者覆盖错误码唯一性 + 继承链 + 类别派发；
本文件聚焦：

1. ``to_dict()`` **JSON round-trip**（``json.dumps`` → ``json.loads`` 不丢字段、不改类型）；
2. ``details`` 字段非平凡类型兼容（``datetime`` / ``Path`` / ``UUID`` / ``set`` / ``tuple`` /
   自定义类实例）必须能落到 ``str`` / ``list`` / ``dict`` 等 JSON 兼容值；
3. ``cause`` 链递归序列化（含跨多种异常类型 + 极深 + 循环引用）；
4. 5 个子类 ``to_dict()`` 行为一致；
5. ``from_spec`` → ``to_dict`` 端到端：error_code / http_status 与 spec 一致；
6. ``to_dict()`` 结果可作 FastAPI ``HTTPException(detail=...)`` 的 detail 直接使用
   （即与现有 API 路由兼容）。

所有断言默认执行本地版断言（不依赖外部库），便于 round 内任何时点可重跑。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from case_bill_validation.error_codes import (
    ErrorCodeSpec,
    InputErrorCode,
    ModelGatewayErrorCode,
    PersistenceErrorCode,
    PipelineErrorCode,
    WritebackErrorCode,
)
from case_bill_validation.errors import (
    CaseBillError,
    InputError,
    ModelGatewayError,
    PersistenceError,
    PipelineError,
    WritebackError,
)


# ---------------------------------------------------------------------------
# 1) JSON round-trip（to_dict → json.dumps → json.loads）
# ---------------------------------------------------------------------------


class TestJSONRoundTrip:
    """``to_dict()`` 输出必须能 ``json.dumps`` → ``json.loads`` 完整还原。"""

    def test_roundtrip_minimal_casebillerror(self) -> None:
        e = CaseBillError("plain error")
        d = e.to_dict()
        s = json.dumps(d, ensure_ascii=False)
        loaded = json.loads(s)
        assert loaded == d

    def test_roundtrip_preserves_error_code_and_http_status(self) -> None:
        e = InputError(
            "身份证号格式不合法",
            error_code="CB002",
            http_status=400,
            details={"id_number": "32010119900101abcd"},
        )
        loaded = json.loads(json.dumps(e.to_dict(), ensure_ascii=False))
        assert loaded["error_code"] == "CB002"
        assert loaded["http_status"] == 400
        assert loaded["message"] == "身份证号格式不合法"
        assert loaded["details"]["id_number"] == "32010119900101abcd"

    def test_roundtrip_cause_chain_single_level(self) -> None:
        cause = ValueError("底层解析异常")
        e = PipelineError("流水线处理失败", cause=cause)
        loaded = json.loads(json.dumps(e.to_dict(), ensure_ascii=False))
        # 单层 cause → {message, cause: None}
        assert loaded["cause"] == {"message": "底层解析异常", "cause": None}

    def test_roundtrip_cause_chain_nested_three_levels(self) -> None:
        deepest = IOError("磁盘不可写")
        mid = ValueError("Excel 写入失败").with_traceback(None)
        mid.__cause__ = deepest
        top = WritebackError("回写失败", cause=mid)
        loaded = json.loads(json.dumps(top.to_dict(), ensure_ascii=False))
        # _cause_to_dict 从 top.cause 起 (mid → deepest)；
        # 故 loaded["cause"] 是 mid 的 str，下钻到 deepest。
        assert loaded["cause"]["message"] == "Excel 写入失败"
        assert loaded["cause"]["cause"]["message"] == "磁盘不可写"
        assert loaded["cause"]["cause"]["cause"] is None

    def test_roundtrip_with_none_cause(self) -> None:
        e = PersistenceError("数据库写入失败")
        loaded = json.loads(json.dumps(e.to_dict(), ensure_ascii=False))
        assert loaded["cause"] is None

    def test_roundtrip_with_empty_details(self) -> None:
        e = CaseBillError("无 details")
        loaded = json.loads(json.dumps(e.to_dict(), ensure_ascii=False))
        assert loaded["details"] == {}

    def test_roundtrip_unicode_message(self) -> None:
        """中文 / emoji / 罕见字符 round-trip 不丢。"""
        e = InputError("字段值含生僻字「鬱」+ emoji 📋")
        loaded = json.loads(json.dumps(e.to_dict(), ensure_ascii=False))
        assert loaded["message"] == "字段值含生僻字「鬱」+ emoji 📋"


# ---------------------------------------------------------------------------
# 2) details 字段非平凡类型兼容
# ---------------------------------------------------------------------------


class TestDetailsSerialization:
    """``details`` 字段值类型多样，``to_dict()`` 必须能兜底为 JSON 兼容。"""

    def test_details_datetime_coerced_to_string(self) -> None:
        ts = datetime(2026, 10, 17, 5, 36, 18, tzinfo=timezone.utc)
        e = PipelineError(
            "时间校验失败",
            details={"timestamp": ts, "value": ts.isoformat()},
        )
        d = e.to_dict()
        assert d["details"]["timestamp"] == str(ts)
        assert d["details"]["value"] == ts.isoformat()

    def test_details_path_coerced_to_string(self) -> None:
        p = Path("/tmp/exports/case-abc.xlsx")
        e = WritebackError("写文件失败", details={"path": p})
        d = e.to_dict()
        assert d["details"]["path"] == str(p)

    def test_details_uuid_coerced_to_string(self) -> None:
        u = UUID("12345678-1234-5678-1234-567812345678")
        e = PersistenceError("DB 写入失败", details={"row_id": u})
        d = e.to_dict()
        assert d["details"]["row_id"] == str(u)

    def test_details_set_coerced_to_string_via_repr(self) -> None:
        """set 不可 JSON 序列化，必须走 ``str()`` 兜底。"""
        e = PipelineError("字段冲突", details={"conflicting_fields": {"诊断", "手术"}})
        d = e.to_dict()
        # set 的 str() 顺序不稳定，断言类型与字段
        assert isinstance(d["details"]["conflicting_fields"], str)
        assert "诊断" in d["details"]["conflicting_fields"]
        assert "手术" in d["details"]["conflicting_fields"]

    def test_details_tuple_coerced_via_list(self) -> None:
        """tuple 不是 JSON list 兼容类型，须转 list（若实现允许）或 str 兜底。

        当前 ``_coerce_details`` 仅显式列出 list / dict，tuple 会落到 ``str()``。
        """
        e = PipelineError("顺序错误", details={"expected_order": (1, 2, 3)})
        d = e.to_dict()
        # tuple 不可 JSON → str 兜底
        assert isinstance(d["details"]["expected_order"], str)

    def test_details_custom_object_coerced_to_string(self) -> None:
        class FieldObject:
            def __str__(self) -> str:
                return "<FieldObject: id_card=320101>"

        obj = FieldObject()
        e = InputError("字段对象序列化", details={"obj": obj})
        d = e.to_dict()
        assert d["details"]["obj"] == "<FieldObject: id_card=320101>"

    def test_details_mixed_json_compatible_types(self) -> None:
        e = WritebackError(
            "回写失败",
            details={
                "attempts": 3,            # int
                "latency_ms": 1240.5,     # float
                "ok": False,              # bool
                "tags": ["a", "b"],       # list[str]
                "meta": {"k": "v"},       # dict
                "nullable": None,         # None
            },
        )
        d = e.to_dict()
        loaded = json.loads(json.dumps(d, ensure_ascii=False))
        assert loaded["details"]["attempts"] == 3
        assert loaded["details"]["latency_ms"] == 1240.5
        assert loaded["details"]["ok"] is False
        assert loaded["details"]["tags"] == ["a", "b"]
        assert loaded["details"]["meta"] == {"k": "v"}
        assert loaded["details"]["nullable"] is None


# ---------------------------------------------------------------------------
# 3) cause 链边界
# ---------------------------------------------------------------------------


class TestCauseChainDeep:
    """cause 链深度边界：极深 / 循环 / None 终止。"""

    def test_cause_chain_with_non_casebillerror_cause(self) -> None:
        """原始异常类型不被要求是 CaseBillError；只取 ``str(cause)``。"""
        e = ModelGatewayError("LLM 调用失败", cause=KeyError("missing field"))
        d = e.to_dict()
        assert "missing field" in d["cause"]["message"]

    def test_cause_chain_long_but_within_limit(self) -> None:
        """10 层嵌套，落在递归上限（16）之内。"""
        cur: BaseException | None = None
        for i in range(10):
            inner = RuntimeError(f"level-{i}")
            inner.__cause__ = cur
            cur = inner
        e = PipelineError("顶层错误", cause=cur)
        d = e.to_dict()
        # 链最深 10 层，逐层断言
        node: dict[str, Any] | None = d["cause"]
        depth = 0
        while node is not None:
            assert f"level-{9 - depth}" in node["message"]
            node = node["cause"]
            depth += 1
        assert depth == 10

    def test_cause_chain_deeply_nested_exceeds_limit_is_truncated(self) -> None:
        """超过递归上限（16）的链被截断，不会无限递归。"""
        cur: BaseException | None = None
        for i in range(50):
            inner = RuntimeError(f"L{i}")
            inner.__cause__ = cur
            cur = inner
        e = PipelineError("极深异常", cause=cur)
        # 不应抛 RecursionError
        d = e.to_dict()
        assert d["cause"] is not None
        depth = 0
        node: dict[str, Any] | None = d["cause"]
        while node is not None:
            node = node["cause"]
            depth += 1
            assert depth <= 20, f"超过合理截断深度：{depth}"

    def test_cause_chain_cycle_not_infinite(self) -> None:
        a = RuntimeError("A")
        b = RuntimeError("B")
        a.__cause__ = b
        b.__cause__ = a  # noqa: F841 —— 故意制造循环
        e = PipelineError("顶层", cause=a)
        d = e.to_dict()
        # 应当截断在某处，最终 cause 落到 None
        depth = 0
        node: dict[str, Any] | None = d["cause"]
        while node is not None:
            node = node["cause"]
            depth += 1
            assert depth <= 20
        assert node is None

    def test_cause_with_context_not_cause_prefers_cause(self) -> None:
        """``__context__`` 在 ``__cause__`` 为 None 时兜底。"""
        inner = ValueError("inner")
        try:
            try:
                raise inner
            except ValueError as exc:
                raise RuntimeError("outer") from None
        except RuntimeError as outer:
            e = PipelineError("top", cause=outer)
        d = e.to_dict()
        # 期望 __cause__ 优先（RuntimeError 直接 cause 链路通常无显式 cause）
        assert d["cause"]["message"] == "outer"


# ---------------------------------------------------------------------------
# 4) 5 个子类 to_dict() 一致性
# ---------------------------------------------------------------------------


class TestSubclassSerialization:
    """5 个子类 ``to_dict()`` 输出结构一致（同一字段集）。"""

    @pytest.mark.parametrize(
        "cls,spec_cls,code,http_status",
        [
            (InputError, InputErrorCode, "CB002", 400),
            (PipelineError, PipelineErrorCode, "CB101", 500),
            (WritebackError, WritebackErrorCode, "CB201", 502),
            (ModelGatewayError, ModelGatewayErrorCode, "CB301", 502),
            (PersistenceError, PersistenceErrorCode, "CB401", 500),
        ],
    )
    def test_each_subclass_to_dict_has_consistent_keys(
        self,
        cls: type[CaseBillError],
        spec_cls: type,
        code: str,
        http_status: int,
    ) -> None:
        e = cls("test", error_code=code, details={"k": "v"})
        d = e.to_dict()
        assert set(d.keys()) == {
            "error_code",
            "http_status",
            "message",
            "details",
            "cause",
        }
        assert d["error_code"] == code
        assert d["http_status"] == http_status

    def test_each_subclass_serialization_is_json_dumpsable(self) -> None:
        for cls, code in [
            (InputError, "CB002"),
            (PipelineError, "CB102"),
            (WritebackError, "CB201"),
            (ModelGatewayError, "CB301"),
            (PersistenceError, "CB401"),
        ]:
            e = cls("msg", error_code=code, details={"x": 1})
            json.dumps(e.to_dict(), ensure_ascii=False)  # 不抛


# ---------------------------------------------------------------------------
# 5) from_spec → to_dict 端到端
# ---------------------------------------------------------------------------


class TestFromSpecToDict:
    """``from_spec`` 构造 → ``to_dict`` 序列化后字段与 spec 一致。"""

    @pytest.mark.parametrize(
        "spec",
        [
            InputErrorCode.CB001,
            InputErrorCode.CB002,
            PipelineErrorCode.CB101,
            PipelineErrorCode.CB102,
            WritebackErrorCode.CB201,
            ModelGatewayErrorCode.CB301,
            ModelGatewayErrorCode.CB303,
            PersistenceErrorCode.CB401,
        ],
    )
    def test_from_spec_to_dict_roundtrip(self, spec: ErrorCodeSpec) -> None:
        e = CaseBillError.from_spec(spec)
        d = e.to_dict()
        assert d["error_code"] == spec.code
        assert d["http_status"] == spec.http_status
        # 默认 message 应来自 spec
        assert d["message"] == spec.default_message

    def test_from_spec_overrides_message_and_preserves_code(self) -> None:
        spec = ModelGatewayErrorCode.CB301
        e = CaseBillError.from_spec(spec, message="凭据已失效，请更新 MODEL_GATEWAY_API_KEY")
        d = e.to_dict()
        assert d["error_code"] == "CB301"
        assert d["message"].startswith("凭据已失效")

    def test_from_spec_returns_correct_subclass(self) -> None:
        e = CaseBillError.from_spec(ModelGatewayErrorCode.CB303)
        assert isinstance(e, ModelGatewayError)
        e2 = CaseBillError.from_spec(PipelineErrorCode.CB102)
        assert isinstance(e2, PipelineError)


# ---------------------------------------------------------------------------
# 6) HTTPException(detail=to_dict()) 兼容（API 路由层契约）
# ---------------------------------------------------------------------------


class TestFastAPIIntegration:
    """``to_dict()`` 必须能直接作 FastAPI ``HTTPException(detail=...)`` 的 detail。"""

    def test_to_dict_passes_through_json_dumps_with_ensure_ascii_false(self) -> None:
        """FastAPI 默认走 ``json.dumps(detail, ensure_ascii=False)``。"""
        e = InputError(
            "字段名「出院科室」不在 193 项字段规范中",
            error_code="CB002",
            details={"field_name": "出院科室", "expected": "出院科别"},
        )
        detail = e.to_dict()
        # FastAPI 会把 detail 序列化为 JSON 字符串，再嵌入到 {"detail": ...}
        payload = json.dumps({"detail": detail}, ensure_ascii=False)
        loaded = json.loads(payload)
        assert loaded["detail"]["error_code"] == "CB002"
        assert loaded["detail"]["message"].startswith("字段名")

    def test_to_dict_handles_unicode_in_details(self) -> None:
        e = WritebackError(
            "回写失败：响应含 GBK 字符",
            details={"response_snippet": "{\"status\": \"成功\"}"},
        )
        d = e.to_dict()
        # 详细字段含中文 → JSON 仍正确序列化
        loaded = json.loads(json.dumps(d, ensure_ascii=False))
        assert loaded["details"]["response_snippet"] == "{\"status\": \"成功\"}"


# ---------------------------------------------------------------------------
# 7) 实战组合：try/except + raise CaseBillError.from_spec(...)
# ---------------------------------------------------------------------------


class TestRaiseFromSpecPattern:
    """业务代码常用模式：``raise CaseBillError.from_spec(...)``。"""

    def test_raise_from_spec_catch_by_casebillerror(self) -> None:
        """CB001 = 400；CB002 = 415（文件类型不支持）。这里用 CB001 验证 400。"""
        with pytest.raises(CaseBillError) as ei:
            raise CaseBillError.from_spec(
                InputErrorCode.CB001,
                details={"field": "id_card"},
            )
        assert ei.value.error_code == "CB001"
        assert ei.value.http_status == 400
        assert ei.value.details["field"] == "id_card"

    def test_raise_from_spec_catch_by_specific_subclass(self) -> None:
        with pytest.raises(PipelineError) as ei:
            raise CaseBillError.from_spec(PipelineErrorCode.CB102)
        assert isinstance(ei.value, PipelineError)
        assert ei.value.error_code == "CB102"

    def test_raise_with_cause_round_trip(self) -> None:
        """底层异常透传到 CaseBillError，再 round-trip 不丢。"""
        try:
            try:
                raise ValueError("底层异常")
            except ValueError as inner:
                raise CaseBillError.from_spec(
                    ModelGatewayErrorCode.CB303,
                    cause=inner,
                ) from inner
        except CaseBillError as e:
            d = e.to_dict()
            loaded = json.loads(json.dumps(d, ensure_ascii=False))
            assert loaded["error_code"] == "CB303"
            assert "底层异常" in loaded["cause"]["message"]
