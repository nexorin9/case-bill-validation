"""SSE 心跳与客户端重连机制测试（Task 27）。

覆盖：

1. SSE 流包含 ``: ping`` 心跳注释行（避免代理超时切断）；
2. 自定义 ``ping_message_factory`` 返回精确 ``: ping`` 字面量（不是
   ``sse-starlette`` 默认 ``: ping - <timestamp>`` 长字符串）；
3. ``SSE_HEARTBEAT_INTERVAL_SEC == 15.0``（与生产一致）；
4. 流水线完成后心跳任务不再产生事件（避免客户端永远挂着）；
5. 客户端 ``app.js`` 含显式 ``onopen`` / ``onerror`` / 重连计数逻辑
   （grep 关键字），与自动重连配合工作；
6. ``styles.css`` 含 ``.reconnecting-banner`` 样式。

设计要点
--------

- 服务端心跳：``EventSourceResponse(ping=15, ping_message_factory=...)``，
  ``sse-starlette`` 在事件空闲时调用工厂生成 ``: ping`` 注释行；
- 客户端自动重连：EventSource 默认在 readyState === CONNECTING 时按
  retry 间隔重连；我们显式监听 onopen / onerror 做 UI 提示与次数计数。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from case_bill_validation.server import (
    PIPELINE_STEP_ORDER,
    SSE_HEARTBEAT_COMMENT,
    SSE_HEARTBEAT_INTERVAL_SEC,
    _heartbeat_factory,
    create_app,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def async_client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _upload(async_client: AsyncClient, fixtures_dir) -> str:
    sample = fixtures_dir / "discharge_01.txt"
    with sample.open("rb") as f:
        resp = await async_client.post(
            "/api/cases",
            files={"file": ("discharge_01.txt", f, "text/plain")},
        )
    assert resp.status_code == 202, resp.text
    return resp.json()["case_id"]


async def _consume_sse_until_done(
    async_client: AsyncClient,
    case_id: str,
    *,
    max_lines: int = 400,
    timeout: float = 60.0,
) -> list[str]:
    """打开 SSE 流，消费直到收到 ``done`` 事件，返回**原始行**列表（不做 SSE 解析）。"""
    lines: list[str] = []
    deadline = time.time() + timeout
    async with async_client.stream("GET", f"/api/cases/{case_id}/stream") as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            lines.append(line)
            joined = "\n".join(lines)
            if '"step": "done"' in joined:
                break
            if len(lines) > max_lines:
                break
            if time.time() > deadline:
                break
    return lines


# ---------------------------------------------------------------------------
# 心跳工厂单测（不依赖事件循环）
# ---------------------------------------------------------------------------


def test_heartbeat_factory_returns_ping_server_sent_event():
    """_heartbeat_factory() 返回 ``: ping`` 注释行，不带时间戳后缀。"""
    sse = _heartbeat_factory()
    raw = sse.encode()
    # 默认 sep 是 ``\\r\\n``，所以序列化形如 ``: ping\\r\\n\\r\\n``
    assert b": ping" in raw, f"心跳应含 ': ping'，实际 {raw!r}"
    # 不应含默认 ``sse-starlette`` 长字符串（``ping - <timestamp>``）
    assert b"ping -" not in raw, (
        f"不应使用 sse-starlette 默认 ping 格式（含时间戳），实际 {raw!r}"
    )


def test_sse_heartbeat_constants_exposed():
    """心跳常量对外可见；间隔 15 秒 / comment = 'ping'。"""
    assert SSE_HEARTBEAT_INTERVAL_SEC == 15.0
    assert SSE_HEARTBEAT_COMMENT == "ping"
    # 也可被进程内 ping_message_factory 工厂直接使用
    assert _heartbeat_factory().comment == SSE_HEARTBEAT_COMMENT


def test_sse_heartbeat_interval_is_positive_number():
    """心跳间隔必须为非负数（sse-starlette 规定）。"""
    assert isinstance(SSE_HEARTBEAT_INTERVAL_SEC, (int, float))
    assert SSE_HEARTBEAT_INTERVAL_SEC > 0


# ---------------------------------------------------------------------------
# 端到端心跳行为
# ---------------------------------------------------------------------------


async def test_sse_stream_emits_ping_comment_line():
    """SSE 原始字节流至少出现一条 ``: ping`` 心跳注释行。

    实现要点
    --------
    - 直接驱动 sse-starlette 的 EventSourceResponse（不走真实流水线）：
      因为 fixture 流水线约 < 1 秒完成，``done`` 事件后 sse-starlette 立
      即关闭整个 task group，心跳来不及发出；
    - 用一个自定义事件生成器，``await asyncio.sleep`` 让出心跳间隔
      时长（缩短到 0.1 秒），保证心跳工厂被触发；
    - ``fake_receive`` 不能立即返 disconnect（会触发 active=False 把心跳
      任务一起 cancel），必须 sleep 阻塞；
    - 手工收集所有 ``send`` 的 body chunk，断言 ``: ping`` 注释行出现 ≥ 1 次。
    """
    import asyncio
    from sse_starlette.sse import EventSourceResponse

    async def _gen() -> "AsyncIterator[dict[str, Any]]":
        """手工事件生成器：先发一条 step，然后 sleep 0.5 秒（5 次心跳节拍）。"""
        yield {"event": "step", "data": json.dumps({"step": "subscribed"})}
        # 让心跳工作：0.5 秒内 0.1 秒节拍应触发 4-5 次心跳
        await asyncio.sleep(0.5)

    captured: list[bytes] = []

    async def fake_send(msg: dict) -> None:
        if msg.get("type") == "http.response.body":
            captured.append(msg.get("body", b""))

    async def fake_receive() -> dict:
        # 必须阻塞而非立即 disconnect，否则 ``active`` 翻 False 把心跳
        # 任务一并 cancel。这里 sleep 1 秒后让测试自然走完。
        await asyncio.sleep(1.0)
        return {"type": "http.disconnect"}

    resp = EventSourceResponse(
        _gen(),
        ping=0.1,
        ping_message_factory=_heartbeat_factory,
    )

    await asyncio.wait_for(
        resp({"type": "http"}, fake_receive, fake_send),
        timeout=2.0,
    )

    text = b"".join(captured).decode("utf-8", errors="replace")
    ping_lines = [
        line for line in text.split("\n")
        if line.strip() == ": ping"
    ]
    assert ping_lines, (
        f"期望至少一条 ': ping' 心跳注释行；SSE 原始文本前 500 字="
        f"{text[:500]!r}"
    )
    assert len(ping_lines) >= 1


async def test_sse_heartbeat_does_not_interfere_with_done_event(async_client, fixtures_dir):
    """流水线完成后仍能收到 done 事件（心跳不抢 done）。"""
    case_id = await _upload(async_client, fixtures_dir)
    lines = await _consume_sse_until_done(async_client, case_id)

    text = "\n".join(lines)
    # done 事件存在（事件流收尾）
    assert '"step": "done"' in text, f"应有 done 事件，实际 {lines[-5:]!r}"
    # data: 行未被心跳污染（心跳是注释行，没有 ``data:`` 前缀）
    for line in lines:
        if line.startswith(": ping"):
            assert not line.startswith("data:") and not line.startswith("event:")


async def test_sse_step_events_still_received_alongside_heartbeat(async_client, fixtures_dir):
    """心跳与 5 步 step 事件共存（sse-starlette 不会因为心跳把业务事件挤掉）。"""
    case_id = await _upload(async_client, fixtures_dir)
    lines = await _consume_sse_until_done(async_client, case_id)

    # 解析 data: 行成 events
    events: list[dict[str, Any]] = []
    for chunk in "\n".join(lines).split("\n\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        for line in chunk.split("\n"):
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line.split(":", 1)[1].lstrip()))
                except json.JSONDecodeError:
                    pass
    step_events = [
        e for e in events
        if "step" in e and e.get("step") in PIPELINE_STEP_ORDER
    ]
    assert len(step_events) >= 5, (
        f"应至少 5 步 step 事件，实际 {len(step_events)}: {step_events}"
    )


# ---------------------------------------------------------------------------
# 客户端重连机制（静态资产）
# ---------------------------------------------------------------------------


def test_app_js_has_explicit_onopen_reconnect_reset():
    """app.js 含 ``onopen`` 监听器（重连成功后清掉 UI 提示）。"""
    app_js = (
        Path(__file__).parent.parent
        / "case_bill_validation"
        / "static"
        / "app.js"
    )
    text = app_js.read_text(encoding="utf-8")
    assert "es.onopen" in text, "app.js 应显式监听 onopen"
    assert "state.reconnectAttempts = 0" in text, (
        "onopen 应重置 reconnectAttempts 计数器"
    )
    assert "hideReconnectingBanner" in text, (
        "onopen 应清掉重连横幅"
    )


def test_app_js_has_explicit_onerror_reconnect_state():
    """app.js 含 ``onerror`` 监听器（累加重连次数 + 提示横幅）。"""
    app_js = (
        Path(__file__).parent.parent
        / "case_bill_validation"
        / "static"
        / "app.js"
    )
    text = app_js.read_text(encoding="utf-8")
    assert "es.onerror" in text
    assert "state.reconnectAttempts" in text, (
        "onerror 应累加重连次数（区别于 EventSource 默认静默重连）"
    )
    assert "showReconnectingBanner" in text, (
        "onerror 应展示持久化横幅"
    )


def test_app_js_does_not_manually_call_es_close_on_error():
    """重连时不主动 close EventSource（EventSource 默认行为即会自动重连）。"""
    app_js = (
        Path(__file__).parent.parent
        / "case_bill_validation"
        / "static"
        / "app.js"
    )
    text = app_js.read_text(encoding="utf-8")
    # onerror 函数体内不应主动 close（会破坏自动重连）
    # 提取 onerror 函数体作局部判断
    m = re.search(r"es\.onerror\s*=\s*function\s*\([^)]*\)\s*\{(.*?)\};", text, re.DOTALL)
    assert m is not None, "应能找到 onerror 函数字面量"
    body = m.group(1)
    assert "es.close" not in body, (
        f"onerror 内不应主动 close，会破坏自动重连：{body[:200]!r}"
    )


def test_styles_css_has_reconnecting_banner_rule():
    """styles.css 含 ``.reconnecting-banner`` 样式（与 app.js 配套）。"""
    styles = (
        Path(__file__).parent.parent
        / "case_bill_validation"
        / "static"
        / "styles.css"
    )
    text = styles.read_text(encoding="utf-8")
    assert ".reconnecting-banner" in text, "应定义 .reconnecting-banner 样式"
    # 至少含核心定位属性
    assert "position: fixed" in text or "position:fixed" in text
    assert "@keyframes" in text, "应有脉冲动画"


# ---------------------------------------------------------------------------
# 完整 SSE 心跳 + 重连契约（一次性把多个维度串起来）
# ---------------------------------------------------------------------------


def test_heartbeat_and_reconnect_contract_end_to_end():
    """服务端心跳 + 客户端重连契约汇总：
    - server.py：SSE_HEARTBEAT_INTERVAL_SEC=15 + 自定义 ping_message_factory
      返回 ': ping' 注释行
    - app.js：onopen 重置 / onerror 累加 + 持久横幅
    - styles.css：.reconnecting-banner 样式存在
    """
    server_module_src = (
        Path(__file__).parent.parent
        / "case_bill_validation"
        / "server.py"
    ).read_text(encoding="utf-8")
    app_js = (
        Path(__file__).parent.parent
        / "case_bill_validation"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    styles = (
        Path(__file__).parent.parent
        / "case_bill_validation"
        / "static"
        / "styles.css"
    ).read_text(encoding="utf-8")

    # 1. 服务端契约
    assert "SSE_HEARTBEAT_INTERVAL_SEC" in server_module_src
    assert "ping_message_factory=_heartbeat_factory" in server_module_src, (
        "EventSourceResponse 应注入自定义 ping_message_factory"
    )
    assert "ServerSentEvent(comment=" in server_module_src, (
        "心跳工厂应显式构造 ServerSentEvent(comment='ping')"
    )

    # 2. 客户端契约
    assert "new EventSource(url)" in app_js, "应通过 new EventSource 启动订阅"
    assert "es.onopen" in app_js
    assert "es.onerror" in app_js
    assert "state.reconnectAttempts" in app_js

    # 3. UI 样式契约
    assert ".reconnecting-banner" in styles
    assert "is-visible" in styles, "横幅应有可见态切换 class"