"""
Data Retriever

This module provides the DataRetriever class, which discovers the navigation 
structure of the CISUC website and builds a mapping of logical pages to URLs 
to guide subsequent web crawling.
"""

import requests
from bs4 import BeautifulSoup
from typing import Any
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DataRetriever:
    """
    Retriever for discoverable site structure and navigation links.
    
    Parses the main website navigation to identify crawlable sub-pages and 
    maintains a fallback default structure.
    """
    
    # Default fallback submenu structure
    DEFAULT_SUBMENU_STRUCTURE: dict[str, list[str]] = {
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
    
    def __init__(self, main_url: str = "https://www.cisuc.uc.pt/en") -> None:
        """
        Initialize the retriever with a target entry URL.
        
        Args:
            main_url: The entry point URL for navigation discovery.
        """
        self.main_url = main_url
        self.submenu_structure: dict[str, Any] = self.DEFAULT_SUBMENU_STRUCTURE.copy()
        self.all_links: dict[str, str] = {}
        self.topics_to_ignore: list[str] = ["People", "Projects", "Publications", "News", " ", "Login", ""]
    
    def fetch_main_page(self) -> str | None:
        """
        Retrieve the HTML content of the main entry page.
        
        Returns:
            str | None: The raw HTML content, or None if the request failed.
        """
        try:
            logger.info(f"Fetching main page: {self.main_url}")
            response = requests.get(self.main_url, timeout=10)
            
            if response.status_code == 200:
                logger.info("Main page fetched successfully")
                return response.text
            else:
                logger.error(f"Failed to retrieve main page. Status code: {response.status_code}")
                return None
        
        except requests.Timeout:
            logger.error(f"Timeout fetching main page")
            return None
        except requests.RequestException as e:
            logger.error(f"Request error fetching main page: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching main page: {e}")
            return None
    
    def extract_navigation_menu(self, html_content: str) -> bool:
        """
        Parse HTML content to identify and extract logical navigation items.
        
        Args:
            html_content: The raw HTML of the main page.
            
        Returns:
            bool: True if at least some navigation items were extracted.
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            header = soup.find(id='header')
            
            if not header:
                logger.warning("Header element not found in page")
                return False
            
            nav_items = header.find_all('li', class_='nav-item')
            logger.info(f"Found {len(nav_items)} navigation items")
            
            # Get menu items and update submenu structure
            for item in nav_items:
                link = item.find('a')
                if link and link.has_attr('href'):
                    text = link.get_text(strip=True)
                    href = link['href']
                    
                    # Skip ignored topics
                    if text not in self.topics_to_ignore:
                        self.submenu_structure[text] = href
                        logger.info(f"Added menu item: {text} -> {href}")
            
            return True
        
        except Exception as e:
            logger.error(f"Error extracting navigation menu: {e}")
            return False
    
    def build_all_links(self) -> dict[str, str]:
        """
        Construct a complete dictionary of target URLs based on the identified structure.
        
        Returns:
            dict[str, str]: Mapping of logical page names to their full URLs.
        """
        logger.info("Building all links to scrape...")
        
        self.all_links = {}
        
        for key, value in self.submenu_structure.items():
            if isinstance(value, list):
                # It's a submenu list
                for submenu in value:
                    # Build URL from main URL and submenu name
                    url = f"{self.main_url}/{submenu.replace(' ', '-').lower()}"
                    self.all_links[submenu] = url
            else:
                # It's a direct URL
                self.all_links[key] = value
        
        logger.info(f"Built {len(self.all_links)} links to scrape")
        return self.all_links
    
    def get_all_links(self) -> dict[str, str]:
        """
        High-level method to fetch the page, extract navigation, and build the link map.
        
        Returns:
            dict[str, str]: The final mapping of logical pages to target URLs.
        """
        # Fetch main page
        html_content = self.fetch_main_page()
        
        if not html_content:
            logger.warning("Using default submenu structure (failed to fetch page)")
            return self.build_all_links()
        
        # Extract navigation menu
        if not self.extract_navigation_menu(html_content):
            logger.warning("Failed to extract navigation menu, using defaults")
        
        # Build all links
        return self.build_all_links()
    
    def print_links(self) -> None:
        """
        Utility method to print all identified links to the console.
        """
        print("\nAll Links to Scrape:")
        for key, value in self.all_links.items():
            print(f"- {key}: {value}")
    
    def set_submenu_structure(self, structure: dict[str, Any]) -> None:
        """
        Manually override the navigation structure map.
        
        Args:
            structure: A dictionary mapping categories to lists of sub-pages or direct URLs.
        """
        self.submenu_structure = structure
        logger.info("Custom submenu structure set")
    
    def set_topics_to_ignore(self, topics: list[str]) -> None:
        """
        Override the list of menu text items to be excluded from discovery.
        
        Args:
            topics: A list of strings to ignore.
        """
        self.topics_to_ignore = topics
        logger.info(f"Topics to ignore set: {topics}")


if __name__ == "__main__":
    # Example usage
    retriever = DataRetriever()
    
    # Get all links
    links_map = retriever.get_all_links()
    
    # Print the links
    retriever.print_links()
    
    print(f"\nTotal links: {len(links_map)}")
