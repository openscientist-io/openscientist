"""Tests for pdf_generator module."""

from pathlib import Path
from unittest.mock import patch

import pytest

from shandy.pdf_generator import ReportPDF, markdown_to_pdf


class TestProcessInlineFormatting:
    """Tests for inline markdown processing."""

    def test_removes_backtick_code(self):
        result = ReportPDF._process_inline_formatting("Use `pandas` for data")
        assert "`" not in result
        assert "pandas" in result

    def test_preserves_bold(self):
        result = ReportPDF._process_inline_formatting("This is **bold** text")
        assert "**bold**" in result

    def test_preserves_italic(self):
        result = ReportPDF._process_inline_formatting("This is *italic* text")
        assert "*italic*" in result

    def test_plain_text_unchanged(self):
        text = "No formatting here"
        assert ReportPDF._process_inline_formatting(text) == text


class TestMarkdownToPdf:
    """Tests for full markdown-to-PDF conversion."""

    @pytest.fixture
    def sample_md(self, tmp_path: Path) -> Path:
        md = tmp_path / "report.md"
        md.write_text(
            "# Test Report\n\n"
            "## Introduction\n\n"
            "This is a test paragraph.\n\n"
            "### Details\n\n"
            "- Item one\n"
            "- Item two\n\n"
            "1. First\n"
            "2. Second\n\n"
            "```python\nprint('hello')\n```\n\n"
            "---\n\n"
            "#### Subheading\n\n"
            "Final paragraph with **bold** and `code`.\n"
        )
        return md

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            markdown_to_pdf(tmp_path / "nonexistent.md")

    @patch("shandy.pdf_generator.ReportPDF")
    def test_calls_correct_methods(self, mock_pdf_cls, sample_md, tmp_path):
        """Verify that the parser calls the right PDF methods for each element."""
        mock_pdf = mock_pdf_cls.return_value

        markdown_to_pdf(sample_md, pdf_path=tmp_path / "out.pdf")

        # Should have been called for "# Test Report"
        mock_pdf.add_title.assert_called()
        # Should have been called for "## Introduction"
        mock_pdf.add_heading_1.assert_called()
        # Should have been called for "### Details"
        mock_pdf.add_heading_2.assert_called()
        # Should have been called for "#### Subheading"
        mock_pdf.add_heading_3.assert_called()
        # Should have list items
        assert mock_pdf.add_list_item.call_count >= 4  # 2 unordered + 2 ordered
        # Should have code block
        mock_pdf.add_code_block.assert_called_once()
        # Should have paragraph
        mock_pdf.add_paragraph.assert_called()
        # Footer
        mock_pdf.add_shandy_footer.assert_called_once()

    @patch("shandy.pdf_generator.ReportPDF")
    def test_no_footer_option(self, mock_pdf_cls, sample_md, tmp_path):
        mock_pdf = mock_pdf_cls.return_value
        markdown_to_pdf(sample_md, pdf_path=tmp_path / "out.pdf", add_footer=False)
        mock_pdf.add_shandy_footer.assert_not_called()

    @patch("shandy.pdf_generator.ReportPDF")
    def test_default_pdf_path(self, mock_pdf_cls, sample_md):
        result = markdown_to_pdf(sample_md)
        assert result == sample_md.with_suffix(".pdf")

    @patch("shandy.pdf_generator.ReportPDF")
    def test_custom_pdf_path(self, mock_pdf_cls, sample_md, tmp_path):
        custom = tmp_path / "custom_name.pdf"
        result = markdown_to_pdf(sample_md, pdf_path=custom)
        assert result == custom

    @patch("shandy.pdf_generator.ReportPDF")
    def test_horizontal_rule_parsed(self, mock_pdf_cls, tmp_path):
        md = tmp_path / "hr.md"
        md.write_text("Text above\n\n---\n\nText below\n")
        mock_pdf = mock_pdf_cls.return_value
        markdown_to_pdf(md, pdf_path=tmp_path / "out.pdf")
        # hr triggers pdf.line() call
        mock_pdf.line.assert_called()
