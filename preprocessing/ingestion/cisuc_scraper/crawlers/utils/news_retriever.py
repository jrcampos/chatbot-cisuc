"""
News Retriever

This module provides the RetrieveNews and NewsCrawler classes for extracting 
news articles from the CISUC website. It utilizes Playwright for dynamic 
content loading (scrolling) and ThreadPoolExecutor for high-performance 
parallel extraction of individual articles.
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import os
import json
import re
from typing import Any
import logging

from playwright.sync_api import sync_playwright

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class RetrieveNews:
    """
    Handles the retrieval and parsing of individual news article pages.
    """

    def __init__(self, url: str, output_dir: str = "news_data") -> None:
        """
        Initialize the individual news retriever.

        Args:
            url: The URL of the news article.
            output_dir: Directory where the parsed JSON data will be saved.
        """
        self.url = url
        self.output_dir = output_dir

    def fetch_page(self) -> bytes:
        """
        Fetch the raw HTML content of the news page.

        Returns:
            bytes: The raw HTML content.

        Raises:
            Exception: If the page retrieval fails or times out.
        """
        try:
            response = requests.get(self.url, timeout=10)
            if response.status_code == 200:
                return response.content
            else:
                raise Exception(
                    f"Failed to retrieve the page. Status code: {response.status_code}"
                )
        except requests.Timeout:
            raise Exception(f"Timeout fetching {self.url}")
        except requests.RequestException as e:
            raise Exception(f"Request error: {e}")

    def parse_page(self, html_content: bytes) -> dict[str, str]:
        """
        Extract metadata and main content from the news article HTML.

        Args:
            html_content: The raw HTML bytes of the page.

        Returns:
            dict[str, str]: A dictionary containing title, date, image_url, content, and URL.
        """
        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # Extract the news title
            title_elem = soup.find("h3")
            title = title_elem.get_text(strip=True) if title_elem else "No title found"

            # Extract the date
            date_elem = soup.find(
                "p", class_="small color-medium-grey font-weight-semi-bold order-1"
            )
            date = date_elem.get_text(strip=True) if date_elem else "No date found"

            # Extract the image inside the specified div
            divs = soup.find_all("div", class_="col-md-7 col-lg-6 mt-24")
            image_url = "No image found"

            if len(divs) > 2:
                image_tag = divs[2].find("img")
                if image_tag:
                    image_url = image_tag["src"]

            # Extract news content
            content_div = soup.find("div", class_="container pb-0", id="news-container")
            paragraphs = content_div.find_all("p") if content_div else []
            content = "\n".join([p.get_text(strip=True) for p in paragraphs])

            return {
                "title": title,
                "date": date,
                "image_url": image_url,
                "content": content,
                "url": self.url,
            }
        except Exception as e:
            logger.error(f"Error parsing {self.url}: {e}")
            return {
                "title": "Error",
                "date": "Unknown",
                "image_url": "Error",
                "content": f"Failed to parse: {str(e)}",
                "url": self.url,
            }

    def save_data(self, news_data: dict[str, str]) -> bool:
        """
        Save the parsed news data into a local JSON file.

        Args:
            news_data: Dictionary of parsed news information.

        Returns:
            bool: True if saving was successful, False otherwise.
        """
        try:
            os.makedirs(self.output_dir, exist_ok=True)

            # Sanitize title for filename
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", news_data["title"]).replace(
                " ", "_"
            )
            filename = os.path.join(self.output_dir, f"{safe_title}.json")

            with open(filename, "w", encoding="utf-8") as f:
                json.dump(news_data, f, indent=4, ensure_ascii=False)

            logger.info(f"News data saved to {filename}")
            return True
        except Exception as e:
            logger.error(f"Error saving news data: {e}")
            return False


class NewsCrawler:
    """
    Crawler that discovery news links via Playwright and extracts content in parallel.
    """

    def __init__(self, url: str, max_workers: int = 4, format_manager: Any = None) -> None:
        """
        Initialize the news crawler with worker settings and an optional format manager.

        Args:
            url: The base URL listing the news articles.
            max_workers: Number of parallel threads for content extraction.
            format_manager: Optional FormatManager instance for unified saving.
        """
        self.url = url
        self.max_workers = max_workers
        self.format_manager = format_manager
        self.news_links: list[str] = []
        self.processed_count: int = 0
        self.failed_count: int = 0
        self.all_news_data: list[dict[str, Any]] = []

    def crawl_news_links(self) -> list[str]:
        """
        Load the news list page and scroll to the bottom using Playwright to discover all links.

        Returns:
            list[str]: A list of unique news article URLs.
        """
        logger.info(
            "Loading news page and scrolling to load all articles using Playwright..."
        )

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

                page.goto(self.url, wait_until="networkidle")

                # Scroll behavior configuration
                scroll_pause_time = 2.5
                stability_threshold = 3
                max_scroll_time = 5 * 60

                last_height = page.evaluate("document.body.scrollHeight")
                stable_count = 0
                scroll_attempts = 0
                start_time = time.time()

                logger.info(
                    f"Starting scroll with stability_threshold={stability_threshold}, max_time={max_scroll_time}s"
                )

                while stable_count < stability_threshold:
                    elapsed_time = time.time() - start_time
                    if elapsed_time > max_scroll_time:
                        logger.warning(
                            f"Scroll timeout reached after {elapsed_time:.1f}s"
                        )
                        break

                    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    page.wait_for_timeout(scroll_pause_time * 1000)

                    new_height = page.evaluate("document.body.scrollHeight")
                    scroll_attempts += 1

                    if new_height == last_height:
                        stable_count += 1
                        logger.info(
                            f"Scroll {scroll_attempts}: Height stable ({stable_count}/{stability_threshold})"
                        )
                    else:
                        stable_count = 0
                        logger.info(
                            f"Scroll {scroll_attempts}: Height changed {last_height} → {new_height}"
                        )

                    last_height = new_height

                logger.info(
                    f"Scrolling completed after {scroll_attempts} scrolls and {time.time() - start_time:.1f}s"
                )

                # Extract links after dynamic loading
                logger.info("Extracting all news links from page...")
                news_elements = page.query_selector_all("a.text-decoration-none")

                raw_links = []
                for elem in news_elements:
                    href = elem.get_attribute("href")
                    if href:
                        if href.startswith("/"):
                            href = f"https://www.cisuc.uc.pt{href}"
                        raw_links.append(href)

                # Remove duplicates while preserving order
                seen = set()
                self.news_links = []
                for link in raw_links:
                    if link not in seen:
                        seen.add(link)
                        self.news_links.append(link)

                logger.info(
                    f"Found {len(self.news_links)} unique news links after deduplication"
                )

                browser.close()
                return self.news_links

        except Exception as e:
            logger.error(f"Error crawling news links: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return []

    def _process_single_news(self, link: str, output_dir: str) -> dict[str, Any] | None:
        """
        Retrieve and parse a single news article.

        Args:
            link: URL of the news article.
            output_dir: Target directory for compatibility.

        Returns:
            dict[str, Any] | None: Parsed news data, or None if an error occurred.
        """
        try:
            retriever = RetrieveNews(link, output_dir=output_dir)
            html_content = retriever.fetch_page()
            news_data = retriever.parse_page(html_content)

            if news_data:
                self.processed_count += 1
                news_data["url"] = link
                return news_data
            else:
                self.failed_count += 1
                return None

        except Exception as e:
            logger.error(f"Error processing {link}: {e}")
            self.failed_count += 1
            return None

    def retrieve_and_save_news(self, news_links: list[str], output_dir: str) -> bool:
        """
        Process a list of news articles sequentially.

        Args:
            news_links: List of article URLs.
            output_dir: Directory to save results.

        Returns:
            bool: True if no articles failed.
        """
        os.makedirs(output_dir, exist_ok=True)

        logger.info(f"Starting sequential processing of {len(news_links)} articles...")

        for link in news_links:
            self._process_single_news(link, output_dir)

        logger.info(
            f"Sequential processing completed: {self.processed_count} succeeded, {self.failed_count} failed"
        )
        return self.failed_count == 0

    def retrieve_and_save_news_parallel(
        self, news_links: list[str], output_dir: str
    ) -> bool:
        """
        Process a list of news articles in parallel and save as a unified JSON artifact.

        Args:
            news_links: List of article URLs.
            output_dir: Target output directory.

        Returns:
            bool: True if all data was collected and saved successfully.
        """
        os.makedirs(output_dir, exist_ok=True)

        logger.info(
            f"Starting parallel processing of {len(news_links)} articles with {self.max_workers} workers..."
        )

        start_time = time.time()
        all_news_data: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._process_single_news, link, output_dir): link
                for link in news_links
            }

            completed = 0
            for future in as_completed(futures):
                try:
                    news_data = future.result()
                    if news_data is not None:
                        all_news_data.append(news_data)
                    completed += 1
                    if completed % 5 == 0:
                        logger.info(
                            f"Progress: {completed}/{len(news_links)} articles processed"
                        )
                except Exception as e:
                    logger.error(f"Error in parallel processing: {e}")

        elapsed_time = time.time() - start_time
        logger.info(
            f"Parallel processing completed in {elapsed_time:.2f}s: "
            f"{self.processed_count} succeeded, {self.failed_count} failed"
        )

        if self.format_manager and all_news_data:
            return self.format_manager.save(all_news_data, output_dir, "news")
        else:
            logger.warning("No FormatManager provided or no news data to save")
            return False

    def close(self) -> None:
        """
        Reserved for potential browser driver cleanup.
        """
        logger.info("NewsCrawler resources released")


if __name__ == "__main__":
    # Example standalone usage
    crawler_instance = NewsCrawler("https://www.cisuc.uc.pt/en/posts")

    try:
        links = crawler_instance.crawl_news_links()
        out = os.path.join(os.getcwd(), "data", "JSONs", "News")
        crawler_instance.retrieve_and_save_news_parallel(links, output_dir=out)
    finally:
        crawler_instance.close()
