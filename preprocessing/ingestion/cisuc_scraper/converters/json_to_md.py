#!/usr/bin/env python3
"""
JSON to Markdown Converter

This module provides tools for converting structured JSON data into Markdown format.
It includes specialized support for parsing and formatting BibTeX entries found 
within JSON fields, ensuring research publications are represented accurately.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any


class BibtexParser:
    """
    Parser for BibTeX entries using Regular Expressions to extract structured citation data.
    """
    
    # Common BibTeX field patterns
    # Matches: field={value}, field="value", or field=value
    FIELD_PATTERN = r'(\w+)\s*=\s*(?:[{"]([^{}"]*)[}"]|([^,\n]+))'
    ENTRY_TYPE_PATTERN = r'@(\w+)\s*\{([^,]+)'
    
    @staticmethod
    def parse(bibtex_string: str) -> dict[str, str]:
        """
        Parse a BibTeX entry string and extract its fields into a dictionary.
        
        Args:
            bibtex_string: The raw BibTeX formatted string.
            
        Returns:
            dict[str, str]: A dictionary containing the parsed fields and entry type.
        """
        if not bibtex_string or not isinstance(bibtex_string, str):
            return {}
        
        result: dict[str, str] = {}
        
        # Extract entry type and citation key
        entry_match = re.search(BibtexParser.ENTRY_TYPE_PATTERN, bibtex_string)
        if entry_match:
            result['type'] = entry_match.group(1).lower()
            result['citation_key'] = entry_match.group(2).strip()
        
        # Extract all fields
        fields = re.findall(BibtexParser.FIELD_PATTERN, bibtex_string, re.MULTILINE | re.DOTALL)
        
        for field_match in fields:
            field_name = field_match[0]
            # Get value from either group 1 (quoted) or group 2 (unquoted)
            field_value = field_match[1] if field_match[1] else field_match[2]
            
            # Clean up the value (remove extra whitespace, newlines)
            cleaned_value = re.sub(r'\s+', ' ', field_value.strip())
            
            # Skip empty fields
            if cleaned_value:
                result[field_name.lower()] = cleaned_value
        
        return result
    
    @staticmethod
    def format_to_markdown(bibtex_data: dict[str, str]) -> str:
        """
        Convert a dictionary of parsed BibTeX data into a readable Markdown string.
        
        Args:
            bibtex_data: Dictionary containing parsed BibTeX fields.
            
        Returns:
            str: The data formatted as a Markdown string.
        """
        if not bibtex_data:
            return "*Invalid or empty BibTeX*"
        
        lines: list[str] = []
        
        # Publication type
        pub_type = bibtex_data.get('type', 'unknown')
        type_labels = {
            'article': 'Journal Article',
            'inproceedings': 'Conference Paper',
            'incollection': 'Book Chapter',
            'book': 'Book',
            'phdthesis': 'PhD Thesis',
            'mastersthesis': 'Master\'s Thesis',
            'techreport': 'Technical Report',
            'misc': 'Miscellaneous'
        }
        lines.append(f"**Type:** {type_labels.get(pub_type, pub_type.title())}")
        
        # Authors
        if 'author' in bibtex_data:
            authors = bibtex_data['author'].replace(' and ', ', ')
            lines.append(f"**Authors:** {authors}")
        
        # Title
        if 'title' in bibtex_data:
            lines.append(f"**Title:** {bibtex_data['title']}")
        
        # Year
        if 'year' in bibtex_data:
            lines.append(f"**Year:** {bibtex_data['year']}")
        
        # Journal or Booktitle
        if 'journal' in bibtex_data:
            lines.append(f"**Journal:** {bibtex_data['journal']}")
        elif 'booktitle' in bibtex_data:
            lines.append(f"**Conference:** {bibtex_data['booktitle']}")
        
        # Volume and Number
        volume_parts: list[str] = []
        if 'volume' in bibtex_data:
            volume_parts.append(f"Vol. {bibtex_data['volume']}")
        if 'number' in bibtex_data:
            volume_parts.append(f"No. {bibtex_data['number']}")
        if volume_parts:
            lines.append(f"**Volume/Number:** {', '.join(volume_parts)}")
        
        # Pages
        if 'pages' in bibtex_data:
            pages = bibtex_data['pages'].replace('&mdash;', '-').replace('--', '-')
            if pages:
                lines.append(f"**Pages:** {pages}")
        
        # DOI
        if 'doi' in bibtex_data:
            doi = bibtex_data['doi']
            if doi:
                lines.append(f"**DOI:** [{doi}](https://doi.org/{doi})")
        
        # Publisher
        if 'publisher' in bibtex_data:
            lines.append(f"**Publisher:** {bibtex_data['publisher']}")
        
        # Editor
        if 'editor' in bibtex_data:
            editor = bibtex_data['editor']
            if editor:
                lines.append(f"**Editor:** {editor}")
        
        # Keywords
        if 'keywords' in bibtex_data:
            keywords = bibtex_data['keywords']
            if keywords:
                lines.append(f"**Keywords:** {keywords}")
        
        # Citation key (for reference)
        if 'citation_key' in bibtex_data:
            lines.append(f"**Citation Key:** `{bibtex_data['citation_key']}`")
        
        return '\n'.join(lines)


class JSONtoMarkdownConverter:
    """
    Converter for transforming JSON files into hierarchical Markdown documents.
    """
    
    def __init__(
        self,
        json_dir: str | None = None,
        output_dir: str | None = None,
        logger: logging.Logger | None = None
    ) -> None:
        """
        Initialize the converter with target directories and an optional logger.
        
        Args:
            json_dir: Source directory for JSON files.
            output_dir: Destination directory for Markdown files.
            logger: Optional logging instance.
        """
        # Set default paths
        self.json_dir = Path(json_dir) if json_dir else Path("data") / "api"
        self.output_dir = Path(output_dir) if output_dir else Path("data") / "api" / "markdown"
        
        # Create output directory if it doesn't exist
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Setup logging
        if logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)
            if not self.logger.handlers:
                handler = logging.StreamHandler()
                formatter = logging.Formatter(
                    '[%(levelname)s] %(name)s - %(message)s'
                )
                handler.setFormatter(formatter)
                self.logger.addHandler(handler)
                self.logger.setLevel(logging.INFO)
        else:
            self.logger = logger
        
        # Initialize BibTeX parser
        self.bibtex_parser = BibtexParser()
    
    def format_key(self, key: str) -> str:
        """
        Transform a raw dictionary key into a readable label (e.g., snake_case to Title Case).
        
        Args:
            key: The raw key string.

        Returns:
            str: The formatted label.
        """
        return key.replace("_", " ").title()
    
    def value_to_markdown(self, value: Any, indent_level: int = 0, key_name: str = "") -> str:
        """
        Recursively convert an arbitrary Python value into its Markdown representation.
        
        Args:
            value: The data value to convert.
            indent_level: Current indentation depth.
            key_name: Key associated with the value, used for semantic detection.
            
        Returns:
            str: The formatted Markdown string.
        """
        if value is None:
            return "*None*"
        elif isinstance(value, bool):
            return "✓ Yes" if value else "✗ No"
        elif isinstance(value, str):
            # Check if this is a BibTeX field
            if key_name.lower() in ['bibtex', 'bibtexdata', 'bibtex_data']:
                return self._format_bibtex(value, indent_level)
            # Remove HTML tags if present
            if value.startswith("<"):
                return self._clean_html(value)
            return value
        elif isinstance(value, (int, float)):
            return str(value)
        elif isinstance(value, list):
            if not value:
                return "Empty"
            return self._list_to_markdown(value, indent_level)
        elif isinstance(value, dict):
            return self._dict_to_markdown(value, indent_level)
        else:
            return str(value)
    
    def _format_bibtex(self, bibtex_string: str, indent_level: int = 0) -> str:
        """
        Specifically format a BibTeX string into indented Markdown.
        
        Args:
            bibtex_string: Raw BibTeX entry.
            indent_level: Indentation depth.
            
        Returns:
            str: Indented Markdown citation.
        """
        indent = "  " * indent_level
        
        # Parse the BibTeX
        parsed = self.bibtex_parser.parse(bibtex_string)
        
        if not parsed:
            return "*Invalid BibTeX*"
        
        # Format to Markdown
        formatted = self.bibtex_parser.format_to_markdown(parsed)
        
        # Add indentation to each line
        lines = formatted.split('\n')
        indented_lines = [f"\n{indent}{line}" for line in lines]
        
        return "".join(indented_lines)
    
    def _clean_html(self, text: str) -> str:
        """
        Remove common HTML tags and entities from a string.
        
        Args:
            text: Raw text potentially containing HTML.

        Returns:
            str: Sanitized text.
        """
        text = re.sub(r'<[^>]+>', '', text)
        text = text.replace("&nbsp;", " ")
        text = text.replace("&quot;", '"')
        text = text.replace("&amp;", "&")
        return text.strip()
    
    def _list_to_markdown(self, items: list[Any], indent_level: int = 0) -> str:
        """
        Convert a list of items into Markdown list format.
        
        Args:
            items: The list to convert.
            indent_level: Current indentation depth.

        Returns:
            str: Formatted Markdown list.
        """
        indent = "  " * indent_level
        result: list[str] = []
        
        # Check if it's a list of dictionaries or simple values
        if items and isinstance(items[0], dict):
            # For lists of dictionaries, show each as a sub-item
            for idx, item in enumerate(items, 1):
                result.append(f"\n{indent}**Item {idx}:**")
                result.append(self._dict_to_markdown(item, indent_level + 1))
        else:
            # For simple lists
            for item in items:
                if isinstance(item, str):
                    result.append(f"\n{indent}- {item}")
                elif isinstance(item, dict):
                    result.append(f"\n{indent}- {self._dict_to_markdown(item, indent_level + 1)}")
                else:
                    result.append(f"\n{indent}- {self.value_to_markdown(item, indent_level + 1)}")
        
        return "".join(result)
    
    def _dict_to_markdown(self, obj: dict[str, Any], indent_level: int = 0) -> str:
        """
        Convert a dictionary into a key-value Markdown representation.
        
        Args:
            obj: The dictionary to convert.
            indent_level: Current indentation depth.

        Returns:
            str: Formatted Markdown.
        """
        indent = "  " * indent_level
        result: list[str] = []
        
        for key, value in obj.items():
            formatted_key = self.format_key(key)
            
            if isinstance(value, (dict, list)) and value:
                result.append(f"\n{indent}**{formatted_key}:**")
                result.append(self.value_to_markdown(value, indent_level + 1, key_name=key))
            else:
                formatted_value = self.value_to_markdown(value, indent_level, key_name=key)
                result.append(f"\n{indent}**{formatted_key}:** {formatted_value}")
        
        return "".join(result)
    
    def json_to_markdown(self, json_data: Any, title: str = "") -> str:
        """
        Transform a full JSON object (list or dict) into a complete Markdown document string.
        
        Args:
            json_data: The parsed JSON data.
            title: An optional title for the document.
            
        Returns:
            str: The final Markdown document string.
        """
        md_lines: list[str] = []
        
        if title:
            md_lines.append(f"# {title}\n")
        
        # Check if JSON has wrapper structure like {"data": [...]}
        if isinstance(json_data, dict) and "data" in json_data and isinstance(json_data["data"], list):
            # Extract the list from the wrapper
            json_data = json_data["data"]
            self.logger.debug("Detected API wrapper structure, extracting 'data' array")
        
        if isinstance(json_data, list):
            for item in json_data:
                if isinstance(item, dict):
                    # If item has a 'name' or 'title' field, use it as sub-header
                    if 'name' in item:
                        md_lines.append(f"### {item['name']}\n")
                    elif 'title' in item:
                        md_lines.append(f"### {item['title']}\n")
                    
                    md_lines.append(self._dict_to_markdown(item, indent_level=0))
                else:
                    md_lines.append(self.value_to_markdown(item, indent_level=0))
                
                md_lines.append("\n" + "-" * 80 + "\n")
        
        elif isinstance(json_data, dict):
            md_lines.append(self._dict_to_markdown(json_data, indent_level=0))
        
        else:
            md_lines.append(self.value_to_markdown(json_data, indent_level=0))
        
        return "\n".join(md_lines)
    
    def convert_file(self, json_filename: str, output_filename: str | None = None) -> bool:
        """
        Convert a single JSON file into its Markdown counterpart.
        
        Args:
            json_filename: Name of the input JSON file within json_dir.
            output_filename: Optional name for the output Markdown file.
            
        Returns:
            bool: True if conversion was successful, False otherwise.
        """
        json_path = self.json_dir / json_filename
        
        if not json_path.exists():
            self.logger.error(f"JSON file not found: {json_path}")
            return False
        
        if output_filename is None:
            output_filename = json_filename.replace(".json", ".md")
        
        output_path = self.output_dir / output_filename
        
        try:
            # Read JSON file
            self.logger.info(f"Reading: {json_path}")
            with open(json_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
            
            # Convert to Markdown
            self.logger.debug(f"Converting: {json_filename}")
            title = self.format_key(json_filename.replace(".json", ""))
            markdown_content = self.json_to_markdown(json_data, title=title)
            
            # Write to Markdown file
            self.logger.info(f"Writing: {output_path}")
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            
            self.logger.info(f"Successfully converted: {json_filename} -> {output_filename}")
            return True
        
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON format in {json_filename}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error processing {json_filename}: {e}", exc_info=True)
            return False
    
    def convert_all(self) -> None:
        """
        Automate the conversion of all JSON files present in the source directory.
        """
        if not self.json_dir.exists():
            self.logger.error(f"JSON directory not found: {self.json_dir}")
            return
        
        json_files = list(self.json_dir.glob("*.json"))
        
        if not json_files:
            self.logger.warning(f"No JSON files found in: {self.json_dir}")
            return
        
        self.logger.info(f"Starting conversion of {len(json_files)} JSON files...")
        
        successful = 0
        failed = 0
        
        for json_file in sorted(json_files):
            if self.convert_file(json_file.name):
                successful += 1
            else:
                failed += 1
        
        self.logger.info("=" * 80)
        self.logger.info(f"Conversion Summary: Successful: {successful}, Failed: {failed}")
        self.logger.info(f"Output directory: {self.output_dir.absolute()}")
        self.logger.info("=" * 80)


def main() -> None:
    """
    Main execution entry point for the converter.
    """
    print("=" * 80)
    print("JSON to Markdown Converter")
    print("=" * 80 + "\n")
    
    # Create converter instance
    converter = JSONtoMarkdownConverter(
        json_dir="data/api",
        output_dir="data/api/markdown"
    )
    
    # Convert all JSON files
    try:
        converter.convert_all()
    except UnicodeEncodeError:
        # Handle encoding issues on Windows
        import io
        import sys
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')


if __name__ == "__main__":
    main()
