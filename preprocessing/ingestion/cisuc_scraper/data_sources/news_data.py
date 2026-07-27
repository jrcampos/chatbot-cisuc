"""
News Data Source

This module implements the NewsDataSource class, which coordinates the retrieval 
of news articles from the CISUC website using a Selenium-based NewsCrawler.
It manages the crawling lifecycle and subsequent format conversion of fetched articles.
"""

import sys
import os
from pathlib import Path
from typing import Any

from ..extractors.utils import Logger
from ..crawlers.utils.news_retriever import NewsCrawler
from ..converters import FormatManager

from preprocessing.paths import resolve_raw_path

class NewsDataSource:
    """
    Data source responsible for orchestrating the discovery and extraction of news articles.
    
    Coordinates the execution of the NewsCrawler and handles the conversion of 
    extracted JSON items into final storage formats.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """
        Initialize the news data source with configuration parameters.

        Args:
            config: A dictionary containing system-wide configuration settings.
        """
        self.config: dict[str, Any] = config.get("sources", {}).get("news", {})
        self.enabled: bool = self.config.get("enabled", True)
        self.output_dir = resolve_raw_path(
            self.config.get("output_dir", "news")
        )
        self.base_url: str = self.config.get("base_url", "https://www.cisuc.uc.pt/en/posts")
        self.clean_text: bool = self.config.get("clean_text", True)

        # Output format configuration
        output_format = config.get("output", {}).get("format", "json")
        self.format_manager = FormatManager(output_format=output_format)

        # Lazy initialization for the crawler to avoid unnecessary overhead
        self.crawler: NewsCrawler | None = None

    def initialize(self) -> bool:
        """
        Prepare the news crawler and ensure the output directory exists.

        Returns:
            bool: True if initialization was successful, False otherwise.
        """
        Logger.log_info("Initializing News Data Source...")

        # Create output directory
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        try:
            self.crawler = NewsCrawler(
                self.base_url, format_manager=self.format_manager
            )
            Logger.log_success("News Data Source initialized")
            return True
        except Exception as e:
            Logger.log_error(f"Error initializing news crawler: {e}")
            return False

    def fetch(self) -> bool:
        """
        Discover news article links and retrieve their content.
        
        This method executes the discovery crawl, retrieves content in parallel,
        and converts the resulting data based on the output configuration.

        Returns:
            bool: True if the fetching and conversion process completed without critical errors.
        """
        if not self.enabled:
            Logger.log_info("News data source is disabled")
            return True

        if not self.crawler:
            Logger.log_error("News crawler not initialized")
            return False

        Logger.log_info("Fetching news articles...")

        try:
            # Crawl for news links
            Logger.log_info(f"Crawling news links from {self.base_url}...")
            news_links = self.crawler.crawl_news_links()

            if not news_links:
                Logger.log_warning("No news links found")
                return False

            Logger.log_success(f"Found {len(news_links)} news articles")

            # Retrieve and save news articles using parallel processing
            Logger.log_info("Retrieving news article content (parallel processing)...")
            self.crawler.retrieve_and_save_news_parallel(news_links, self.output_dir)

            Logger.log_success("News articles fetched successfully")

            # Convert format if needed
            Logger.log_info(
                f"Processing news format: {self.format_manager.format.value}"
            )
            conversion_result = self.format_manager.convert_news(
                news_source_dir=self.output_dir, output_base_dir=self.output_dir
            )

            if not conversion_result["success"]:
                Logger.log_warning(
                    f"Format conversion warnings: {conversion_result['errors']}"
                )
            else:
                Logger.log_success("News format conversion completed")

            return True

        except Exception as e:
            Logger.log_error(f"Error fetching news articles: {e}")
            return False
        finally:
            # Ensure crawler resources are released
            if self.crawler:
                try:
                    self.crawler.close()
                except Exception as e:
                    Logger.log_warning(f"Error closing news crawler: {e}")

    def close(self) -> None:
        """
        Explicitly release crawler resources and clean up temporary state.
        """
        if self.crawler:
            try:
                self.crawler.close()
            except Exception as e:
                Logger.log_warning(f"Error closing news crawler: {e}")
