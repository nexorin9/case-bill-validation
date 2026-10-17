"""字段抽取 processor（Task 6 实装；Task 22 边界加固）。

设计要点：

- 输入：``BillingCaseDocument``（已由 ``SegmenterProcessor`` 填充 ``spans`` /
  ``sections``，并完成 ``raw_text`` 落盘）。
- 输出：写入 ``doc.extracted_fields`` —— 12 类高频字段（身份证号、入院日期、
  出院日期、住院天数、ICD-10 诊断码、ICD-9-CM-3 手术码、年龄、性别、姓名、
  出生日期、主要手术日期、出院科别等）的正则抽取结果，剩余字段由 LLM 补抽
  或标 ``needs_manual``。
- 路径：``extract_by_regex → 失败时 extract_by_llm → 仍失败标 needs_manual``。
  LLM 路径由 Task 8 接入 ``ModelGateway``；Task 6 阶段 ``extract_by_llm`` 是
  stub（标 ``NEEDS_MANUAL`` + 记 ``audit_log``），保证链路不中断。
- 状态机：``pending → extracting → rule_checked``（由 Task 7 推进）或
  ``pending → flagged``（正则 + LLM 双重失败）。
- **Task 22 边界加固**：
  - 编码探测 ``detect_extraction_encoding``（与 parser 层 ``detect_text_encoding``
    对齐但只评估已解码字符串的字符频率，用于判定 GBK 还是 UTF-8 落地）；
  - 控制字符 / 脏行检测 ``is_low_confidence_span`` + ``has_heavy_control_chars``；
  - 单字段抽取命中后若 value 含控制字符 → 改标 needs_manual；
  - 整份 raw_text 控制字符过密 → doc.needs_manual=True + 全字段 source=NEEDS_MANUAL。

参考：

- ``github_ref/stanza/stanza/pipeline/core.py:176-513`` Pipeline 顺序累加模式
  （t5 已借鉴）；本模块是 Pipeline 第二步，输入 spans → 输出 extracted_fields；
- ``spec.md`` §3（193 项字段规范）与 §5（closure_dimensions 中"关键实体
  绑定"维度：span 必落在原文边界内 + 与 value 一致）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Optional

from case_bill_validation.document import (
    AuditEntry,
    BillingCaseDocument,
    FieldMark,
    FieldSource,
    FieldStatus,
    SpanRef,
)
from case_bill_validation.pipeline.core import Processor
from case_bill_validation.rules.field_ruleset import BillingField

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task 22 · 边界检测常量
# ---------------------------------------------------------------------------


# 控制字符（C0/C1 控制码 + DEL + 软 hyphen 等都会被剔除；保留常见空白与中文空格）。
# 当 ``raw_text`` 内此类字符占比超过 ``CONTROL_CHAR_DOC_THRESHOLD``，全单据
# 标 ``needs_manual``；当某字段 value 的占比超过 ``CONTROL_CHAR_FIELD_THRESHOLD``
# 该字段单独标 ``needs_manual``（不污染其他字段）。
CONTROL_CHAR_FIELD_THRESHOLD: float = 0.05
CONTROL_CHAR_DOC_THRESHOLD: float = 0.02

# 低置信度判定：超长字段阈值（单字段 value 字符数）
LONG_FIELD_CHAR_THRESHOLD: int = 500


def count_control_chars(text: str) -> int:
    """统计字符串中的"控制 / 不可打印字符"数。

    控制字符指 category 以 ``C`` 开头（``Cc`` 控制 / ``Cf`` 格式 / ``Cs`` 代理
    / ``Co`` 私用 / ``Cn`` 未分配）的字符；常规空白 ``Zs``（含全/半角空格）
    视为正常字符，不计入。
    **换行 / 制表 / 回车** (\n / \t / \r) 形式上是 ``Cc``，但属于合法排版字符，
    多行病案文档天然含大量 ``\n`` → 不计入控制字符计数（避免把"格式良好的
    多段落"误判为脏文）。
    """
    if not text:
        return 0
    # 合法排版空白：换行 / 制表 / 回车
    formatting_whitespace = {"\n", "\r", "\t"}
    return sum(
        1
        for ch in text
        if unicodedata.category(ch).startswith("C") and ch not in formatting_whitespace
    )


def control_char_ratio(text: str) -> float:
    """控制字符 / 总字符数。空串返 0。"""
    if not text:
        return 0.0
    return count_control_chars(text) / len(text)


def is_low_confidence_span(value: str, threshold: float = CONTROL_CHAR_FIELD_THRESHOLD) -> bool:
    """字段 value 含过多控制字符 → 视为脏行 / OCR 误识别，需标记 needs_manual。

    仅在 ``value`` 非空时检查；空字符串由"未抽取"逻辑处理，不算低置信度。
    """
    if not value:
        return False
    return control_char_ratio(value) > threshold


def has_heavy_control_chars(
    raw_text: str, threshold: float = CONTROL_CHAR_DOC_THRESHOLD
) -> bool:
    """整份 raw_text 控制字符过密 → 整单据 needs_manual。"""
    if not raw_text:
        return False
    return control_char_ratio(raw_text) > threshold


def is_unusually_long(value: str, threshold: int = LONG_FIELD_CHAR_THRESHOLD) -> bool:
    """单字段 value 超过阈值（如主诉 > 500 字） → 标记低置信度，仅记录观察。"""
    return len(value) > threshold


# ---------------------------------------------------------------------------
# 编码落地判定（解码已完成后，回头看 raw_text 的字符分布）
# ---------------------------------------------------------------------------


def detect_extraction_encoding(raw_text: str) -> str:
    """对已解码的 ``raw_text`` 做"落地编码"判定。

    - 出现 GBK 二义字符（U+3000 全角空格、U+FF1A 全角冒号等中文标点）→ 视为 GBK 来源；
    - 出现 ASCII 替换字符 U+FFFD（``decode errors='replace'`` 留下的坏字符） → 视为未知；
    - 仅 ASCII → utf-8 兜底。
    """
    if not raw_text:
        return "utf-8"
    if "�" in raw_text:
        return "unknown"
    # GBK 落地常见的全角标点 + 中文标点混合
    gbk_markers = "　，：；。？！（）【】《》"
    if any(marker in raw_text for marker in gbk_markers):
        return "gbk"
    return "utf-8"


# ---------------------------------------------------------------------------
# 正则字典：12 类高频字段（含若干兼容写法）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRegex:
    """单字段的若干正则候选；按顺序试，第一个命中即用。"""

    patterns: tuple[str, ...]
    # 当 value 是中文标签"姓名"等被一并切出来时，用 capture_group 取第几组；
    # 默认 1（首个捕获组）。
    capture_group: int = 1


# 顺序：先精确（带冒号）→ 再宽松（"性别 男" 形式）
# 注意：捕获组编号在所有 pattern 中保持一致（取 label/冒号后面的内容）
_FIELD_REGEX_TABLE: dict[str, FieldRegex] = {
    "姓名": FieldRegex(
        patterns=(
            r"姓名[:：]\s*([^\s　,，。；;]+)",
            r"患者[于在]?\d*[个]?月[前内]?[无明显诱因出现]*?([^\s　,，。；;]{2,4})[，,]",
        )
    ),
    "性别": FieldRegex(
        patterns=(
            r"性别[:：]\s*([男女])",
            r"[性別]\s*[:：]?\s*([男女])",
        )
    ),
    "年龄": FieldRegex(
        patterns=(
            r"年龄[:：]\s*(\d+)\s*岁",
            r"年龄\s*(\d+)\s*[岁y]",
        )
    ),
    "证件号码": FieldRegex(
        patterns=(
            r"(?:证件号码|身份证号?)[::]\s*(\S+)",
            r"住院号[:：]\s*(\S+)",
        )
    ),
    "出生日期": FieldRegex(
        patterns=(
            r"出生日期?[:：]\s*(\d{4}-\d{1,2}-\d{1,2})",
            r"(?:出生|生于)(?:[:：]\s*)?(\d{4}-\d{1,2}-\d{1,2})",
            r"(\d{6})(?=\d{4}\d{4})",  # 身份证内嵌出生日期：6 位 → 接 4 位；本规则仅作占位
        )
    ),
    "入院日期": FieldRegex(
        patterns=(
            r"入院日期?[:：]\s*(\d{4}-\d{1,2}-\d{1,2})",
        )
    ),
    "出院日期": FieldRegex(
        patterns=(
            r"出院日期?[:：]\s*(\d{4}-\d{1,2}-\d{1,2})",
        )
    ),
    "实际住院天数": FieldRegex(
        patterns=(
            r"实际住院天数[:：]\s*(\d+)",
            r"住院天数[:：]\s*(\d+)\s*天",
        )
    ),
    "入院科别": FieldRegex(
        patterns=(
            r"入院科[别室][:：]\s*([^\s　,，。；;]+科)",
            r"入院时(?:所在)?科室?[:：]\s*([^\s　,，。；;]+科)",
        )
    ),
    "出院科别": FieldRegex(
        patterns=(
            r"出院科[别室][:：]\s*([^\s　,，。；;]+科)",
            r"出院时(?:所在)?科室?[:：]\s*([^\s　,，。；;]+科)",
        )
    ),
    "主要诊断代码": FieldRegex(
        patterns=(
            # ICD-10 编码：A 字母开头 + 2 位数字 + 可选 .1-2 位数字
            r"主要诊断(?:代码|ICD)[:：]\s*([A-Z]\d{2}(?:\.\d{1,2})?)",
            # 在 ICD-10 列表中找首个 (单独成词)
            r"(?<![一-龥])([A-Z]\d{2}(?:\.\d{1,2})?)(?![一-龥])",
        )
    ),
    "主要手术操作代码": FieldRegex(
        patterns=(
            r"主要手术(?:操作)?(?:代码|ICD)[:：]\s*(\d{2}(?:\.\d{1,2})?)",
            r"(?<![一-龥\d])(\d{2}\.\d{2})(?![一-龥\d])",
        )
    ),
    "主要手术操作日期": FieldRegex(
        patterns=(
            r"主要手术(?:操作)?日期?[:：]\s*(\d{4}-\d{1,2}-\d{1,2})",
            r"于\s*(\d{4}-\d{1,2}-\d{1,2})\s*(?:在|行|实施|完成)",
        )
    ),
    "离院方式": FieldRegex(
        patterns=(
            r"离院方式[:：]\s*([1-49])(医嘱离院|医嘱转院|医嘱转基层医疗机构|非医嘱离院|其他)?",
        )
    ),
}


# 占位符列表（与 fixtures 的 <EXAMPLE_*> 对齐；用于审计追溯）
_PLACEHOLDER_NAMES = {
    "<EXAMPLE_NAME>",
    "<EXAMPLE_ID>",
    "<EXAMPLE_DOB>",
    "<EXAMPLE_ADMISSION>",
    "<EXAMPLE_DISCHARGE>",
    "<EXAMPLE_SURGERY>",
    "<EXAMPLE_HOSPITAL>",
    "<EXAMPLE_PHONE>",
    "<EXAMPLE_CONTACT>",
    "<EXAMPLE_ADDRESS>",
    "<EXAMPLE_INSURE_AREA>",
    "<EXAMPLE_EMPLOYER>",
    "<EXAMPLE_REG_NO>",
    "<EXAMPLE_MOTHER>",
    "<EXAMPLE_MOTHER_ID>",
    "<EXAMPLE_SURGEON>",
    "<EXAMPLE_ANESTHESIA>",
    "<EXAMPLE_DATE>",
    "<EXAMPLE_BILL_START>",
    "<EXAMPLE_BILL_END>",
    "<EXAMPLE_DISCHARGE_ORDER>",
}


# ---------------------------------------------------------------------------
# 抽取结果数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionMatch:
    """正则匹配命中位置。"""

    value: str
    start_char: int
    end_char: int
    confidence: float


# ---------------------------------------------------------------------------
# 正则抽取纯函数
# ---------------------------------------------------------------------------


def _find_regex_hit(
    raw_text: str, field_regex: FieldRegex
) -> Optional[ExtractionMatch]:
    """尝试多个 pattern，返回首个合法匹配。

    合法性校验：``value`` 非空 + 不在占位符集合中 + span 在 raw_text 内。
    """
    for pattern in field_regex.patterns:
        for m in re.finditer(pattern, raw_text):
            try:
                value = m.group(field_regex.capture_group).strip()
            except (IndexError, AttributeError):
                continue
            if not value:
                continue
            if value in _PLACEHOLDER_NAMES:
                # 占位符仍可作为命中保留（用于审计"该字段由人工填占位"）
                # 但 confidence 标 0.5 以区分真实数据
                return ExtractionMatch(
                    value=value,
                    start_char=m.start(field_regex.capture_group),
                    end_char=m.end(field_regex.capture_group),
                    confidence=0.5,
                )
            return ExtractionMatch(
                value=value,
                start_char=m.start(field_regex.capture_group),
                end_char=m.end(field_regex.capture_group),
                confidence=0.9,
            )
    return None


def extract_by_regex(
    raw_text: str,
    field_def: BillingField,
    field_regex: FieldRegex | None = None,
) -> Optional[ExtractionMatch]:
    """公共入口：按字段查表 + 跑正则。"""
    if field_regex is None:
        field_regex = _FIELD_REGEX_TABLE.get(field_def.name)
    if field_regex is None:
        return None
    return _find_regex_hit(raw_text, field_regex)


# ---------------------------------------------------------------------------
# LLM 抽取占位（Task 8 接入 ModelGateway）
# ---------------------------------------------------------------------------


def extract_by_llm(
    raw_text: str,
    field_def: BillingField,
    llm_gateway: Optional[Any] = None,
) -> Optional[ExtractionMatch]:
    """LLM 抽取（Task 6 占位：仅当 gateway 已注入时调用，否则返回 None）。

    Task 8 实装：
    - 调 ``ModelGateway.chat_structured``，schema 形如
      ``{"value": str, "span_start": int, "span_end": int, "confidence": float}``；
    - 失败（401/超时/JSON 解析/结构化校验）按 spec.md §5 走"rule_checked +
      semantic_skipped"路径，**不静默写数据**。

    Task 6 阶段：若 ``llm_gateway`` 注入且提供 ``extract_field`` 方法，调用之；
    否则返回 None（表示 LLM 不可用 → 标 ``needs_manual``）。
    """
    if llm_gateway is None:
        return None
    extract_fn = getattr(llm_gateway, "extract_field", None)
    if not callable(extract_fn):
        return None
    try:
        result = extract_fn(raw_text=raw_text, field_def=field_def)
    except Exception as exc:  # noqa: BLE001 —— 占位阶段吞掉所有异常
        logger.warning("LLM 抽取 %s 失败: %s", field_def.name, exc)
        return None
    if not result:
        return None
    # 兼容 dict 与 ExtractionMatch 两类返回
    if isinstance(result, ExtractionMatch):
        return result
    if isinstance(result, dict):
        try:
            return ExtractionMatch(
                value=str(result["value"]),
                start_char=int(result["span_start"]),
                end_char=int(result["span_end"]),
                confidence=float(result.get("confidence", 0.6)),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Processor 主类
# ---------------------------------------------------------------------------


class ExtractorProcessor(Processor):
    """字段抽取 processor（Task 6 实装）。

    工作流
    ------
    1. 遍历 ``BillingFieldRuleSet`` 193 项（或经 ``target_field_names`` 缩小）；
    2. 每字段先调 :func:`extract_by_regex`，命中则写 ``FieldMark``；
    3. 正则失败时调 :func:`extract_by_llm`（stub → ``needs_manual``）；
    4. 双重失败 → 字段 ``status='flagged'`` + ``source='needs_manual'``；
    5. 抽取成功后字段状态机推进 ``pending → extracting``，由下一 processor
       (rule_checker) 推进到 ``rule_checked``；本 processor 不写
       ``rule_checked``，避免越权。

    REQUIRES / PROVIDES
    -------------------
    - requires: ``{'spans', 'sections'}`` 由 segmenter 提供；
    - provides: ``{'extracted_fields'}`` 注入到 ``doc.extracted_fields``。

    注入点
    ------
    - ``rule_set`` —— :class:`BillingFieldRuleSet` 实例；不传则 lazy 打开
      默认 SQLite（``BILLING_DB_PATH`` 环境变量或 ``./exports/billing_fields.db``）；
    - ``llm_gateway`` —— Task 8 实装的 ModelGateway；可选；
    - ``target_field_names`` —— 只抽指定字段子集（便于 e2e 测时只验 12 类高频）；
      默认 ``None`` 表示全部 193 项。
    """

    step_name = "extractor"
    REQUIRES_DEFAULT = {"spans", "sections"}
    PROVIDES_DEFAULT = {"extracted_fields"}

    def __init__(
        self,
        rule_set: Any = None,
        llm_gateway: Any = None,
        target_field_names: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._rule_set = rule_set
        self._llm_gateway = llm_gateway
        self._target_field_names = target_field_names

    # ------------------------------------------------------------------
    # 主路径
    # ------------------------------------------------------------------

    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        rule_set = self._resolve_rule_set()
        target_names = self._resolve_target_names(rule_set)

        # 若 raw_text 为空：直接降级 needs_manual，标 processed_steps 后返回
        if not doc.raw_text:
            doc.needs_manual = True
            doc.append_audit(
                AuditEntry(
                    actor="extractor",
                    action="skip_empty_raw_text",
                    note="raw_text 为空；不抽字段",
                )
            )
            doc.record_step(self.step_name)
            return doc

        # Task 22：整份 raw_text 控制字符过密 → 整单据 needs_manual；
        # 已抽取字段的 source 一律改 NEEDS_MANUAL（即便正则命中也不写脏值）。
        whole_doc_dirty = has_heavy_control_chars(doc.raw_text)
        if whole_doc_dirty:
            doc.needs_manual = True
            ratio = control_char_ratio(doc.raw_text)
            detected = detect_extraction_encoding(doc.raw_text)
            doc.append_audit(
                AuditEntry(
                    actor="extractor",
                    action="raw_text_high_control_char_ratio",
                    note=(
                        f"control_ratio={ratio:.4f} > threshold="
                        f"{CONTROL_CHAR_DOC_THRESHOLD}; encoding={detected}; "
                        "强制全字段 needs_manual"
                    ),
                )
            )

        # 提前确认所有 span 在原文内（segmenter 已有保证，这里再次 self-check）
        text_len = len(doc.raw_text)

        for fname in target_names:
            field_def = rule_set.get_field(fname)
            if field_def is None:
                # 字段不在 193 项中（非法命名）—— 跳过并写审计
                doc.append_audit(
                    AuditEntry(
                        actor="extractor",
                        field=fname,
                        action="unknown_field",
                        note=f"字段 {fname} 不在 193 项字段表中",
                    )
                )
                continue

            # 跳过空 raw_text 已被前置处理；这里正常抽取
            mark = self._extract_one(doc.raw_text, field_def)
            if mark is None:
                continue

            # span 边界 self-check（即便上游已校验，这里再挡一次）
            if mark.span is not None:
                if mark.span.end_char > text_len or mark.span.start_char < 0:
                    logger.warning(
                        "字段 %s 的 span 越界（end=%s > %s）—— 改为 needs_manual",
                        fname,
                        mark.span.end_char,
                        text_len,
                    )
                    mark = mark.model_copy(
                        update={"span": None, "source": FieldSource.NEEDS_MANUAL}
                    )
                    mark = self._safe_mark_needs_manual(mark)

            # Task 22：单字段抽取值含控制字符 → 改标 needs_manual（不写脏值进数据库）
            if mark.value is not None and isinstance(mark.value, str):
                if is_low_confidence_span(mark.value):
                    logger.warning(
                        "字段 %s 的 value 含控制字符 ratio=%.3f → needs_manual",
                        fname,
                        control_char_ratio(mark.value),
                    )
                    doc.append_audit(
                        AuditEntry(
                            actor="extractor",
                            field=fname,
                            action="field_value_low_confidence",
                            note=(
                                f"value 控制字符 ratio="
                                f"{control_char_ratio(mark.value):.4f} > "
                                f"{CONTROL_CHAR_FIELD_THRESHOLD}"
                            ),
                        )
                    )
                    mark = mark.model_copy(
                        update={
                            "value": None,
                            "span": None,
                            "source": FieldSource.NEEDS_MANUAL,
                            "note": "low_confidence_span",
                        }
                    )
                    mark = self._safe_mark_needs_manual(mark)

            # Task 22：全单据已标 needs_manual → 即便本字段 value 是干净的，
            # 也避免在源头状态不稳定时下传（rule_checker 仍可标 flagged 兜底）。
            if whole_doc_dirty:
                mark = mark.model_copy(
                    update={"source": FieldSource.NEEDS_MANUAL}
                )
                mark = self._safe_mark_needs_manual(mark)

            # Task 22：超长字段观察（仅记 audit，不强行 needs_manual）
            if mark.value is not None and isinstance(mark.value, str):
                if is_unusually_long(mark.value):
                    doc.append_audit(
                        AuditEntry(
                            actor="extractor",
                            field=fname,
                            action="field_unusually_long",
                            note=(
                                f"value length={len(mark.value)} chars > "
                                f"threshold={LONG_FIELD_CHAR_THRESHOLD}"
                            ),
                        )
                    )

            doc.upsert_field(mark)

        doc.record_step(self.step_name)
        return doc

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _extract_one(
        self, raw_text: str, field_def: BillingField
    ) -> FieldMark | None:
        """抽取单字段；返回 FieldMark 或 None（既未命中又不需要占位）。"""
        # 1) 正则优先
        match = extract_by_regex(raw_text, field_def)
        if match is not None:
            span = SpanRef(
                start_char=match.start_char,
                end_char=match.end_char,
            ) if (match.end_char > match.start_char) else None
            mark = FieldMark(
                field_name=field_def.name,
                value=match.value,
                span=span,
                confidence=match.confidence,
                status=FieldStatus.PENDING,  # 由 rule_checker 推进
                source=FieldSource.REGEX,
            )
            return self._move_to_extracting(mark)

        # 2) LLM 兜底（Task 8 接入）
        match = extract_by_llm(raw_text, field_def, self._llm_gateway)
        if match is not None:
            span = (
                SpanRef(start_char=match.start_char, end_char=match.end_char)
                if match.end_char > match.start_char
                else None
            )
            mark = FieldMark(
                field_name=field_def.name,
                value=match.value,
                span=span,
                confidence=match.confidence,
                status=FieldStatus.PENDING,
                source=FieldSource.LLM,
            )
            return self._move_to_extracting(mark)

        # 3) 双重失败 → needs_manual
        return FieldMark(
            field_name=field_def.name,
            value=None,
            span=None,
            confidence=0.0,
            status=FieldStatus.PENDING,
            source=FieldSource.NEEDS_MANUAL,
        ).mark_needs_manual()

    @staticmethod
    def _move_to_extracting(mark: FieldMark) -> FieldMark:
        """把字段状态从 PENDING 推到 EXTRACTING；非法迁移由 mark_* 把关。"""
        try:
            return mark.mark_extracting()
        except Exception:  # noqa: BLE001 —— 已为终态时直接返回原 mark
            return mark

    @staticmethod
    def _safe_mark_needs_manual(mark: FieldMark) -> FieldMark:
        """把字段标 ``FLAGGED`` + ``source=NEEDS_MANUAL``；已为 FLAGGED 时返回原 mark。

        Task 22 引入：同一字段可能被多层边界检查都判 needs_manual（regex 失败 →
        FLAGGED；又触发 whole_doc_dirty / low_confidence_span → 想再标 FLAGGED）；
        这里显式吞掉 ``InvalidTransitionError`` 而非让流水线整体崩。
        """
        try:
            return mark.mark_needs_manual()
        except Exception:  # noqa: BLE001 —— FLAGGED/terminated 状态时幂等跳过
            return mark

    def _resolve_rule_set(self) -> Any:
        """注入优先；否则按 BILLING_DB_PATH 打开。"""
        if self._rule_set is not None:
            return self._rule_set
        # 懒加载默认 db 路径
        from pathlib import Path

        from case_bill_validation.rules.field_ruleset import BillingFieldRuleSet

        env_db = Path("./exports/billing_fields.db")
        if env_db.exists():
            return BillingFieldRuleSet.open(env_db)
        # 兜底：tests/conftest 注入的 tmp_billing_db 路径
        return BillingFieldRuleSet.open(env_db)

    def _resolve_target_names(self, rule_set: Any) -> list[str]:
        """返回本轮要抽取的字段名列表。"""
        if self._target_field_names is not None:
            return list(self._target_field_names)
        # 默认 12 类高频字段 + 必填字段
        high_freq = list(_FIELD_REGEX_TABLE.keys())
        required = list(rule_set.required_fields())
        # 保序去重
        seen: set[str] = set()
        out: list[str] = []
        for n in high_freq + required:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out


# ---------------------------------------------------------------------------
# 暴露给 spec.md / 上层调用的 API
# ---------------------------------------------------------------------------


__all__ = [
    "ExtractorProcessor",
    "extract_by_regex",
    "extract_by_llm",
    "ExtractionMatch",
    "FieldRegex",
    # Task 22 · 边界检测 API
    "count_control_chars",
    "control_char_ratio",
    "is_low_confidence_span",
    "has_heavy_control_chars",
    "is_unusually_long",
    "detect_extraction_encoding",
    "CONTROL_CHAR_FIELD_THRESHOLD",
    "CONTROL_CHAR_DOC_THRESHOLD",
    "LONG_FIELD_CHAR_THRESHOLD",
]


# ---------------------------------------------------------------------------
# 工厂 helper（便于测试与 e2e 注入）
# ---------------------------------------------------------------------------


def make_extractor(
    rule_set: Any | None = None,
    llm_gateway: Any | None = None,
    target_field_names: list[str] | None = None,
) -> ExtractorProcessor:
    """便捷工厂：build ExtractorProcessor 实例。

    等价于 ``ExtractorProcessor(rule_set=..., llm_gateway=...,
    target_field_names=...)``；暴露为顶层函数便于跨模块 import。
    """
    return ExtractorProcessor(
        rule_set=rule_set,
        llm_gateway=llm_gateway,
        target_field_names=target_field_names,
    )