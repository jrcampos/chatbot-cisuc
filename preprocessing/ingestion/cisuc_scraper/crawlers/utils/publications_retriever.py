"""
Publications Retriever

This module provides the PublicationsRetriever class for extracting research 
publications from the CISUC website. It combines Playwright for navigation 
and pagination with ThreadPoolExecutor for concurrent extraction of 
detailed metadata (abstracts, keywords) from external publisher sites via DOI links.
"""

from playwright.sync_api import sync_playwright
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
import time
from typing import Any
from ...extractors.utils import Logger


class PublicationsRetriever:
    """
    Retriever for research publications, capable of handling pagination and DOI-based metadata enrichment.
    """

    def __init__(self, base_url: str, output_dir: str, format_manager: Any) -> None:
        """
        Initialize the publications retriever.

        Args:
            base_url: The URL listing the publications.
            output_dir: Target directory for saving JSON artifacts.
            format_manager: A FormatManager instance for handling unified storage.
        """
        self.base_url = base_url
        self.output_dir = output_dir
        self.format_manager = format_manager
        self.enabled: bool = True
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

    def close(self) -> None:
        """
        Release network resources and close the active session.
        """
        Logger.log_info("Closing PublicationsRetriever resources...")
        self.session.close()

    def _fetch_ieee_doi_data(self, html: str) -> tuple[str, list[str]]:
        """
        Extract abstract and keywords specifically from IEEE Xplore HTML content.

        Args:
            html: Raw HTML string from an IEEE page.

        Returns:
            tuple[str, list[str]]: A tuple containing the abstract text and a list of keywords.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            abstract_tag = soup.find("div", class_="abstract-text")
            abstract = abstract_tag.get_text(strip=True) if abstract_tag else "N/A"

            keywords_section = soup.find("div", class_="doc-keywords")
            keywords: list[str] = []
            if keywords_section:
                keywords = [kw.get_text(strip=True) for kw in keywords_section.find_all("a")]
            return abstract, keywords
        except Exception:
            return "Error parsing IEEE data", []

    def fetch_doi_details(self, doi_link: str) -> tuple[str, list[str]]:
        """
        Follow a DOI link and attempt to extract detailed metadata from the publisher's site.

        This method is designed to be executed within a thread pool.

        Args:
            doi_link: The DOI URL to follow.

        Returns:
            tuple[str, list[str]]: Extracted abstract and keywords.
        """
        if not doi_link or doi_link == "N/A":
            return "N/A", []

        try:
            # Follow redirects to the final publisher URL
            response = self.session.get(doi_link, timeout=15, allow_redirects=True)
            final_url = response.url

            if "ieeexplore.ieee.org" in final_url:
                return self._fetch_ieee_doi_data(response.text)

            return "Publisher not supported for auto-extraction", []
        except Exception as e:
            return f"Error: {str(e)}", []

    def extract_card_data(self, card: Any) -> dict[str, Any]:
        """
        Extract basic publication metadata from a BeautifulSoup tag representing a list card.

        Args:
            card: A BeautifulSoup tag object for a publication card.

        Returns:
            dict[str, Any]: Basic metadata dictionary (type, title, DOI).
        """
        tipo_tag = card.find("h5", class_="type")
        titulo_tag = card.find("p", class_="title")

        doi_link = "N/A"
        links_div = card.find("div", class_="links")
        if links_div:
            doi_tag = links_div.find("a", href=lambda x: x and "doi.org" in x)
            if doi_tag:
                doi_link = doi_tag["href"]

        return {
            "tipo": tipo_tag.get_text(strip=True) if tipo_tag else "N/A",
            "titulo": titulo_tag.get_text(" ", strip=True) if titulo_tag else "N/A",
            "doi": doi_link,
            "abstract": "N/A",
            "keywords": []
        }

    def fetch_publications(self) -> tuple[bool, list[str]]:
        """
        Execute the main publications retrieval flow, traversing all pages.

        Utilizes Playwright for browser-based navigation and pagination, 
        and BeautifulSoup for in-page extraction.

        Returns:
            tuple[bool, list[str]]: A tuple of (success_status, list_of_all_doi_links).
        """
        if not self.enabled:
            Logger.log_info("Publications data source is disabled")
            return True, []

        all_publications: list[dict[str, Any]] = []
        all_doi_links: list[str] = []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                # Optimization: block heavy media to speed up crawling
                context = browser.new_context()
                page = context.new_page()
                page.route("**/*.{png,jpg,jpeg,css,woff2}", lambda route: route.abort())

                Logger.log_info(f"Navegando para {self.base_url}...")
                page.goto(self.base_url, wait_until="domcontentloaded")

                page_number = 1
                while True:
                    Logger.log_info(f"Extraindo página {page_number}...")
                    page.wait_for_selector("div.card-pub", timeout=15000)

                    soup = BeautifulSoup(page.content(), "html.parser")
                    cards = soup.find_all("div", class_="card-pub")

                    if not cards:
                        break

                    # 1. Extract basic data from the current page
                    current_page_pubs: list[dict[str, Any]] = []
                    for card in cards:
                        data = self.extract_card_data(card)
                        current_page_pubs.append(data)
                        if data["doi"] != "N/A":
                            all_doi_links.append(data["doi"])

                    # 2. Extract detailed DOI data asynchronously
                    with ThreadPoolExecutor(max_workers=8) as executor:
                        future_to_pub = {
                            executor.submit(self.fetch_doi_details, pub["doi"]): pub 
                            for pub in current_page_pubs if pub["doi"] != "N/A"
                        }
                        for future in future_to_pub:
                            pub = future_to_pub[future]
                            abstract, keywords = future.result()
                            pub["abstract"] = abstract
                            pub["keywords"] = keywords

                    all_publications.extend(current_page_pubs)

                    # 3. Handle Pagination
                    next_button = page.locator("#pagination a.next")
                    if next_button.is_visible():
                        classes = next_button.get_attribute("class") or ""
                        if "disabled" in classes or "inactive" in classes:
                            break

                        next_button.click()
                        time.sleep(1) 
                        page_number += 1
                    else:
                        break

                browser.close()

            # Save the final results
            save_success = self.format_manager.save(
                all_publications, self.output_dir, "publications"
            ) if self.format_manager else True

            Logger.log_info(f"Total extraído: {len(all_publications)}")
            return save_success, all_doi_links

        except Exception as e:
            Logger.log_error(f"Error fetching publications: {e}")
            return False, []

if __name__ == "__main__":
    # Example standalone initialization
    retriever_instance = PublicationsRetriever(
        base_url="https://www.cisuc.uc.pt/en/publications",
        output_dir="data/publications",
        format_manager=None 
    )
    try:
        ok, links = retriever_instance.fetch_publications()
        print(f"Finalizado com sucesso: {ok}. Links encontrados: {len(links)}")
    finally:
        retriever_instance.close()