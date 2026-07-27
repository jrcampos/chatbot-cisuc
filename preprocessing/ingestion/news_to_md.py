"""
News to Markdown Converter
Converts all JSON news files to a single optimized markdown file for VDB ingestion.
"""

import json
import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Any
import sys


class NewsToMarkdown:
    """
    Converter for JSON news files to optimized markdown format for Vector Databases.
    
    This class handles the discovery, parsing, and formatting of news articles
    stored as JSON files, combining them into a single markdown archive with
    appropriate metadata for retrieval systems.
    """
    
    def __init__(self) -> None:
        """
        Initialize the converter with default paths and statistics.
        """
        self.project_root = Path(__file__).parent
        self.news_source_dir = self.project_root / "data" / "news"
        self.output_dir = self.project_root / "data" / "news" / "news_md"
        self.logs_dir = self.project_root / "logs"
        
        # Statistics
        self.stats: dict[str, Any] = {
            "total_files_found": 0,
            "successfully_processed": 0,
            "skipped_files": 0,
            "total_characters": 0,
            "valid_images": 0,
            "missing_images": 0,
            "processed_files": [],
            "skipped_files_details": []
        }
        
        self.logger: logging.Logger | None = None
        self.setup_logging()
        
    def setup_logging(self) -> None:
        """
        Configure logging to both a timestamped file and the console.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"news_to_md_{timestamp}.txt"
        log_filepath = self.logs_dir / log_filename
        
        # Create logger
        self.logger = logging.getLogger("NewsToMarkdown")
        self.logger.setLevel(logging.DEBUG)
        
        # File handler
        file_handler = logging.FileHandler(log_filepath, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        self.logger.info("="*60)
        self.logger.info("NEWS TO MARKDOWN CONVERSION REPORT")
        self.logger.info(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info("="*60)
        
    def validate_directories(self) -> bool:
        """
        Create necessary directories and validate existence of required paths.
        
        Returns:
            bool: True if all directories are valid and accessible, False otherwise.
        """
        if self.logger is None:
            return False

        self.logger.info("\nVALIDATING DIRECTORIES...")
        
        # Check source directory
        if not self.news_source_dir.exists():
            self.logger.error(f"Source directory not found: {self.news_source_dir}")
            return False
        self.logger.info(f"✓ Source directory: {self.news_source_dir}")
        
        # Create output directory if needed
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"✓ Output directory: {self.output_dir}")
        except Exception as e:
            self.logger.error(f"Failed to create output directory: {e}")
            return False
        
        # Verify logs directory
        if not self.logs_dir.exists():
            self.logger.error(f"Logs directory not found: {self.logs_dir}")
            return False
        self.logger.info(f"✓ Logs directory: {self.logs_dir}")
        
        return True
    
    def discover_files(self) -> list[Path]:
        """
        Discover all relevant JSON files in the news source directory.
        
        Filters out common system files like 'desktop.ini'.
        
        Returns:
            list[Path]: A sorted list of Path objects to the discovered JSON files.
        """
        if self.logger is None:
            return []

        self.logger.info("\nDISCOVERING FILES...")
        
        json_files = sorted(self.news_source_dir.glob("*.json"))
        
        # Filter out desktop.ini if present
        json_files = [f for f in json_files if f.name != "desktop.ini"]
        
        self.stats["total_files_found"] = len(json_files)
        self.logger.info(f"✓ Found {len(json_files)} JSON files")
        
        return json_files
    
    def parse_json(self, filepath: Path) -> dict[str, Any] | None:
        """
        Safely parse a JSON file into a dictionary.
        
        Args:
            filepath: Path to the JSON file to be parsed.
            
        Returns:
            dict[str, Any] | None: The parsed JSON data as a dictionary, 
                                   or None if an error occurred during reading or parsing.
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        except json.JSONDecodeError as e:
            if self.logger:
                self.logger.debug(f"JSON decode error in {filepath.name}: {e}")
            self.stats["skipped_files_details"].append({
                "file": filepath.name,
                "reason": f"JSON decode error: {str(e)[:50]}"
            })
            return None
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Error reading {filepath.name}: {e}")
            self.stats["skipped_files_details"].append({
                "file": filepath.name,
                "reason": f"Read error: {str(e)[:50]}"
            })
            return None
    
    def get_field(self, data: dict[str, Any], field: str, default: str = "") -> str:
        """
        Safely extract and sanitize a field from JSON data.
        
        Args:
            data: JSON data dictionary to extract from.
            field: Name of the field to extract.
            default: Default value to return if the field is missing or empty.
            
        Returns:
            str: The sanitized field value as a string.
        """
        value = data.get(field, default)
        if not value or (isinstance(value, str) and value.strip() == ""):
            return default
        return str(value).strip()
    
    def format_image_link(self, image_url: str, title: str) -> str:
        """
        Format an image URL as a markdown image link or a placeholder if missing.
        
        Args:
            image_url: The URL of the image.
            title: The title or alt text for the image.
            
        Returns:
            str: A formatted markdown image string.
        """
        if not image_url or image_url == "No image found" or image_url.strip() == "":
            self.stats["missing_images"] += 1
            return "![Image not found](No image)"
        
        # Valid URL
        self.stats["valid_images"] += 1
        # Escape special characters in title
        safe_title = title.replace('"', '\\"')
        return f'![{title}]({image_url} "{safe_title}")'
    
    def format_markdown_entry(self, data: dict[str, Any], filename: str) -> str:
        """
        Format a single news entry as markdown with VDB-compatible metadata.
        
        Args:
            data: The parsed JSON data for a single news item.
            filename: The name of the original source file for metadata tracking.
            
        Returns:
            str: The news item formatted as a markdown entry with YAML frontmatter.
        """
        title = self.get_field(data, "title", "Untitled News")
        date = self.get_field(data, "date", "Unknown Date")
        image_url = self.get_field(data, "image_url", "No image found")
        content = self.get_field(data, "content", "")
        
        # Build markdown entry with YAML frontmatter
        entry = f"""---
title: {title}
date: {date}
source: {filename}
---

{self.format_image_link(image_url, title)}

*Published: {date}*

{content}

---

"""
        return entry
    
    def process_all_files(self) -> str:
        """
        Process all discovered JSON files and combine them into a single markdown string.
        
        Returns:
            str: The combined markdown content containing all processed news items.
        """
        if self.logger is None:
            return ""

        self.logger.info("\n⚙️  PROCESSING FILES...")
        
        json_files = self.discover_files()
        combined_markdown = ""
        
        # Add header
        combined_markdown += f"""# CISUC News Archive

**Total News Articles:** {self.stats['total_files_found']}  
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**Format:** Optimized for Vector Database Ingestion

---

"""
        
        for idx, filepath in enumerate(json_files, 1):
            # Parse JSON
            data = self.parse_json(filepath)
            if data is None:
                self.stats["skipped_files"] += 1
                continue
            
            # Format entry
            try:
                entry = self.format_markdown_entry(data, filepath.name)
                combined_markdown += entry
                
                self.stats["successfully_processed"] += 1
                self.stats["processed_files"].append(filepath.name)
                self.stats["total_characters"] += len(entry)
                
                # Progress indicator
                if idx % 50 == 0:
                    self.logger.info(f"  Processing: {idx}/{len(json_files)} files...")
                    
            except Exception as e:
                self.logger.debug(f"Error formatting {filepath.name}: {e}")
                self.stats["skipped_files"] += 1
                self.stats["skipped_files_details"].append({
                    "file": filepath.name,
                    "reason": f"Formatting error: {str(e)[:50]}"
                })
        
        self.logger.info(f"✓ Processed: {self.stats['successfully_processed']}/{len(json_files)} files")
        
        return combined_markdown
    
    def write_output(self, markdown_content: str) -> bool:
        """
        Write the combined markdown content to a file in the output directory.
        
        Args:
            markdown_content: The full markdown string to be written.
            
        Returns:
            bool: True if writing was successful, False otherwise.
        """
        if self.logger is None:
            return False

        self.logger.info("\n💾 WRITING OUTPUT...")
        
        output_file = self.output_dir / "news_combined.md"
        
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            
            file_size_kb = output_file.stat().st_size / 1024
            self.logger.info(f"✓ Output file: {output_file}")
            self.logger.info(f"✓ File size: {file_size_kb:.2f} KB")
            
            return True
        except Exception as e:
            self.logger.error(f"❌ Failed to write output file: {e}")
            return False
    
    def generate_report(self) -> None:
        """
        Generate and log a summary report of the conversion process.
        """
        if self.logger is None:
            return

        self.logger.info("\n📊 CONVERSION SUMMARY")
        self.logger.info("="*60)
        
        self.logger.info(f"\n📈 STATISTICS:")
        self.logger.info(f"  Total Files Found: {self.stats['total_files_found']}")
        self.logger.info(f"  Successfully Processed: {self.stats['successfully_processed']}")
        self.logger.info(f"  Skipped (Errors): {self.stats['skipped_files']}")
        self.logger.info(f"  Total Characters: {self.stats['total_characters']:,}")
        
        self.logger.info(f"\n🖼️  IMAGE STATISTICS:")
        self.logger.info(f"  Valid Image URLs: {self.stats['valid_images']}")
        self.logger.info(f"  Missing/Not Found: {self.stats['missing_images']}")
        
        if self.stats["skipped_files"] > 0:
            self.logger.info(f"\n⚠️  SKIPPED FILES ({self.stats['skipped_files']}):")
            for skip_info in self.stats["skipped_files_details"][:10]:  # Show first 10
                self.logger.info(f"  - {skip_info['file']}: {skip_info['reason']}")
            
            if len(self.stats["skipped_files_details"]) > 10:
                self.logger.info(f"  ... and {len(self.stats['skipped_files_details']) - 10} more")
        
        self.logger.info(f"\n✅ STATUS: COMPLETED SUCCESSFULLY")
        self.logger.info("="*60)
    
    def run(self) -> bool:
        """
        Execute the full conversion pipeline.
        
        Returns:
            bool: True if the entire process completed without critical errors, False otherwise.
        """
        try:
            # Validate directories
            if not self.validate_directories():
                if self.logger:
                    self.logger.error("❌ Directory validation failed")
                return False
            
            # Process files
            markdown_content = self.process_all_files()
            
            # Write output
            if not self.write_output(markdown_content):
                return False
            
            # Generate report
            self.generate_report()
            
            if self.logger:
                self.logger.info("\n🎉 Conversion completed successfully!")
            return True
            
        except Exception as e:
            if self.logger:
                self.logger.error(f"❌ Unexpected error: {e}")
                self.logger.exception("Full traceback:")
            return False


def main() -> int:
    """
    Main entry point for the news to markdown converter script.
    
    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    converter = NewsToMarkdown()
    success = converter.run()
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
