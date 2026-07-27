#!/usr/bin/env python3
"""
Format Manager

This module orchestrates the conversion of scraped data between different formats.
It routes data through specialized converters (e.g., NewsToMarkdown, JSONtoMarkdownConverter)
based on the desired output configuration (JSON, Markdown, or both).
"""

import json
import logging
from pathlib import Path
from typing import Any
from enum import Enum

from .news_to_md import NewsToMarkdown
from .json_to_md import JSONtoMarkdownConverter


class OutputFormat(Enum):
    """
    Supported output formats for the scraping system.
    """
    JSON = "json"
    MARKDOWN = "markdown"
    BOTH = "both"


class FormatManager:
    """
    Manager for orchestrating and routing data format conversions.
    
    Determines which converters to execute based on the configured output format
    and handles the generation of final data artifacts.
    """

    def __init__(
        self, output_format: str = "json", logger: logging.Logger | None = None
    ) -> None:
        """
        Initialize the FormatManager with a target output format.

        Args:
            output_format: Desired output format ("json", "markdown", or "both").
            logger: Optional logger instance for recording operations.

        Raises:
            ValueError: If an unsupported output format is provided.
        """
        # Validate and set output format
        format_str = output_format.lower()
        try:
            self.format = OutputFormat(format_str)
        except ValueError:
            raise ValueError(
                f"Invalid output format: {output_format}. Must be 'json', 'markdown', or 'both'"
            )

        # Setup logging
        if logger is None:
            self.logger = logging.getLogger(self.__class__.__name__)
            if not self.logger.handlers:
                handler = logging.StreamHandler()
                formatter = logging.Formatter("[%(levelname)s] %(name)s - %(message)s")
                handler.setFormatter(formatter)
                self.logger.addHandler(handler)
                self.logger.setLevel(logging.INFO)
        else:
            self.logger = logger

        self.logger.info(f"FormatManager initialized with format: {self.format.value}")

    def should_generate_json(self) -> bool:
        """
        Check if the current configuration requires JSON output.
        
        Returns:
            bool: True if JSON should be generated.
        """
        return self.format in (OutputFormat.JSON, OutputFormat.BOTH)

    def should_generate_markdown(self) -> bool:
        """
        Check if the current configuration requires Markdown output.
        
        Returns:
            bool: True if Markdown should be generated.
        """
        return self.format in (OutputFormat.MARKDOWN, OutputFormat.BOTH)

    def convert_news(
        self, news_source_dir: str, output_base_dir: str
    ) -> dict[str, Any]:
        """
        Coordinate the conversion of news data from JSON to the configured format(s).

        Args:
            news_source_dir: Directory containing individual JSON news files.
            output_base_dir: The base directory for writing outputs.

        Returns:
            dict[str, Any]: A dictionary containing conversion results and file paths.
        """
        self.logger.info(f"Converting news data (format: {self.format.value})")

        result: dict[str, Any] = {
            "format": self.format.value,
            "json_files": [],
            "markdown_files": [],
            "success": False,
            "errors": [],
        }

        try:
            # JSON files are already in place from crawler
            news_json_dir = Path(news_source_dir)
            if news_json_dir.exists():
                json_files = list(news_json_dir.glob("*.json"))
                result["json_files"] = [f.name for f in json_files]
                self.logger.info(f"Found {len(json_files)} JSON news files")

            # Convert to Markdown if needed
            if self.should_generate_markdown():
                self.logger.info("Converting news to Markdown format...")
                try:
                    converter = NewsToMarkdown(
                        news_source_dir=news_source_dir,
                        output_dir=str(Path(output_base_dir) / "markdown"),
                    )
                    conversion_ok = converter.run()
                    if conversion_ok:
                        result["markdown_files"].append("news_combined.md")
                        self.logger.info("News Markdown conversion completed")
                    else:
                        error_msg = "Failed to convert news to Markdown: conversion returned False"
                        self.logger.error(error_msg)
                        result["errors"].append(error_msg)
                except Exception as e:
                    error_msg = f"Failed to convert news to Markdown: {str(e)}"
                    self.logger.error(error_msg)
                    result["errors"].append(error_msg)

            result["success"] = len(result["errors"]) == 0
            return result

        except Exception as e:
            error_msg = f"Error in news conversion: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result["errors"].append(error_msg)
            return result

    def convert_api_data(
        self, api_json_dir: str, output_base_dir: str
    ) -> dict[str, Any]:
        """
        Coordinate the conversion of API-sourced data (users, projects) to the configured format(s).

        Args:
            api_json_dir: Directory containing individual API JSON files.
            output_base_dir: The base directory for writing outputs.

        Returns:
            dict[str, Any]: A dictionary containing conversion results and file paths.
        """
        self.logger.info(f"Converting API data (format: {self.format.value})")

        result: dict[str, Any] = {
            "format": self.format.value,
            "json_files": [],
            "markdown_files": [],
            "success": False,
            "errors": [],
        }

        try:
            # JSON files are already in place from API
            api_json_path = Path(api_json_dir)
            if api_json_path.exists():
                json_files = list(api_json_path.glob("*.json"))
                result["json_files"] = [f.name for f in json_files]
                self.logger.info(f"Found {len(json_files)} JSON API files")

            # Convert to Markdown if needed
            if self.should_generate_markdown():
                self.logger.info("Converting API data to Markdown format...")
                try:
                    markdown_output_dir = Path(output_base_dir) / "markdown"
                    converter = JSONtoMarkdownConverter(
                        json_dir=api_json_dir, output_dir=str(markdown_output_dir)
                    )

                    # Convert each JSON file
                    for json_file in result["json_files"]:
                        try:
                            md_filename = json_file.replace(".json", ".md")
                            if converter.convert_file(json_file, md_filename):
                                result["markdown_files"].append(md_filename)
                        except Exception as e:
                            error_msg = f"Failed to convert {json_file}: {str(e)}"
                            self.logger.warning(error_msg)
                            result["errors"].append(error_msg)

                    self.logger.info(
                        f"API Markdown conversion completed: {len(result['markdown_files'])} files"
                    )

                except Exception as e:
                    error_msg = f"Failed to initialize API converter: {str(e)}"
                    self.logger.error(error_msg)
                    result["errors"].append(error_msg)

            result["success"] = len(result["errors"]) == 0
            return result

        except Exception as e:
            error_msg = f"Error in API data conversion: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result["errors"].append(error_msg)
            return result

    def save(self, data: list[dict[str, Any]], output_dir: str, filename: str) -> bool:
        """
        Save a list of data items into a unified JSON file with a wrapper structure.

        Args:
            data: List of dictionaries to be saved.
            output_dir: Target directory for the JSON file.
            filename: The base filename (without extension).

        Returns:
            bool: True if saving was successful, False otherwise.
        """
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            output_file = Path(output_dir) / f"{filename}.json"

            # Create unified structure: {filename: [list of items]}
            unified_data = {filename: data}

            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(unified_data, f, indent=2, ensure_ascii=False)

            self.logger.info(f"Saved {len(data)} items to {output_file}")
            return True
        except Exception as e:
            self.logger.error(f"Error saving {filename}: {e}")
            return False

    def convert_publications(
        self, publications_source_dir: str, output_base_dir: str
    ) -> dict[str, Any]:
        """
        Coordinate the conversion of publications data to configured format(s).

        Args:
            publications_source_dir: Directory containing JSON publications files.
            output_base_dir: Base output directory.

        Returns:
            dict[str, Any]: Dictionary with conversion results and file paths.
        """
        self.logger.info(f"Converting publications data (format: {self.format.value})")

        result: dict[str, Any] = {
            "format": self.format.value,
            "json_files": [],
            "markdown_files": [],
            "success": False,
            "errors": [],
        }

        try:
            # JSON files are already in place from publications retriever
            publications_json_dir = Path(publications_source_dir)
            if publications_json_dir.exists():
                json_files = list(publications_json_dir.glob("*.json"))
                result["json_files"] = [f.name for f in json_files]
                self.logger.info(f"Found {len(json_files)} JSON publications files")

            # Convert to Markdown if needed
            if self.should_generate_markdown():
                self.logger.info(
                    "Markdown conversion for publications not yet implemented"
                )
                # TODO: Implement custom PublicationsToMarkdown converter here

            result["success"] = len(result["errors"]) == 0
            return result

        except Exception as e:
            error_msg = f"Error in publications conversion: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            result["errors"].append(error_msg)
            return result

    def get_output_info(self) -> dict[str, Any]:
        """
        Retrieve information about the current output configuration.

        Returns:
            dict[str, Any]: Dictionary containing format details and capabilities.
        """
        return {
            "format": self.format.value,
            "generates_json": self.should_generate_json(),
            "generates_markdown": self.should_generate_markdown(),
            "description": self._get_format_description(),
        }

    def _get_format_description(self) -> str:
        """
        Get a human-readable description of the current output format.
        
        Returns:
            str: The format description.
        """
        descriptions = {
            OutputFormat.JSON: "Only JSON output files",
            OutputFormat.MARKDOWN: "Only Markdown output files (converted from JSON)",
            OutputFormat.BOTH: "Both JSON and Markdown output files",
        }
        return descriptions.get(self.format, "Unknown format")
