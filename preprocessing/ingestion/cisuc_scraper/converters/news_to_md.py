"""
News to Markdown Converter
Converts all JSON news files to a single optimized markdown file for VDB ingestion.
"""

import json
import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional


class NewsToMarkdown:
    """Convert JSON news files to optimized markdown format for VDB."""
    
    def __init__(self, news_source_dir: Optional[str] = None, output_dir: Optional[str] = None):
        """
        Initialize paths and logging.
        
        Args:
            news_source_dir: Directory containing JSON news files
            output_dir: Directory to save markdown output
        """
        if news_source_dir is None:
            self.project_root = Path(__file__).parent.parent.parent.parent
            self.news_source_dir = self.project_root / "src" / "data" / "news"
        else:
            self.news_source_dir = Path(news_source_dir)
        
        if output_dir is None:
            self.output_dir = self.news_source_dir / "markdown"
        else:
            self.output_dir = Path(output_dir)
        
        # Create logs directory
        self.logs_dir = self.project_root if news_source_dir is None else Path(news_source_dir).parent / "src" / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        
        # Statistics
        self.stats = {
            "total_files_found": 0,
            "successfully_processed": 0,
            "skipped_files": 0,
            "total_characters": 0,
            "valid_images": 0,
            "missing_images": 0,
            "processed_files": [],
            "skipped_files_details": []
        }
        
        self.logger = None
        self.setup_logging()
        
    def setup_logging(self):
        """Configure logging to file and console."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"news_to_md_{timestamp}.txt"
        log_filepath = self.logs_dir / log_filename
        
        # Create logger
        self.logger = logging.getLogger("NewsToMarkdown")
        self.logger.setLevel(logging.DEBUG)
        
        # Remove existing handlers
        self.logger.handlers = []
        
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
        """Create necessary directories and validate paths."""
        self.logger.info("\nVALIDATING DIRECTORIES...")
        
        # Check source directory
        if not self.news_source_dir.exists():
            self.logger.error(f"Source directory not found: {self.news_source_dir}")
            return False
        self.logger.info(f"Source directory: {self.news_source_dir}")
        
        # Create output directory if needed
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Output directory: {self.output_dir}")
        except Exception as e:
            self.logger.error(f"Failed to create output directory: {e}")
            return False
        
        return True
    
    def discover_files(self) -> List[Path]:
        """Discover all JSON files in news directory."""
        self.logger.info("\nDISCOVERING FILES...")
        
        json_files = sorted(self.news_source_dir.glob("*.json"))
        
        # Filter out desktop.ini and markdown subdirs
        json_files = [f for f in json_files if f.name != "desktop.ini" and f.is_file()]
        
        self.stats["total_files_found"] = len(json_files)
        self.logger.info(f"Found {len(json_files)} JSON files")
        
        return json_files
    
    def parse_json(self, filepath: Path) -> Optional[Dict]:
        """
        Safely parse JSON file.
        
        Args:
            filepath: Path to JSON file
            
        Returns:
            Parsed JSON dict or None if error
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        except json.JSONDecodeError as e:
            self.logger.debug(f"JSON decode error in {filepath.name}: {e}")
            self.stats["skipped_files_details"].append({
                "file": filepath.name,
                "reason": f"JSON decode error: {str(e)[:50]}"
            })
            return None
        except Exception as e:
            self.logger.debug(f"Error reading {filepath.name}: {e}")
            self.stats["skipped_files_details"].append({
                "file": filepath.name,
                "reason": f"Read error: {str(e)[:50]}"
            })
            return None
    
    def get_field(self, data: Dict, field: str, default: str = "") -> str:
        """
        Safely extract field from JSON data.
        
        Args:
            data: JSON data dictionary
            field: Field name to extract
            default: Default value if field missing or empty
            
        Returns:
            Field value as string
        """
        value = data.get(field, default)
        if not value or (isinstance(value, str) and value.strip() == ""):
            return default
        return str(value).strip()
    
    def format_image_link(self, image_url: str, title: str) -> str:
        """
        Format image URL as markdown link or "not found" text.
        
        Args:
            image_url: Image URL
            title: Image title/alt text
            
        Returns:
            Markdown image link or "Image not found" text
        """
        if not image_url or image_url == "No image found" or image_url.strip() == "":
            self.stats["missing_images"] += 1
            return "![Image not found](No image)"
        
        # Valid URL
        self.stats["valid_images"] += 1
        # Escape special characters in title
        safe_title = title.replace('"', '\\"')
        return f'![{title}]({image_url} "{safe_title}")'
    
    def format_markdown_entry(self, data: Dict, filename: str) -> str:
        """
        Format single news entry as markdown with VDB metadata.
        
        Args:
            data: Parsed JSON data
            filename: Original JSON filename
            
        Returns:
            Formatted markdown string
        """
        title = self.get_field(data, "title", "Untitled News")
        date = self.get_field(data, "date", "Unknown Date")
        image_url = self.get_field(data, "image_url", "No image found")
        content = self.get_field(data, "content", "")
        url = self.get_field(data, "url", "")
        
        # Build markdown entry with YAML frontmatter
        entry = f"""---
title: {title}
date: {date}
source: {filename}
url: {url}
---

{self.format_image_link(image_url, title)}

*Published: {date}*

{content}

---

"""
        return entry
    
    def process_all_files(self) -> str:
        """
        Process all JSON files and combine into markdown.
        Handles both individual article objects and wrapper structures like {"news": [...]}
        
        Returns:
            Combined markdown string
        """
        self.logger.info("\nPROCESSING FILES...")
        
        json_files = self.discover_files()
        combined_markdown = ""
        
        # First pass: count total articles (to update header)
        total_articles = 0
        articles_to_process = []  # List of (article_dict, source_filename)
        
        for filepath in json_files:
            data = self.parse_json(filepath)
            if data is None:
                self.stats["skipped_files"] += 1
                continue
            
            # Check if this is a wrapper object containing an array
            if isinstance(data, dict) and len(data) == 1:
                key = list(data.keys())[0]
                value = data[key]
                if isinstance(value, list):
                    # This is a wrapper structure like {"news": [...]}
                    for item in value:
                        if isinstance(item, dict):
                            articles_to_process.append((item, filepath.name))
                            total_articles += 1
                    continue
            
            # Regular single-article object
            articles_to_process.append((data, filepath.name))
            total_articles += 1
        
        # Update total count in stats
        self.stats["total_files_found"] = total_articles
        
        # Add header with correct total count
        combined_markdown += f"""# CISUC News Archive

**Total News Articles:** {total_articles}  
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**Format:** Optimized for Vector Database Ingestion

---

"""
        
        # Process all articles
        for idx, (article_data, source_filename) in enumerate(articles_to_process, 1):
            try:
                entry = self.format_markdown_entry(article_data, source_filename)
                combined_markdown += entry
                
                self.stats["successfully_processed"] += 1
                self.stats["processed_files"].append(source_filename)
                self.stats["total_characters"] += len(entry)
                
                # Progress indicator
                if idx % 50 == 0:
                    self.logger.info(f"  Processing: {idx}/{total_articles} articles...")
                    
            except Exception as e:
                self.logger.debug(f"Error formatting article from {source_filename}: {e}")
                self.stats["skipped_files"] += 1
                self.stats["skipped_files_details"].append({
                    "file": source_filename,
                    "reason": f"Formatting error: {str(e)[:50]}"
                })
        
        self.logger.info(f"Processed: {self.stats['successfully_processed']}/{total_articles} articles")
        
        return combined_markdown
    
    def write_output(self, markdown_content: str, filename: str = "news_combined.md") -> bool:
        """
        Write markdown content to output file.
        
        Args:
            markdown_content: Combined markdown string
            filename: Output filename
            
        Returns:
            True if successful, False otherwise
        """
        self.logger.info("\nWRITING OUTPUT...")
        
        output_file = self.output_dir / filename
        
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            
            file_size_kb = output_file.stat().st_size / 1024
            self.logger.info(f"Output file: {output_file}")
            self.logger.info(f"File size: {file_size_kb:.2f} KB")
            
            return True
        except Exception as e:
            self.logger.error(f"Failed to write output file: {e}")
            return False
    
    def generate_report(self):
        """Generate and log conversion report."""
        self.logger.info("\nCONVERSION SUMMARY")
        self.logger.info("="*60)
        
        self.logger.info(f"\nSTATISTICS:")
        self.logger.info(f"  Total Files Found: {self.stats['total_files_found']}")
        self.logger.info(f"  Successfully Processed: {self.stats['successfully_processed']}")
        self.logger.info(f"  Skipped (Errors): {self.stats['skipped_files']}")
        self.logger.info(f"  Total Characters: {self.stats['total_characters']:,}")
        
        self.logger.info(f"\nIMAGE STATISTICS:")
        self.logger.info(f"  Valid Image URLs: {self.stats['valid_images']}")
        self.logger.info(f"  Missing/Not Found: {self.stats['missing_images']}")
        
        if self.stats["skipped_files"] > 0:
            self.logger.info(f"\nSKIPPED FILES ({self.stats['skipped_files']}):")
            for skip_info in self.stats["skipped_files_details"][:10]:  # Show first 10
                self.logger.info(f"  - {skip_info['file']}: {skip_info['reason']}")
            
            if len(self.stats["skipped_files_details"]) > 10:
                self.logger.info(f"  ... and {len(self.stats['skipped_files_details']) - 10} more")
        
        self.logger.info(f"\nSTATUS: COMPLETED SUCCESSFULLY")
        self.logger.info("="*60)
    
    def run(self) -> bool:
        """Main execution method."""
        try:
            # Validate directories
            if not self.validate_directories():
                self.logger.error("Directory validation failed")
                return False
            
            # Process files
            markdown_content = self.process_all_files()
            
            # Write output
            if not self.write_output(markdown_content):
                return False
            
            # Generate report
            self.generate_report()
            
            self.logger.info("\nConversion completed successfully!")
            return True
            
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            self.logger.exception("Full traceback:")
            return False


def main():
    """Entry point."""
    converter = NewsToMarkdown()
    success = converter.run()
    return 0 if success else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
