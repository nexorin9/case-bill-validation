"""段落/句子切分 Processor（中文病案文档专用）。

输入：``BillingCaseDocument`` 含 ``raw_text``。
输出：填充 ``doc.spans`` 与 ``doc.sections``。

设计要点
--------

- 按章节标题正则匹配 ``主诉 / 现病史 / 既往史 / 体格检查 / 辅助检查 / 诊断 /
  治疗经过 / 出院医嘱 / 出院时情况 / 出院诊断 / 出院时医嘱`` 等；
- 段落边界：``\\n\\n`` 或一级章节标题命中；
- 句子边界：中文标点 ``。！？；…`` + ``\\n`` + ``\\r``；
- ``section`` 字典：``{section_name: [span_indices]}``，便于后续 processor 按
  章节抽取字段（如"诊断"章节找 ICD-10 编码、"治疗经过"找手术日期）。

字段语义（与 ``spec.md`` §3 一致）：

- ``doc.spans`` —— ``list[SpanRef]``，覆盖段落 + 句子两种粒度，句子 span
  嵌套在所属段落 span 之后（先段落后句子，按字符序）；
- ``doc.sections`` —— 注入到 ``doc.record_step('segmenter')`` 之后的
  ``doc.pipeline_state`` 旁的 dict（通过 ``extracted_fields`` 之外的
  ``BillingCaseDocument.sections`` 字段暴露，由 t4 新增字段承载）。

状态机
------

- 输入 ``DocumentState.DRAFT``，调用后切到 ``DocumentState.IN_REVIEW``；
- 字段状态不受本 processor 影响（抽取尚未发生）。

降级
----

- 若 ``raw_text`` 为空 → 标 ``doc.needs_manual = True``，span/section 留空，
  不抛错；后续 extractor 会看到 needs_manual 标记并跳过对应字段抽取。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from case_bill_validation.document import (
    BillingCaseDocument,
    DocumentState,
    SpanRef,
    assert_document_transition,
)
from case_bill_validation.pipeline.core import Processor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 章节标题正则（中文病案出院小结惯用写法）
# ---------------------------------------------------------------------------

_SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("主诉", re.compile(r"^\s*主[ 　]*诉[：:]\s*")),
    ("现病史", re.compile(r"^\s*现[ 　]*病[ 　]*史[：:]\s*")),
    ("既往史", re.compile(r"^\s*既[ 　]*往[ 　]*史[：:]\s*")),
    ("个人史", re.compile(r"^\s*个[ 　]*人[ 　]*史[：:]\s*")),
    ("家族史", re.compile(r"^\s*家[ 　]*族[ 　]*史[：:]\s*")),
    ("体格检查", re.compile(r"^\s*体[ 　]*格[ 　]*检[ 　]*查[：:]\s*")),
    ("辅助检查", re.compile(r"^\s*辅[ 　]*助[ 　]*检[ 　]*查[：:]\s*")),
    ("入院诊断", re.compile(r"^\s*入[ 　]*院[ 　]*诊[ 　]*断[：:]\s*")),
    ("入院时情况", re.compile(r"^\s*入[ 　]*院[ 　]*时[ 　]*情[ 　]*况[：:]\s*")),
    ("诊疗经过", re.compile(r"^\s*(诊[ 　]*疗[ 　]*经[ 　]*过|治疗经过)[：:]\s*")),
    ("手术记录", re.compile(r"^\s*手[ 　]*术[ 　]*记[ 　]*录[：:]\s*")),
    ("出院诊断", re.compile(r"^\s*出[ 　]*院[ 　]*诊[ 　]*断[：:]\s*")),
    ("出院时情况", re.compile(r"^\s*出[ 　]*院[ 　]*时[ 　]*情[ 　]*况[：:]\s*")),
    ("出院医嘱", re.compile(r"^\s*出[ 　]*院[ 　]*医[ 　]*嘱[：:]\s*")),
]


def detect_section_title(line: str) -> str | None:
    """若 ``line`` 命中任一标题正则，返回章节名；否则 ``None``。"""
    stripped = line.strip()
    if not stripped:
        return None
    for name, pat in _SECTION_PATTERNS:
        if pat.match(stripped):
            return name
    return None


# ---------------------------------------------------------------------------
# 句子边界正则（中文标点 + 换行）
# ---------------------------------------------------------------------------

# 中文/英文/全角句末标点 + 分号（中式病案也常用作句子边界）
_SENTENCE_END_RE = re.compile(r"(?<=[。！？；…])")


@dataclass(frozen=True)
class SectionRange:
    """章节范围：``[start_char, end_char, name]``。"""

    start_char: int
    end_char: int
    name: str


def split_into_paragraphs(raw_text: str) -> list[tuple[int, int]]:
    """按 ``\\n\\n`` 或单 ``\\n`` 切段落，返回每段 ``(start_char, end_char)``。

    - 段落保留前后空白字符于边界外（trim 但不丢）；下游 sentence 切分
      在段落内做。
    - 跨段落顺序保留，按原文出现顺序输出。
    """
    if not raw_text:
        return []
    paragraphs: list[tuple[int, int]] = []
    cursor = 0
    n = len(raw_text)
    while cursor < n:
        # 找下一个段落边界（双换行优先，单换行次之）
        # 双换行：cursor.."\n\n"
        next_double = raw_text.find("\n\n", cursor)
        next_single = raw_text.find("\n", cursor)
        if next_double == -1 and next_single == -1:
            paragraphs.append((cursor, n))
            break
        if next_double != -1 and (next_single == -1 or next_double <= next_single):
            end = next_double
            paragraphs.append((cursor, end))
            cursor = end + 2  # 跳过 "\n\n"
        else:
            end = next_single
            paragraphs.append((cursor, end))
            cursor = end + 1
    # 去掉空段落（仅当整段是空白时）
    return [
        (s, e)
        for s, e in paragraphs
        if raw_text[s:e].strip()
    ]


def split_paragraph_into_sentences(
    raw_text: str, para_start: int, para_end: int
) -> list[tuple[int, int]]:
    """在段落 ``[para_start, para_end)`` 内按中文句末标点切句子。

    若段落内无句末标点 → 整段作为一个句子。
    """
    text = raw_text[para_start:para_end]
    if not text.strip():
        return []
    sentences: list[tuple[int, int]] = []
    last_cut = 0
    for m in _SENTENCE_END_RE.finditer(text):
        end_local = m.end()
        if end_local <= last_cut:
            continue
        sentences.append((para_start + last_cut, para_start + end_local))
        last_cut = end_local
    if last_cut < len(text):
        sentences.append((para_start + last_cut, para_end))
    return [s for s in sentences if raw_text[s[0] : s[1]].strip()]


def detect_sections(raw_text: str) -> list[SectionRange]:
    """按行扫描章节标题，返回 ``SectionRange`` 列表（按字符序）。"""
    if not raw_text:
        return []
    sections: list[SectionRange] = []
    pos = 0
    current: tuple[str, int] | None = None  # (name, start)
    n = len(raw_text)
    # 按行扫描
    line_start = 0
    while line_start < n:
        nl = raw_text.find("\n", line_start)
        line_end = nl if nl != -1 else n
        line = raw_text[line_start:line_end]
        title = detect_section_title(line)
        if title is not None:
            # 关闭上一段
            if current is not None:
                sections.append(
                    SectionRange(current[1], line_start, current[0])
                )
            current = (title, line_start)
        line_start = line_end + 1 if nl != -1 else n
    # 收尾
    if current is not None:
        sections.append(SectionRange(current[1], n, current[0]))
    return sections


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class SegmenterProcessor(Processor):
    """段落/句子切分。

    REQUIRES: 无（依赖 raw_text，由 DocumentParser 在更早阶段填充）。
    PROVIDES: ``spans``, ``sections``。
    """

    REQUIRES_DEFAULT: set[str] = set()
    PROVIDES_DEFAULT: set[str] = {"spans", "sections"}
    step_name: str = "segmenter"

    def process(self, doc: BillingCaseDocument) -> BillingCaseDocument:
        if not doc.raw_text:
            logger.info(
                "case_id=%s raw_text 为空；segmenter 跳过",
                doc.case_id,
            )
            doc.needs_manual = True
            doc.record_step(self.step_name)
            # 单据状态推进：DRAFT → IN_REVIEW（pipeline 已启动即视为审核中）
            if doc.pipeline_state == DocumentState.DRAFT:
                try:
                    assert_document_transition(
                        doc.pipeline_state, DocumentState.IN_REVIEW
                    )
                    doc.pipeline_state = DocumentState.IN_REVIEW
                except Exception:  # noqa: BLE001
                    pass
            return doc

        spans: list[SpanRef] = []
        # 1) 段落
        paragraphs = split_into_paragraphs(doc.raw_text)
        for para_start, para_end in paragraphs:
            spans.append(SpanRef(start_char=para_start, end_char=para_end))
        # 2) 句子（每个段落内切）
        for para_start, para_end in paragraphs:
            for s_start, s_end in split_paragraph_into_sentences(
                doc.raw_text, para_start, para_end
            ):
                spans.append(SpanRef(start_char=s_start, end_char=s_end))
        # 3) 章节（章节 = 一个 SectionRange 也加入 spans，便于"看依据"按钮跳转章节首）
        section_ranges = detect_sections(doc.raw_text)
        for sr in section_ranges:
            spans.append(
                SpanRef(start_char=sr.start_char, end_char=sr.end_char)
            )

        doc.spans = spans
        # sections 字典：{section_name: [span_index_in_doc.spans]}
        sections_map: dict[str, list[int]] = {}
        for sr in section_ranges:
            for idx, sp in enumerate(doc.spans):
                if (
                    sp.start_char == sr.start_char
                    and sp.end_char == sr.end_char
                ):
                    sections_map.setdefault(sr.name, []).append(idx)
                    break
        doc.sections = sections_map  # type: ignore[attr-defined]

        # 单据状态推进：DRAFT → IN_REVIEW
        if doc.pipeline_state == DocumentState.DRAFT:
            try:
                assert_document_transition(
                    doc.pipeline_state, DocumentState.IN_REVIEW
                )
                doc.pipeline_state = DocumentState.IN_REVIEW
            except Exception as exc:  # noqa: BLE001
                logger.warning("单据状态推进失败（segmenter）: %s", exc)

        doc.record_step(self.step_name)
        logger.info(
            "case_id=%s segmenter 输出: paragraphs=%d sentences=%d sections=%d",
            doc.case_id,
            len(paragraphs),
            len(doc.spans) - len(paragraphs) - len(section_ranges),
            len(section_ranges),
        )
        return doc


__all__ = [
    "SegmenterProcessor",
    "split_into_paragraphs",
    "split_paragraph_into_sentences",
    "detect_sections",
    "detect_section_title",
    "SectionRange",
]
