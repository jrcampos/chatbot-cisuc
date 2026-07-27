import os
import json
from pathlib import Path
from datetime import datetime
from typing import Any
import re

from preprocessing.paths import RAW_DIR, LOGS_DIR

# Configuration
class Config:
    """
    Centralized configuration settings for the CISUC web crawler and data ingestion.
    
    Contains directory paths, rate limits, domain info, and the site's structural map.
    """

    BASE_DATA_DIR = RAW_DIR
    BASE_LOG_DIR = LOGS_DIR
    RATE_LIMIT_DELAY = 1  # seconds between requests
    DOMAIN = "www.cisuc.uc.pt"
    MAIN_PAGE_URL = "https://www.cisuc.uc.pt/en"
    MAX_EXTERNAL_LINKS_PER_PAGE = 1
    
    # Submenu structure mapping for directory organization
    SUBMENU_STRUCTURE: dict[str, list[str]] = {
        "The Centre": [
            "History",
            "Governing Bodies",
            "Internal Organisation",
            "Contacts",
            "Brand",
            "Acknowledgements"
        ],
        "Research": [
            "Overview",
            "AC",
            "BAI",
            "CMS",
            "IS",
            "NCS",
            "SSE",
            "Resources",
            "Laboratories"
        ],
        "Advanced Training": ["Advanced Training"],
        "Research Integrity": ["Research Integrity"]
    }


class FolderStructureBuilder:
    """
    Manages the creation and retrieval of the local filesystem hierarchy for storing scraped data.
    """

    def __init__(
            self,
            data_dir: str | Path | None = None,
    ) -> None:
        """Initialize the builder with the base data directory."""
        self.data_dir = (
            Path(data_dir)
            if data_dir is not None
            else Path(Config.BASE_DATA_DIR)
        )
        self.created_folders: set[str] = set()
    
    def create_folder_tree(self) -> bool:
        """
        Construct the complete directory tree based on the predefined site structure.
        
        Returns:
            bool: True if the tree was successfully created, False otherwise.
        """
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            
            for category, submenus in Config.SUBMENU_STRUCTURE.items():
                if isinstance(submenus, list):
                    for submenu in submenus:
                        folder_path = self._build_category_path(category, submenu)
                        folder_path.mkdir(parents=True, exist_ok=True)
                        self.created_folders.add(str(folder_path))
            
            return True
        except Exception as e:
            Logger.log_error(f"Failed to create folder tree: {e}")
            return False
    
    def get_category_path(self, category: str, subcategory: str | None = None) -> Path:
        """
        Retrieve the Path object for a specific category and optional subcategory.

        Args:
            category: The top-level category name.
            subcategory: The optional second-level subcategory name.

        Returns:
            Path: The resulting Path object.
        """
        return self._build_category_path(category, subcategory)
    
    def _build_category_path(self, category: str, subcategory: str | None = None) -> Path:
        """
        Internally construct a filesystem path from category and subcategory names.
        
        Args:
            category: The name of the category.
            subcategory: Optional subcategory name.
            
        Returns:
            Path: The constructed Path.
        """
        category_slug = self._get_safe_slug(category)
        path = self.data_dir / category_slug
        
        if subcategory:
            subcategory_slug = self._get_safe_slug(subcategory)
            path = path / subcategory_slug
        
        return path
    
    def get_safe_filename(self, text: str, extension: str = "json") -> str:
        """
        Generate a filesystem-safe filename from arbitrary text.
        
        Args:
            text: The text to convert into a filename.
            extension: The desired file extension (default: 'json').

        Returns:
            str: The sanitized filename string.
        """
        # Remove special characters, convert to lowercase, replace spaces with hyphens
        filename = self._get_safe_slug(text)
        return f"{filename}.{extension}"
    
    @staticmethod
    def _get_safe_slug(text: str) -> str:
        """
        Convert a string into a URL-safe and filesystem-safe slug.
        
        Args:
            text: Input string.

        Returns:
            str: A lowercase string with only alphanumeric characters and hyphens.
        """
        # Convert to lowercase
        text = text.lower()
        # Remove special characters, keep only alphanumeric, hyphens, spaces
        text = re.sub(r'[^a-z0-9\s\-]', '', text)
        # Replace multiple spaces with single hyphen
        text = re.sub(r'\s+', '-', text)
        # Remove leading/trailing hyphens
        text = text.strip('-')
        return text
    
    def get_log_dir(self) -> Path:
        """
        Retrieve and ensure the existence of the logging directory.
        
        Returns:
            Path: The Path object for the log directory.
        """
        log_dir = Config.BASE_LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir


class Logger:
    """
    A class-level logger providing structured output to both the console and a log file.
    
    Tracks execution statistics and generates a final report.
    """
    
    _log_file: Path | None = None
    _start_time: datetime | None = None
    _stats: dict[str, Any] = {
        "pages_scraped": 0,
        "pages_failed": 0,
        "total_paragraphs": 0,
        "total_images": 0,
        "total_links": 0,
        "external_pages_processed": 0,
        "failed_urls": []
    }
    
    @classmethod
    def initialize(cls, output_dir: Path | None = None) -> None:
        """
        Prepare the logger by creating a timestamped log file and recording the start time.
        """
        log_dir = FolderStructureBuilder().get_log_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cls._log_file = log_dir / f"scraping_report_{timestamp}.txt"
        cls._start_time = datetime.now()
        cls._output_dir = output_dir or Config.BASE_DATA_DIR
        
        # Write header
        cls._write_to_file(f"SCRAPING REPORT - {cls._start_time.isoformat()}\n")
        cls._write_to_file("=" * 80 + "\n\n")
    
    @classmethod
    def log_info(cls, message: str) -> None:
        """
        Log an informational message.
        
        Args:
            message: The content to log.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[INFO {timestamp}] {message}"
        print(log_message)
        cls._write_to_file(log_message + "\n")
    
    @classmethod
    def log_success(cls, message: str) -> None:
        """
        Log a success message.
        
        Args:
            message: The content to log.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[SUCCESS {timestamp}] {message}"
        print(f"[OK] {message}")
        cls._write_to_file(log_message + "\n")
    
    @classmethod
    def log_error(cls, message: str) -> None:
        """
        Log an error message.
        
        Args:
            message: The content to log.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[ERROR {timestamp}] {message}"
        print(f"[ERROR] {message}")
        cls._write_to_file(log_message + "\n")
    
    @classmethod
    def log_warning(cls, message: str) -> None:
        """
        Log a warning message.
        
        Args:
            message: The content to log.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[WARNING {timestamp}] {message}"
        print(f"[WARNING] {message}")
        cls._write_to_file(log_message + "\n")
    
    @classmethod
    def update_stats(cls, **kwargs: Any) -> None:
        """
        Update the internal tracking statistics.
        
        Args:
            **kwargs: Key-value pairs matching statistic keys to increment or extend.
        """
        for key, value in kwargs.items():
            if key in cls._stats:
                if isinstance(cls._stats[key], int):
                    cls._stats[key] += value
                elif isinstance(cls._stats[key], list):
                    if isinstance(value, list):
                        cls._stats[key].extend(value)
                    else:
                        cls._stats[key].append(value)
    
    @classmethod
    def generate_final_report(cls) -> Path | None:
        """
        Compile all tracked stats and record the end of execution into a final summary.
        
        Returns:
            Path | None: The path to the final log file, or None if not initialized.
        """
        end_time = datetime.now()
        start_time = cls._start_time if cls._start_time else datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        report = f"\n\n{'=' * 80}\nFINAL SCRAPING REPORT\n{'=' * 80}\n\n"
        report += f"Start Time: {start_time.isoformat()}\n"
        report += f"End Time: {end_time.isoformat()}\n"
        report += f"Duration: {duration:.2f} seconds\n\n"
        
        report += "STATISTICS:\n"
        report += f"  Pages Scraped: {cls._stats['pages_scraped']}\n"
        report += f"  Pages Failed: {cls._stats['pages_failed']}\n"
        report += f"  Total Paragraphs Extracted: {cls._stats['total_paragraphs']}\n"
        report += f"  Total Images Found: {cls._stats['total_images']}\n"
        report += f"  Total Links Found: {cls._stats['total_links']}\n"
        report += f"  External Pages Processed: {cls._stats['external_pages_processed']}\n\n"
        
        if cls._stats['failed_urls']:
            report += "FAILED URLS:\n"
            for failed in cls._stats['failed_urls']:
                report += f"  - {failed}\n"
            report += "\n"
        
        report += f"Output Directory: {cls._output_dir}/\n"
        report += f"Log Directory: {Config.BASE_LOG_DIR}/\n"
        
        cls._write_to_file(report)
        print(report)
        
        return cls._log_file
    
    @classmethod
    def _write_to_file(cls, message: str) -> None:
        """
        Write a raw message string to the initialized log file.
        
        Args:
            message: The message string to write.
        """
        if cls._log_file is None:
            return

        try:
            with open(cls._log_file, 'a', encoding='utf-8') as f:
                f.write(message)
        except Exception as e:
            print(f"Failed to write to log file: {e}")
