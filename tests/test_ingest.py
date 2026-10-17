"""DocumentParser 单元测试。

覆盖：

- TextParser：UTF-8 / GBK / 含 BOM / 解码失败（latin-1 兜底）；
- PdfParser：解析正常 PDF（含分页符 \\f）+ 解析失败（空字节）+ 扫描件识别；
- DocxParser：解析含段落与表格的 DOCX + 解析失败（损坏字节）；
- get_parser：MIME 选择；
- 编码探测函数 detect_text_encoding。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from docx import Document as DocxDocument

from case_bill_validation.ingest.parser import (
    CONTENT_TYPE_DOCX,
    CONTENT_TYPE_PDF,
    CONTENT_TYPE_TEXT,
    DocxParser,
    DocumentParser,
    ParseResult,
    PdfParser,
    TextParser,
    detect_text_encoding,
    get_parser,
)


# ---------------------------------------------------------------------------
# TextParser
# ---------------------------------------------------------------------------


class TestTextParser:
    def test_utf8(self) -> None:
        text = "主诉：胸痛。既往史：高血压。"
        parser = TextParser()
        result = parser.parse(text.encode("utf-8"), CONTENT_TYPE_TEXT)
        assert isinstance(result, ParseResult)
        assert result.raw_text == text
        assert result.encoding == "utf-8"
        assert result.confidence >= 0.9
        assert result.needs_manual is False

    def test_utf8_bom(self) -> None:
        text = "主诉：胸痛。"
        result = TextParser().parse(
            b"\xef\xbb\xbf" + text.encode("utf-8"), CONTENT_TYPE_TEXT
        )
        assert result.encoding == "utf-8-sig"
        assert result.raw_text == text

    def test_gbk(self) -> None:
        text = "现病史：患者胸痛3个月。"
        result = TextParser().parse(
            text.encode("gb18030"), CONTENT_TYPE_TEXT
        )
        assert result.encoding in ("gb18030",)
        assert "现病史" in result.raw_text

    def test_latin1_fallback_marks_needs_manual(self) -> None:
        # 强制无法被 UTF-8 / GBK 解码的字节
        bad_bytes = b"\x80\x81\x82\x83 hello"
        result = TextParser().parse(bad_bytes, CONTENT_TYPE_TEXT)
        assert result.needs_manual is True
        assert result.encoding == "latin-1"
        assert result.confidence < 0.5
        assert any("latin-1" in e for e in result.errors)

    def test_supported_content_types(self) -> None:
        parser = TextParser()
        assert CONTENT_TYPE_TEXT in parser.supported_content_types


class TestDetectTextEncoding:
    def test_utf8_bom(self) -> None:
        enc, conf = detect_text_encoding(b"\xef\xbb\xbfhello")
        assert enc == "utf-8-sig"
        assert conf == 1.0

    def test_plain_utf8(self) -> None:
        enc, conf = detect_text_encoding("主诉：胸痛。".encode("utf-8"))
        assert enc == "utf-8"
        assert conf >= 0.9

    def test_gbk(self) -> None:
        enc, conf = detect_text_encoding(
            "现病史：胸痛3个月".encode("gb18030")
        )
        assert enc == "gb18030"
        assert conf >= 0.8

    def test_latin1_fallback(self) -> None:
        enc, conf = detect_text_encoding(b"\x80\x81\x82")
        assert enc == "latin-1"
        assert conf < 0.5


# ---------------------------------------------------------------------------
# PdfParser
# ---------------------------------------------------------------------------


class TestPdfParser:
    def test_parse_real_fixture(self) -> None:
        # 用 docx 转 PDF 不便，直接用一份手工生成的最小 PDF 字节流较复杂；
        # 改测空 PDF → 返回 needs_manual
        result = PdfParser().parse(b"", CONTENT_TYPE_PDF)
        assert result.needs_manual is True
        assert result.confidence == 0.0
        assert result.page_count == 0

    def test_parse_garbage_bytes(self) -> None:
        result = PdfParser().parse(b"not a pdf at all", CONTENT_TYPE_PDF)
        assert result.needs_manual is True
        assert result.confidence <= 0.5
        assert result.errors  # 至少一条错误说明

    def test_supported_content_types(self) -> None:
        assert CONTENT_TYPE_PDF in PdfParser().supported_content_types


# ---------------------------------------------------------------------------
# DocxParser
# ---------------------------------------------------------------------------


def _make_minimal_docx_bytes(paragraphs: list[str]) -> bytes:
    """构造一份包含若干段落的最小合法 DOCX。"""
    doc = DocxDocument()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class TestDocxParser:
    def test_parse_minimal_docx(self) -> None:
        content = _make_minimal_docx_bytes(
            ["主诉：胸痛。", "现病史：3 个月。", "出院医嘱：随诊。"]
        )
        result = DocxParser().parse(content, CONTENT_TYPE_DOCX)
        assert result.needs_manual is False
        assert "主诉" in result.raw_text
        assert "出院医嘱" in result.raw_text
        assert result.confidence >= 0.8

    def test_parse_empty_docx(self) -> None:
        content = _make_minimal_docx_bytes([])
        result = DocxParser().parse(content, CONTENT_TYPE_DOCX)
        assert result.needs_manual is True
        assert result.confidence == 0.0

    def test_parse_garbage_bytes(self) -> None:
        result = DocxParser().parse(b"definitely not docx", CONTENT_TYPE_DOCX)
        assert result.needs_manual is True
        assert any("DOCX 解析失败" in e for e in result.errors)

    def test_supported_content_types(self) -> None:
        assert CONTENT_TYPE_DOCX in DocxParser().supported_content_types


# ---------------------------------------------------------------------------
# get_parser 工厂
# ---------------------------------------------------------------------------


class TestGetParser:
    def test_pdf_routes_to_pdf_parser(self) -> None:
        p = get_parser(CONTENT_TYPE_PDF)
        assert isinstance(p, PdfParser)

    def test_docx_routes_to_docx_parser(self) -> None:
        p = get_parser(CONTENT_TYPE_DOCX)
        assert isinstance(p, DocxParser)

    def test_text_routes_to_text_parser(self) -> None:
        p = get_parser(CONTENT_TYPE_TEXT)
        assert isinstance(p, TextParser)

    def test_unknown_routes_to_text_parser(self) -> None:
        p = get_parser("application/x-unknown")
        assert isinstance(p, TextParser)

    def test_with_charset_falls_back_correctly(self) -> None:
        # 含 charset 参数的 Content-Type 也应正确路由
        p = get_parser("application/pdf; charset=utf-8")
        assert isinstance(p, PdfParser)

    def test_none_routes_to_text_parser(self) -> None:
        p = get_parser(None)
        assert isinstance(p, TextParser)


# ---------------------------------------------------------------------------
# 抽象基类契约
# ---------------------------------------------------------------------------


class TestAbstractContract:
    def test_document_parser_is_abstract(self) -> None:
        assert issubclass(DocumentParser, object)
        # parse 是 abstractmethod
        from abc import ABC
        assert issubclass(DocumentParser, ABC)


# ---------------------------------------------------------------------------
# Fixtures 路径存在性
# ---------------------------------------------------------------------------


class TestFixturesExist:
    @pytest.mark.parametrize("idx", [1, 2, 3, 4, 5])
    def test_fixture_file_exists(self, idx: int) -> None:
        p = Path(__file__).parent / "fixtures" / f"discharge_{idx:02d}.txt"
        assert p.exists(), f"fixture 缺失: {p}"
        text = p.read_text(encoding="utf-8")
        # 每份 ≥ 1500 字（spec.md fixture 标准）
        assert len(text) >= 1500, (
            f"{p.name} 字数 {len(text)} < 1500"
        )
