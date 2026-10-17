"""DocumentParser 抽象 + PDF / Word / 纯文本三实现。

输入：原始字节流（``content: bytes``）+ 客户端声明的内容类型（``content_type``）。
输出：``ParseResult{raw_text, page_count, confidence, needs_manual, encoding}``。

设计要点
--------

- **解析器选择**：``get_parser(content_type)`` 按 ``application/pdf`` /
  ``application/vnd.openxmlformats-officedocument.wordprocessingml.document`` /
  ``text/plain`` 三种 MIME 选择对应实现；未知类型 → ``TextParser`` + 编码探测；
- **低置信度判定**：
  - PDF：扫描件（每页字符数 < 50）→ confidence=0.3 / needs_manual=True；
  - DOCX：解析后文本为空 → confidence=0.0 / needs_manual=True；
  - 纯文本：解码失败（含 GBK/UTF-8 混编）→ 退回 latin-1 + confidence=0.4；
- **页面切分**：PDF 用 ``\\f`` 字符分隔每页（PyPDF2 ``extract_text()`` 默认行为）；
  Word 段落以 ``\\n\\n`` 分隔，页码无直接信息 → page_count=1；
- **编码探测**（纯文本）：
  - 优先 UTF-8（带 BOM 自动识别）；
  - 其次 GB18030（GBK 超集，向后兼容 GBK/GB2312）；
  - 都失败 → 用 ``errors='replace'`` 解码 + 标 needs_manual。

权威源（spec.md §3）：入院诊断 / 出院诊断 / 手术 / 治疗等关键字段从原文抽取，
因此解析必须**保字符位置稳定**（不在解析阶段做"清洗"——那是 extractor 的工作）。
"""

from __future__ import annotations

import io
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CONTENT_TYPE_PDF: str = "application/pdf"
CONTENT_TYPE_DOCX: str = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
CONTENT_TYPE_TEXT: str = "text/plain"


@dataclass
class ParseResult:
    """解析结果。"""

    raw_text: str
    page_count: int = 1
    confidence: float = 1.0
    needs_manual: bool = False
    encoding: str = "utf-8"
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "raw_text": self.raw_text,
            "page_count": self.page_count,
            "confidence": self.confidence,
            "needs_manual": self.needs_manual,
            "encoding": self.encoding,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class DocumentParser(ABC):
    """文档解析器抽象基类。

    子类按 ``content_type`` 注册；客户端按 MIME 选择实例。
    """

    #: 该实现支持的内容类型（精确匹配）
    supported_content_types: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def parse(self, content: bytes, content_type: str) -> ParseResult:
        """解析 ``content``（按 ``content_type`` 选路）。"""
        raise NotImplementedError

    def _ok(self, raw_text: str, **kwargs: object) -> ParseResult:
        return ParseResult(raw_text=raw_text, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TextParser（纯文本；UTF-8/GBK 自动探测）
# ---------------------------------------------------------------------------


def detect_text_encoding(content: bytes) -> tuple[str, float]:
    """探测 ``content`` 的编码；返回 ``(encoding, confidence)``。

    - 开头 BOM 匹配：UTF-8-sig / UTF-16-LE / UTF-16-BE；
    - 否则尝试 UTF-8 严格解码；
    - 否则尝试 GB18030 解码（GBK 超集）；
    - 否则退回 latin-1（永不失败）+ 低 confidence。
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return ("utf-8-sig", 1.0)
    if content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
        return ("utf-16", 1.0)
    # UTF-8 严格
    try:
        content.decode("utf-8", errors="strict")
        return ("utf-8", 0.95)
    except UnicodeDecodeError:
        pass
    # GB18030（GBK 超集）
    try:
        content.decode("gb18030", errors="strict")
        return ("gb18030", 0.85)
    except UnicodeDecodeError:
        pass
    return ("latin-1", 0.4)


class TextParser(DocumentParser):
    """纯文本解析器；UTF-8/GBK 自动探测。"""

    supported_content_types: ClassVar[tuple[str, ...]] = (CONTENT_TYPE_TEXT,)

    def parse(self, content: bytes, content_type: str) -> ParseResult:
        errors: list[str] = []
        encoding, confidence = detect_text_encoding(content)
        if encoding == "latin-1":
            raw_text = content.decode("latin-1", errors="replace")
            errors.append("无法按 UTF-8/GBK 解码，已用 latin-1 兜底（替换坏字符）")
            needs_manual = True
        else:
            raw_text = content.decode(encoding, errors="replace")
            needs_manual = False
        return self._ok(
            raw_text=raw_text,
            page_count=1,
            confidence=confidence,
            needs_manual=needs_manual,
            encoding=encoding,
            errors=errors,
        )


# ---------------------------------------------------------------------------
# PdfParser（PyPDF2；扫描件低置信度）
# ---------------------------------------------------------------------------


class PdfParser(DocumentParser):
    """PDF 解析器（PyPDF2）。

    每页文本用 ``\\f`` 分隔（form feed），下游 segmenter 在切段时可按
    ``\\f`` 切页；扫描件（每页字符数 < 50）→ confidence=0.3 / needs_manual。
    """

    supported_content_types: ClassVar[tuple[str, ...]] = (CONTENT_TYPE_PDF,)

    #: 单页字符数低于此阈值视为扫描件
    SCAN_PAGE_CHAR_THRESHOLD: ClassVar[int] = 50

    def parse(self, content: bytes, content_type: str) -> ParseResult:
        try:
            from pypdf import PdfReader  # PyPDF2 v3+ 提供
        except ImportError as exc:  # pragma: no cover - 由依赖清单保障
            raise RuntimeError(
                "缺少 PyPDF2 依赖，请 pip install pypdf"
            ) from exc

        try:
            reader = PdfReader(io.BytesIO(content))
        except Exception as exc:  # noqa: BLE001
            return self._ok(
                raw_text="",
                page_count=0,
                confidence=0.0,
                needs_manual=True,
                encoding="utf-8",
                errors=[f"PDF 头解析失败: {type(exc).__name__}: {exc}"],
            )
        page_texts: list[str] = []
        errors: list[str] = []
        scan_pages = 0
        pages_iter = getattr(reader, "pages", []) or []
        for page in pages_iter:
            try:
                txt = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                errors.append(f"PDF 单页解析失败: {exc}")
                txt = ""
                scan_pages += 1
            if len(txt.strip()) < self.SCAN_PAGE_CHAR_THRESHOLD:
                scan_pages += 1
            page_texts.append(txt)
        raw_text = "\f".join(page_texts)
        # 置信度：扫描页占比越高越低
        page_count = len(reader.pages)
        if page_count == 0:
            return self._ok(
                raw_text="",
                page_count=0,
                confidence=0.0,
                needs_manual=True,
                encoding="utf-8",
                errors=["PDF 无可解析页面"],
            )
        scan_ratio = scan_pages / page_count
        if scan_ratio > 0.5:
            confidence = 0.3
            needs_manual = True
        elif scan_ratio > 0.0:
            confidence = 0.6
            needs_manual = False
            errors.append(
                f"{scan_pages}/{page_count} 页疑似扫描件（字符数过低）"
            )
        else:
            confidence = 0.95
            needs_manual = False
        return self._ok(
            raw_text=raw_text,
            page_count=page_count,
            confidence=confidence,
            needs_manual=needs_manual,
            encoding="utf-8",
            errors=errors,
        )


# ---------------------------------------------------------------------------
# DocxParser（python-docx；段落 + 表格按顺序拼接）
# ---------------------------------------------------------------------------


class DocxParser(DocumentParser):
    """DOCX 解析器（python-docx）。

    按文档顺序遍历段落与表格：段落 → ``paragraphs[i].text``，表格 →
    每行单元格 ``"\\t".join(cell.text for cell in row.cells)``；
    段落之间用 ``\\n\\n`` 分隔，表格行之间用 ``\\n`` 分隔。
    """

    supported_content_types: ClassVar[tuple[str, ...]] = (CONTENT_TYPE_DOCX,)

    def parse(self, content: bytes, content_type: str) -> ParseResult:
        try:
            from docx import Document as DocxDocument
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 python-docx 依赖，请 pip install python-docx"
            ) from exc

        errors: list[str] = []
        try:
            doc = DocxDocument(io.BytesIO(content))
        except Exception as exc:  # noqa: BLE001
            return self._ok(
                raw_text="",
                page_count=1,
                confidence=0.0,
                needs_manual=True,
                encoding="utf-8",
                errors=[f"DOCX 解析失败: {exc}"],
            )

        parts: list[str] = []
        for block in doc.element.body.iter():
            tag = block.tag.split("}", 1)[-1]
            if tag == "p":
                # 找到对应段落对象
                for p in doc.paragraphs:
                    if p._element is block:
                        if p.text.strip():
                            parts.append(p.text)
                        break
            elif tag == "tbl":
                # 找到对应表格对象
                for tbl in doc.tables:
                    if tbl._element is block:
                        rows = []
                        for row in tbl.rows:
                            rows.append(
                                "\t".join(cell.text for cell in row.cells)
                            )
                        if rows:
                            parts.append("\n".join(rows))
                        break

        raw_text = "\n\n".join(parts)
        if not raw_text.strip():
            return self._ok(
                raw_text=raw_text,
                page_count=1,
                confidence=0.0,
                needs_manual=True,
                encoding="utf-8",
                errors=["DOCX 无可见文本（可能是扫描件或仅含图片）"],
            )
        return self._ok(
            raw_text=raw_text,
            page_count=1,
            confidence=0.9,
            needs_manual=False,
            encoding="utf-8",
            errors=errors,
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

_PARSERS: list[DocumentParser] = [
    PdfParser(),
    DocxParser(),
    TextParser(),
]


def get_parser(content_type: str | None) -> DocumentParser:
    """按 MIME 选解析器；未知类型 → TextParser（编码探测兜底）。"""
    if not content_type:
        return _PARSERS[-1]
    ct = content_type.split(";", 1)[0].strip().lower()
    for p in _PARSERS:
        if ct in p.supported_content_types:
            return p
    return _PARSERS[-1]


__all__ = [
    "CONTENT_TYPE_PDF",
    "CONTENT_TYPE_DOCX",
    "CONTENT_TYPE_TEXT",
    "DocumentParser",
    "ParseResult",
    "TextParser",
    "PdfParser",
    "DocxParser",
    "get_parser",
    "detect_text_encoding",
]
