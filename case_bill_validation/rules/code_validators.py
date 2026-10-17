"""ICD-10 / ICD-9-CM-3 编码区间校验器（Task 24）。

设计要点：

- 单一职责：本模块**仅**做编码形态与区间校验（不写数据库、不写状态机），
  业务层（``RuleCheckerProcessor``）调用 :func:`validate_code` 路由到
  :func:`validate_icd10` 或 :func:`validate_icd9_cm3`；
- **形态校验**走正则 ——
    - ICD-10      ：``^[A-Z][0-9]{2}(\\.[0-9]{1,2})?$``（字母 + 2 位数字 +
      可选 . + 1–2 位数字；与医保版 ICD-10 第 4 位扩展一致；不使用 3 位扩展
      以贴合现行病案首页/结算清单口径）；
    - ICD-9-CM-3  ：``^[0-9]{2}(\\.[0-9]{1,2})?$``（2 位数字 + 可选 . +
      1–2 位数字；与国家医保局 ICD-9-CM-3 编码规则一致）；
- **区间校验** 走首字母/首数字范围（A–Z / 00–99）——空字符串、None、
  非字符串都视为非法；
- 与既有 :func:`rule_checker._validate_field_type` 中的正则保持**完全一致**
  以避免回归；
- 公开 API：:func:`validate_icd10`、:func:`validate_icd9_cm3`、
  :func:`validate_code`（路由）+ :data:`ICD_CODE_REGISTRY`（字典，便于扩展
  + 工厂）；
- :func:`validation_error_message` 给业务层拼错误消息用，含字段名 + 编码
  + 合法格式提示。
"""

from __future__ import annotations

import re
from typing import Final


# ---------------------------------------------------------------------------
# 正则常量（与 rule_checker._validate_field_type 既有分支保持一致）
# ---------------------------------------------------------------------------

# ICD-10: 字母 A–Z + 2 位数字 + 可选 . + 1–3 位数字
# 注：保留与既有 rule_checker._validate_field_type 一致的 1–3 位扩展口径，
# 以贴合国内医保版 ICD-10 第 4–5 位扩展（如 I50.905 / I25.101）。
ICD10_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z][0-9]{2}(\.[0-9]{1,3})?$"
)

# ICD-9-CM-3: 2 位数字 + 可选 . + 1–3 位数字
ICD9_CM3_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9]{2}(\.[0-9]{1,3})?$"
)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def validate_icd10(code: object) -> bool:
    """校验 ICD-10 编码形态与区间。

    合法：

    - 字母开头（A–Z）+ 2 位数字 + 可选 .xx 或 .x 扩展；
    - 例如 ``A01.01``、``J18.901``、``I50.905``、``K80.801``、``I25.101``。

    非法（返回 ``False``）：

    - ``None`` / 非字符串；
    - 空字符串 / 全空白；
    - 小写字母开头（``a01.01``）；
    - 字母后只有 1 位数字（``A1.01``）；
    - 字母后 3 位数字无小数点（``A001``）；
    - 数字后跟 3 位小数（``A01.001``）；
    - 超过 2 位小数（``A01.0123``）；
    - 任何非字母开头（如 ``1A01``）。
    """
    if not isinstance(code, str):
        return False
    stripped = code.strip()
    if not stripped:
        return False
    return bool(ICD10_PATTERN.match(stripped))


def validate_icd9_cm3(code: object) -> bool:
    """校验 ICD-9-CM-3 编码形态与区间。

    合法：

    - 2 位数字 + 可选 . + 1–2 位数字；
    - 例如 ``00.01``、``47.0901``、``36.06``、``99.99``。

    非法（返回 ``False``）：

    - ``None`` / 非字符串；
    - 空字符串；
    - 单数字（``1``）；
    - 3 位数字无小数（``100``）；
    - 字母前缀（``a01``、``A01``）；
    - 3 位小数（``01.001``）；
    - 超过 2 位小数（``01.0123``）。
    """
    if not isinstance(code, str):
        return False
    stripped = code.strip()
    if not stripped:
        return False
    return bool(ICD9_CM3_PATTERN.match(stripped))


# ---------------------------------------------------------------------------
# 路由 + 错误消息
# ---------------------------------------------------------------------------


# 路由表（field_type 字面量 → 校验函数）
ICD_CODE_REGISTRY: Final[dict[str, object]] = {
    "icd10": validate_icd10,
    "icd9_cm3": validate_icd9_cm3,
}


class UnknownICDCodeType(ValueError):
    """``field_type`` 不在 ``ICD_CODE_REGISTRY`` 时抛出。"""


def validate_code(code: object, field_type: str) -> bool:
    """路由入口：按 ``field_type`` 调对应校验函数。

    :raises UnknownICDCodeType: ``field_type`` 不是 ICD 编码类型。
    """
    validator = ICD_CODE_REGISTRY.get(field_type)
    if validator is None:
        raise UnknownICDCodeType(
            f"未知编码类型 {field_type!r}；"
            f"期望 'icd10' 或 'icd9_cm3'"
        )
    return bool(validator(code))


def validation_error_message(
    field_name: str,
    code: object,
    field_type: str,
) -> str:
    """生成面向医保办结算员的错误消息（含合法格式提示）。"""
    if field_type == "icd10":
        hint = "字母 + 2 位数字 + 可选 . + 1–3 位扩展（如 I50.905）"
    elif field_type == "icd9_cm3":
        hint = "2 位数字 + 可选 . + 1–3 位扩展（如 36.06）"
    else:
        hint = "未知编码类型"
    return (
        f"字段 {field_name} {field_type} 编码非法：{code!r}；"
        f"合法格式：{hint}"
    )


__all__ = [
    "ICD10_PATTERN",
    "ICD9_CM3_PATTERN",
    "ICD_CODE_REGISTRY",
    "UnknownICDCodeType",
    "validate_icd10",
    "validate_icd9_cm3",
    "validate_code",
    "validation_error_message",
]
