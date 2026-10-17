"""Prompt 模板 + JSON Schema re-export（Task 8 / 21 联动）。

提供：
- :func:`semantic_check_prompt` —— 语义核 prompt（Task 8 用）；
- :func:`extraction_prompt` —— 字段抽取 prompt（Task 6 占位 / 21 实装）；
- 旧版本在本地维护的 JSON Schema dict **已搬到** :mod:`.schemas` 由 jsonschema
  严格校验；本模块仍 re-export ``SEMANTIC_CHECK_SCHEMA`` /
  ``EXTRACTION_SCHEMA`` 两个常量，保持 ``from
  case_bill_validation.llm.prompts import SEMANTIC_CHECK_SCHEMA`` 这类调用
  不破坏。

参考
----
- ``spec.md`` §6（ModelGateway 结构化输出 JSON Schema）；
- ``plan.md`` §6（结构化输出 schema 定义）；
- ``stanza/pipeline/core.py:176-513`` —— pipeline 链路结构化推理；
- ``stanza/pipeline/constituency_processor.py:108-115`` —— PROVIDES / REQUIRES 模式。
"""

from __future__ import annotations

from typing import Any, Mapping

# JSON Schema 常量 re-export（数据真源在 :mod:`.schemas`，jsonschema 严格校验）
from case_bill_validation.llm.schemas import (  # noqa: F401  re-export
    SEMANTIC_CHECK_SCHEMA,
    EXTRACTION_SCHEMA,
    SCHEMAS,
    get_schema,
    get_validator,
    validate_payload,
    validate_against_dict,
    iter_errors,
    is_valid_payload,
    schema_names,
)


# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT_SEMANTIC_CHECK = (
    "你是医保结算清单核验助手，"
    "请对已经被规则审查标 flagged 的字段做语义核——"
    "判断该字段在当前病案上下文中是否合规、并给出依据原文 span。"
    "所有结论必须严格基于原文，不要凭空补全未出现的字段。"
)

_SYSTEM_PROMPT_EXTRACTION = (
    "你是医保结算清单字段抽取助手。"
    "请按 JSON Schema 返回每个字段在原文中出现的位置与值；"
    "未出现的字段不要输出。"
)


def semantic_check_prompt(
    field_def: Mapping[str, Any],
    value: Any,
    raw_text: str,
    *,
    max_chars: int = 6000,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """构造语义核的 (messages, json_schema) 元组。

    参数
    ----
    field_def: 字段定义（含 ``name`` / ``description`` / ``required`` /
      ``field_type`` 等；通常来自 :class:`BillingFieldRuleSet.get_field`）；
    value: 字段已抽取值；
    raw_text: 病案原文（截断到 ``max_chars``，避免超 token）；
    max_chars: 原文最大字符数。

    返回
    ----
    ``(messages, json_schema)``：messages 是 OpenAI-compatible chat 格式；
    json_schema 是 :data:`SEMANTIC_CHECK_SCHEMA`，发到 ModelGateway 时会被
    注入到 ``response_format.json_object`` + ``json_schema`` 字段。
    """
    field_name = str(field_def.get("name", ""))
    field_desc = str(field_def.get("description", "")) or "（无字段说明）"
    field_type = str(field_def.get("field_type", "")) or "（未指定）"
    required = bool(field_def.get("required", False))
    value_repr = repr(value)
    truncated = raw_text[:max_chars]
    suffix = "" if len(raw_text) <= max_chars else "（原文已截断）"

    user_prompt = (
        f"## 字段\n"
        f"- 名称：{field_name}\n"
        f"- 类型：{field_type}\n"
        f"- 是否必填：{'是' if required else '否'}\n"
        f"- 字段说明：{field_desc}\n\n"
        f"## 已抽取值（来自规则 / 正则）\n"
        f"{value_repr}\n\n"
        f"## 病案原文（最多 {max_chars} 字符）{suffix}\n"
        f"```\n{truncated}\n```\n\n"
        "## 任务\n"
        "请按 JSON Schema 输出：\n"
        "- field: 字段名\n"
        "- status: confirmed / rejected / flagged / needs_manual\n"
        "- reason_text: 1–2 句说明（中文；不超过 120 字）\n"
        "- evidence_span: {start_char, end_char, page_no?} —— "
        "在原文中支持你结论的关键句位置（end_char 必须 > start_char）\n\n"
        "## 注意\n"
        "- 不要凭空补全原文中没有的信息；\n"
        "- 若原文中证据不足，请将 status 设为 flagged，并在 reason_text 中"
        "说明「证据不足」。"
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT_SEMANTIC_CHECK},
        {"role": "user", "content": user_prompt},
    ]
    return messages, SEMANTIC_CHECK_SCHEMA


def extraction_prompt(
    target_field_names: list[str],
    raw_text: str,
    *,
    max_chars: int = 8000,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """构造字段抽取的 (messages, json_schema)。

    参数
    ----
    target_field_names: 本轮要抽取的字段名列表；
    raw_text: 病案原文（截断到 ``max_chars``）。

    返回
    ----
    ``(messages, EXTRACTION_SCHEMA)``。Task 6 阶段 ``extractor.extract_by_llm``
    的 stub 已用 duck-typed 协议，Task 21 实装 ``llm.schemas`` 时这里已
    提供 schema 常量。
    """
    truncated = raw_text[:max_chars]
    suffix = "" if len(raw_text) <= max_chars else "（原文已截断）"
    fields_str = "、".join(target_field_names)
    user_prompt = (
        "## 任务\n"
        f"请从下方病案原文中抽取以下字段：{fields_str}。"
        "若字段在原文中未出现，跳过不要输出。\n\n"
        f"## 病案原文（最多 {max_chars} 字符）{suffix}\n"
        f"```\n{truncated}\n```\n\n"
        "## 输出\n"
        "按 JSON Schema 返回 ``fields`` 数组；每项含 name / value / "
        "span_start / span_end / confidence（0–1）。"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT_EXTRACTION},
        {"role": "user", "content": user_prompt},
    ]
    return messages, EXTRACTION_SCHEMA


__all__ = [
    "SEMANTIC_CHECK_SCHEMA",
    "EXTRACTION_SCHEMA",
    "semantic_check_prompt",
    "extraction_prompt",
]
