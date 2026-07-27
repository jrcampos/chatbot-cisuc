"""
Static Content Data Source

This module implements the StaticContentSource class, which coordinates the 
traversal and extraction of static web pages from the CISUC website.
It utilizes a WebCrawler for page-by-page processing and a DataRetriever 
to discover initial entry points.
"""

from pathlib import Path
from typing import Any

from ..crawlers.web_crawler import WebCrawler
from ..crawlers.utils.data_retriever import DataRetriever
from ..extractors.utils import Logger

from preprocessing.paths import resolve_raw_path

class StaticContentSource:
    """
    Data source responsible for orchestrating the crawl of static website content.

    Coordinates the discovery of navigation links and ensures all relevant pages 
    are processed by the WebCrawler.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """
        Initialize the static content source with configuration parameters.

        Args:
            config: A dictionary containing system-wide configuration settings.
        """
        self.config: dict[str, Any] = (
            config.get("sources", {}).get("static", {})
        )
        self.enabled: bool = self.config.get('enabled', True)
        self.output_dir = resolve_raw_path(
            self.config.get("output_dir", "static")
        )
        self.clean_text: bool = self.config.get('clean_text', True)
        self.crawler: WebCrawler | None = None

    def initialize(self) -> bool:
        """
        Prepare the web crawler and ensure it is ready for execution.

        Returns:
            bool: True if the crawler was successfully initialized, False otherwise.
        """
        Logger.log_info("Initializing Static Content Source...")

        self.crawler = WebCrawler(
            clean_text=self.clean_text,
            output_dir=self.output_dir,
        )

        if not self.crawler.initialize():
            Logger.log_error("Failed to initialize Web Crawler")
            return False

        return True

    def fetch(self, urls_dict: dict[str, str] | None = None) -> bool:
        """
        Discover and process static content pages.

        If no URLs are provided, it automatically retrieves them from the 
        website's main navigation structure.

        Args:
            urls_dict: Optional dictionary mapping page names to URLs. 
                       If None, entry points are automatically discovered.

        Returns:
            bool: True if the crawling process completed successfully.
        """
        if not self.enabled:
            Logger.log_info("Static content source is disabled")
            return True

        Logger.log_info("Fetching static content...")

        try:
            if not self.crawler:
                Logger.log_error("Crawler not initialized")
                return False

            # If no URLs provided, retrieve them using DataRetriever
            if urls_dict is None:
                try:
                    Logger.log_info("Retrieving URLs from website navigation...")
                    retriever = DataRetriever()
                    urls_dict = retriever.get_all_links()

                    if not urls_dict:
                        Logger.log_error("DataRetriever returned no URLs")
                        return False
                except Exception as e:
                    Logger.log_error(f"Failed to retrieve URLs from DataRetriever: {e}")
                    return False

            if not urls_dict:
                Logger.log_error("No URLs to process")
                return False

            # Add URLs to crawler queue
            self.crawler.add_initial_urls(urls_dict)

            # Start crawling
            self.crawler.crawl()

            Logger.log_success("Static content fetching completed successfully")
            return True

        except Exception as e:
            Logger.log_error(f"Error fetching static content: {e}")
            return False

    def get_report(self) -> Path | None:
        """
        Retrieve the final execution report from the underlying crawler.

        Returns:
            Path | None: The Path to the report file, or None if no crawler exists.
        """
        if self.crawler:
            return self.crawler.get_report()
        return None

