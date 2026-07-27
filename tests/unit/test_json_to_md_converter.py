"""
Unit tests for JSONtoMarkdownConverter module
Tests JSON to Markdown conversion with various data types
"""

import pytest
import logging
from pathlib import Path
from ingestion.cisuc_scraper.converters.json_to_md import JSONtoMarkdownConverter


class TestJSONtoMarkdownConverterFormatKey:
    """Test key formatting for Markdown"""

    def test_format_snake_case_to_title_case(self):
        """Test conversion of snake_case to Title Case"""
        converter = JSONtoMarkdownConverter()

        assert converter.format_key("user_name") == "User Name"
        assert converter.format_key("api_token") == "Api Token"
        assert converter.format_key("simple") == "Simple"

    def test_format_key_with_numbers(self):
        """Test formatting keys containing numbers"""
        converter = JSONtoMarkdownConverter()

        assert converter.format_key("api_v2_token") == "Api V2 Token"


class TestJSONtoMarkdownConverterValueConversion:
    """Test value to Markdown conversion"""

    def test_convert_none_value(self):
        """Test conversion of None values"""
        converter = JSONtoMarkdownConverter()
        result = converter.value_to_markdown(None)

        assert "*None*" in result

    def test_convert_boolean_true(self):
        """Test conversion of boolean True"""
        converter = JSONtoMarkdownConverter()
        result = converter.value_to_markdown(True)

        assert "✓" in result or "Yes" in result

    def test_convert_boolean_false(self):
        """Test conversion of boolean False"""
        converter = JSONtoMarkdownConverter()
        result = converter.value_to_markdown(False)

        assert "✗" in result or "No" in result

    def test_convert_string(self):
        """Test conversion of string values"""
        converter = JSONtoMarkdownConverter()
        result = converter.value_to_markdown("Hello World")

        assert result == "Hello World"

    def test_convert_number_int(self):
        """Test conversion of integer"""
        converter = JSONtoMarkdownConverter()
        result = converter.value_to_markdown(42)

        assert result == "42"

    def test_convert_number_float(self):
        """Test conversion of float"""
        converter = JSONtoMarkdownConverter()
        result = converter.value_to_markdown(3.14)

        assert "3.14" in result


class TestJSONtoMarkdownConverterDictConversion:
    """Test dictionary to Markdown conversion"""

    def test_convert_simple_dict(self):
        """Test conversion of simple dictionary"""
        converter = JSONtoMarkdownConverter()
        data = {"name": "John", "email": "john@example.com", "active": True}
        result = converter.json_to_markdown(data, title="User")

        assert "# User" in result
        assert "**Name:**" in result
        assert "**Email:**" in result
        assert "john@example.com" in result

    def test_convert_nested_dict(self):
        """Test conversion of nested dictionaries"""
        converter = JSONtoMarkdownConverter()
        data = {"user": {"name": "John", "contact": {"email": "john@example.com"}}}
        result = converter.json_to_markdown(data)

        assert "**User:**" in result
        assert "**Contact:**" in result
        assert "john@example.com" in result


class TestJSONtoMarkdownConverterListConversion:
    """Test list to Markdown conversion"""

    def test_convert_simple_list(self):
        """Test conversion of simple list"""
        converter = JSONtoMarkdownConverter()
        data = {"tags": ["python", "testing", "markdown"]}
        result = converter.json_to_markdown(data)

        assert "python" in result
        assert "testing" in result
        assert "markdown" in result

    def test_convert_list_of_dicts(self, sample_json_list_data):
        """Test conversion of list of dictionaries"""
        converter = JSONtoMarkdownConverter()
        result = converter.json_to_markdown(sample_json_list_data, title="Items")

        assert "# Items" in result
        assert "Total Items:" not in result
        assert "\n## Item 1\n" not in result
        assert "### Item 1" in result
        assert "First item description" in result

    def test_convert_empty_list(self):
        """Test conversion of empty list"""
        converter = JSONtoMarkdownConverter()
        data = {"items": []}
        result = converter.json_to_markdown(data)

        assert "Empty" in result


class TestJSONtoMarkdownConverterJsonArray:
    """Test conversion of JSON arrays"""

    def test_convert_json_array_to_markdown(self, sample_json_list_data):
        """Test conversion of JSON array (top-level list)"""
        converter = JSONtoMarkdownConverter()
        result = converter.json_to_markdown(sample_json_list_data, title="All Items")

        assert "# All Items" in result
        assert "Total Items:" not in result
        assert "\n## Item 1\n" not in result
        assert "\n## Item 2\n" not in result
        assert "### Item 1" in result
        assert "### Item 2" in result


class TestJSONtoMarkdownConverterHTMLCleaning:
    """Test HTML tag removal from values"""

    def test_clean_html_tags(self):
        """Test removal of HTML tags from string values"""
        converter = JSONtoMarkdownConverter()

        html_string = "<p>This is <b>bold</b> text</p>"
        result = converter.value_to_markdown(html_string)

        assert "<" not in result
        assert ">" not in result
        assert "This is bold text" in result


class TestJSONtoMarkdownConverterInitialization:
    """Test converter initialization and configuration"""

    def test_default_output_dirs(self, temp_dir):
        """Test default output directories are created"""
        converter = JSONtoMarkdownConverter(
            json_dir=str(temp_dir / "input"), output_dir=str(temp_dir / "output")
        )

        assert converter.output_dir.exists()

    def test_custom_logger(self, temp_dir):
        """Test initialization with custom logger"""
        logger = logging.getLogger("test_logger")
        converter = JSONtoMarkdownConverter(
            json_dir=str(temp_dir / "input"),
            output_dir=str(temp_dir / "output"),
            logger=logger,
        )

        assert converter.logger == logger


class TestJSONtoMarkdownConverterFileOperations:
    """Test file conversion operations"""

    def test_convert_file_successful(self, sample_json_list_data, temp_dir):
        """Test successful conversion of a JSON file"""
        # Create input file
        json_path = temp_dir / "input"
        json_path.mkdir()
        input_file = json_path / "test.json"

        import json

        with open(input_file, "w", encoding="utf-8") as f:
            json.dump(sample_json_list_data, f)

        # Convert
        converter = JSONtoMarkdownConverter(
            json_dir=str(json_path), output_dir=str(temp_dir / "output")
        )
        success = converter.convert_file("test.json")

        assert success is True
        assert (temp_dir / "output" / "test.md").exists()

    def test_convert_nonexistent_file(self, temp_dir):
        """Test handling of non-existent JSON file"""
        converter = JSONtoMarkdownConverter(
            json_dir=str(temp_dir), output_dir=str(temp_dir / "output")
        )
        success = converter.convert_file("nonexistent.json")

        assert success is False

    def test_convert_invalid_json_file(self, temp_dir):
        """Test handling of invalid JSON file"""
        json_path = temp_dir / "input"
        json_path.mkdir()
        input_file = json_path / "invalid.json"

        # Write invalid JSON
        input_file.write_text("{ invalid json }")

        converter = JSONtoMarkdownConverter(
            json_dir=str(json_path), output_dir=str(temp_dir / "output")
        )
        success = converter.convert_file("invalid.json")

        assert success is False


class TestJSONtoMarkdownConverterComplexData:
    """Test conversion of complex nested data structures"""

    def test_convert_complex_nested_structure(self, sample_json_api_data):
        """Test conversion of complex nested structure"""
        converter = JSONtoMarkdownConverter()
        result = converter.json_to_markdown(sample_json_api_data, title="API Data")

        assert "# API Data" in result
        assert "John Doe" in result
        assert "jane@example.com" in result

    def test_format_preserves_data_integrity(self, sample_json_api_data):
        """Test that all important data is present in output"""
        converter = JSONtoMarkdownConverter()
        result = converter.json_to_markdown(sample_json_api_data)

        # Check all user data is present
        assert "1" in result  # IDs
        assert "John Doe" in result
        assert "Jane Smith" in result
        assert "john@example.com" in result
        assert "jane@example.com" in result
