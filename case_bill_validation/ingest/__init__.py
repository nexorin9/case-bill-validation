"""文档解析层：PDF / Word / 纯文本 → BillingCaseDocument.raw_text。

模块：

- :class:`DocumentParser` —— 抽象基类（``parse(content, content_type) → ParseResult``）；
- :class:`PdfParser` —— PyPDF2 实现的 PDF 解析；
- :class:`DocxParser` —— python-docx 实现的 Word 解析；
- :class:`TextParser` —— utf-8 / gbk 自动探测的纯文本解析；
- :func:`get_parser` —— 按 ``content_type`` 选择具体解析器。

低置信度标记（``needs_manual``）：扫描件 / 编码混杂 / 解析失败 → 上层
segmenter 会继承 ``doc.needs_manual`` 并把后续字段标 FLAGGED + source=NEEDS_MANUAL。
"""

from .parser import (
    CONTENT_TYPE_PDF,
    CONTENT_TYPE_DOCX,
    CONTENT_TYPE_TEXT,
    DocxParser,
    DocumentParser,
    ParseResult,
    PdfParser,
    TextParser,
    detect_text_encoding,
    get_parser,
)

__all__ = [
    "CONTENT_TYPE_PDF",
    "CONTENT_TYPE_DOCX",
    "CONTENT_TYPE_TEXT",
    "DocxParser",
    "DocumentParser",
    "ParseResult",
    "PdfParser",
    "TextParser",
    "detect_text_encoding",
    "get_parser",
]
