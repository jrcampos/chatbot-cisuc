import requests
import time
from typing import Any
from urllib.parse import urlparse, urljoin
from pathlib import Path
from collections import deque

from ..extractors.utils import Config, Logger, FolderStructureBuilder
from ..extractors.content_extractor import ContentExtractor, FileGenerator


class WebCrawler:
    """
    Core web crawler responsible for traversing the CISUC website and managing discovery.
    
    Handles URL normalization, queue management, domain restrictions, and 
    coordinates with extractors to save content locally.
    """

    def __init__(
            self,
            clean_text: bool = True,
            output_dir: str | Path | None = None,
    ) -> None:
        """
        Initialize the web crawler with session settings and tracking sets.

        Args:
            clean_text: Whether to apply text cleaning during extraction.
            output_dir: Base directory where static content will be stored.
        """
        self.visited_urls: set[str] = set()
        self.pending_urls: set[str] = set()
        self.failed_urls: list[dict[str, str]] = []
        self.url_queue: deque[dict[str, Any]] = deque()
        self.external_links_per_page: dict[str, int] = {}

        self.output_dir = (
            Path(output_dir)
            if output_dir is not None
            else Path(Config.BASE_DATA_DIR)
        )

        self.folder_builder = FolderStructureBuilder(
            data_dir=self.output_dir
        )

        self.clean_text = clean_text
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            )
        })

        self.urls_deduplicated: int = 0
    
    def initialize(self) -> bool:
        """
        Initialize the crawler environment, including logging and directory structures.
        
        Returns:
            bool: True if initialization was successful, False otherwise.
        """
        Logger.initialize(self.output_dir)
        Logger.log_info("Initializing Web Crawler...")
        
        # Create folder structure
        if self.folder_builder.create_folder_tree():
            Logger.log_success("Folder structure created successfully")
        else:
            Logger.log_error("Failed to create folder structure")
            return False
        
        return True
    
    def _normalize_url(self, url: str) -> str:
        """
        Normalize a URL to ensure consistent comparison and avoid duplicate crawling.
        
        Handles fragment removal, query parameter filtering (UTM, Session IDs),
        trailing slash removal, and lowercasing.
        
        Args:
            url: The raw URL string to normalize.
            
        Returns:
            str: The normalized URL.
        """
        if not url:
            return ""
        
        # Remove fragment (#section)
        url = url.split('#')[0]
        
        # Handle query parameters: remove utm_*, PHPSESSID
        if '?' in url:
            base, params = url.split('?', 1)
            # Remove trailing slash from base before params
            base = base.rstrip('/')
            param_parts = []
            for param in params.split('&'):
                # Skip tracking and session parameters
                if not param.startswith(('utm_', 'PHPSESSID=')):
                    param_parts.append(param)
            url = base + ('?' + '&'.join(param_parts) if param_parts else '')
        else:
            # Remove trailing slash if no query params
            url = url.rstrip('/')
        
        # Convert to lowercase for comparison
        return url.lower()
    
    def _should_skip_url(self, url: str) -> tuple[bool, str]:
        """
        Evaluate if a URL should be excluded from the crawl based on predefined rules.
        
        Args:
            url: The normalized URL to check.
            
        Returns:
            tuple[bool, str]: A tuple of (should_skip, reason_message).
        """
        if not url:
            return True, "empty URL"
        
        # Skip download-file URLs
        if 'download-file' in url:
            return True, "download-file URL"
        
        # Skip authentication pages
        auth_patterns = ['/login', '/logout', '/register']
        for pattern in auth_patterns:
            if pattern in url:
                return True, f"auth page ({pattern})"
        
        # Skip search pages
        if '/search' in url:
            return True, "search page"
        
        # Skip people/directory pages
        if '/people' in url:
            return True, "people directory"
        
        return False, ""
    

    def add_initial_urls(self, urls_dict: dict[str, str]) -> None:
        """
        Seed the crawler's queue with an initial set of entry-point URLs.
        
        Args:
            urls_dict: A dictionary mapping logical page names to their target URLs.
        """
        Logger.log_info(f"Adding {len(urls_dict)} initial URLs to queue...")
        
        added_count = 0
        skipped_count = 0
        
        for page_name, url in urls_dict.items():
            if not url or not url.startswith('http'):
                continue
            
            # Normalize the URL
            normalized_url = self._normalize_url(url)
            
            # Check if we should skip this URL
            should_skip, skip_reason = self._should_skip_url(normalized_url)
            if should_skip:
                skipped_count += 1
                Logger.log_warning(f"Skipping initial URL ({skip_reason}): {normalized_url}")
                continue
            
            # Check for duplicates in visited and pending
            if normalized_url in self.visited_urls or normalized_url in self.pending_urls:
                skipped_count += 1
                Logger.log_warning(f"Skipping duplicate initial URL: {normalized_url}")
                continue
            
            # Add to queue and pending set
            self.url_queue.append({
                'url': normalized_url,
                'page_name': page_name,
                'category': self._extract_category(page_name),
                'parent_url': None,
                'is_external': False
            })
            self.pending_urls.add(normalized_url)
            added_count += 1
        
        Logger.log_success(f"Added {added_count} initial URLs to queue (skipped {skipped_count} duplicates/filtered)")
    
    
    def crawl(self) -> None:
        """
        Commence the iterative crawling process until the URL queue is exhausted.
        
        Respects the configured rate limit delay between requests.
        """
        Logger.log_info("Starting crawl process...")
        
        while self.url_queue:
            item = self.url_queue.popleft()
            self._process_url(item)
            
            # Rate limiting: 1 second delay between requests
            time.sleep(Config.RATE_LIMIT_DELAY)
        
        Logger.log_success("Crawl process completed!")
    
    def _process_url(self, item: dict[str, Any]) -> None:
        """
        Process a single URL from the queue, including fetching, extraction, and saving.
        
        Args:
            item: A dictionary containing 'url', 'page_name', 'category', and metadata.
        """
        url: str = item['url']
        
        # Remove from pending and skip if already visited
        self.pending_urls.discard(url)
        
        if url in self.visited_urls:
            Logger.log_warning(f"URL already visited: {url}")
            return
        
        self.visited_urls.add(url)
        Logger.log_info(f"Processing: {url} (Queue: {len(self.url_queue)}, Pending: {len(self.pending_urls)})")
        
        try:
            # Fetch the page
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            # Extract content
            extractor = ContentExtractor(response.text, url, clean_text=self.clean_text)
            title = extractor.extract_title()
            paragraphs = extractor.extract_paragraphs()
            headings = extractor.extract_headings()
            internal_links, external_links = extractor.extract_links()
            images = extractor.extract_images()
            
            # Prepare content structure
            content = {
                "paragraphs": paragraphs,
                "titles": headings
            }
            
            links = {
                "internal": internal_links,
                "external": external_links
            }
            
            # Save files
            output_dir = self._get_output_directory(item['category'])
            file_gen = FileGenerator(output_dir)
            
            page_name = self._sanitize_page_name(item['page_name'])
            file_gen.save_both(
                url=url,
                title=title,
                category=item['category'],
                page_name=page_name,
                content=content,
                links=links,
                images=images
            )
            
            # Update statistics
            Logger.update_stats(
                pages_scraped=1,
                total_paragraphs=len(paragraphs),
                total_images=len(images),
                total_links=len(internal_links) + len(external_links)
            )
            
            # Queue internal links for crawling
            self._queue_internal_links(internal_links, item['category'])
            
            # Process external links (max 1 per page)
            self._process_external_links(external_links, item['category'], output_dir, page_name)
            
        except requests.RequestException as e:
            Logger.log_error(f"Failed to fetch {url}: {e}")
            self.failed_urls.append({"url": url, "error": str(e)})
            Logger.update_stats(pages_failed=1, failed_urls=[f"{url} - {str(e)}"])
        
        except Exception as e:
            Logger.log_error(f"Error processing {url}: {e}")
            self.failed_urls.append({"url": url, "error": str(e)})
            Logger.update_stats(pages_failed=1, failed_urls=[f"{url} - {str(e)}"])
    
    def _queue_internal_links(self, links: list[dict[str, Any]], category: str) -> None:
        """
        Filter and queue newly discovered internal links for future processing.
        
        Args:
            links: A list of discovered link dictionaries.
            category: The logical category to assign to these links.
        """
        queued_count = 0
        skipped_count = 0
        
        for link in links:
            url = link.get('url')
            
            if not url:
                continue
            
            # Normalize the URL first
            normalized_url = self._normalize_url(url)
            
            # Check if we should skip this URL
            should_skip, skip_reason = self._should_skip_url(normalized_url)
            if should_skip:
                skipped_count += 1
                continue
            
            # Early deduplication: check both visited and pending at queue entry
            if normalized_url in self.visited_urls:
                skipped_count += 1
                self.urls_deduplicated += 1
                continue
            
            if normalized_url in self.pending_urls:
                skipped_count += 1
                self.urls_deduplicated += 1
                continue
            
            # Extract page name from normalized URL
            path = urlparse(normalized_url).path
            page_name = path.split('/')[-1] or 'index'
            
            # Add to queue and pending set
            self.url_queue.append({
                'url': normalized_url,
                'page_name': page_name,
                'category': category,
                'parent_url': None,
                'is_external': False
            })
            self.pending_urls.add(normalized_url)
            queued_count += 1
        
        if queued_count > 0 or skipped_count > 0:
            Logger.log_info(f"Queued {queued_count} links, skipped {skipped_count} (duplicates/filtered) from {len(links)} total")
    
    
    def _process_external_links(self, 
                               links: list[dict[str, Any]], 
                               category: str, 
                               output_dir: Path,
                               parent_page_name: str) -> None:
        """
        Process the first available external link on a page without further recursive crawling.
        
        Args:
            links: A list of external link dictionaries.
            category: The parent page's category.
            output_dir: The target output directory.
            parent_page_name: The name of the originating page.
        """
        if not links:
            return
        
        # Take only the first external link
        link = links[0]
        url = link.get('url')
        
        # Skip download-file URLs
        if url and 'download-file' in url:
            return
        
        if not url or url in self.visited_urls:
            return
        
        Logger.log_info(f"Processing external link: {url}")
        
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            # Extract content from external page
            extractor = ContentExtractor(response.text, url, clean_text=self.clean_text)
            title = extractor.extract_title()
            paragraphs = extractor.extract_paragraphs()
            headings = extractor.extract_headings()
            _, external_links = extractor.extract_links()  # Don't crawl further
            images = extractor.extract_images()
            
            content = {
                "paragraphs": paragraphs,
                "titles": headings
            }
            
            links_data = {
                "internal": [],
                "external": external_links[:5]  # Limit external links
            }
            
            # Generate safe filename for external page
            ext_domain = urlparse(url).netloc.replace('.', '-').replace('www-', '')
            ext_page_name = f"external-{ext_domain}-{parent_page_name}"
            
            # Save external content
            file_gen = FileGenerator(output_dir)
            file_gen.save_both(
                url=url,
                title=f"[EXTERNAL] {title}",
                category=f"{category} (External)",
                page_name=ext_page_name,
                content=content,
                links=links_data,
                images=images
            )
            
            Logger.update_stats(
                external_pages_processed=1,
                total_paragraphs=len(paragraphs),
                total_images=len(images),
                total_links=len(external_links)
            )
            
            self.visited_urls.add(url)
            
        except requests.RequestException as e:
            Logger.log_warning(f"Failed to fetch external link {url}: {e}")
        except Exception as e:
            Logger.log_warning(f"Error processing external link {url}: {e}")
    
    def _extract_category(self, page_name: str) -> str:
        """
        Deduce the logical category for a page based on its name and the site structure.
        
        Args:
            page_name: The name of the page to categorize.
        
        Returns:
            str: A category slug (e.g., 'research/ac').
        """
        # Find which category and submenu this page belongs to
        for category, submenus in Config.SUBMENU_STRUCTURE.items():
            if isinstance(submenus, list):
                for submenu in submenus:
                    if submenu == page_name:
                        category_slug = self.folder_builder._get_safe_slug(category)
                        submenu_slug = self.folder_builder._get_safe_slug(submenu)
                        return f"{category_slug}/{submenu_slug}"
        
        # If not found in structure, use page name
        return self.folder_builder._get_safe_slug(page_name)
    
    def _get_output_directory(self, category: str) -> Path:
        """
        Determine the appropriate filesystem directory for a given category.
        
        Args:
            category: The logical category path.

        Returns:
            Path: The resolved output Path.
        """
        path = self.output_dir / category
        path.mkdir(parents=True, exist_ok=True)
        return path

   
    def _sanitize_page_name(self, page_name: str) -> str:
        """
        Clean a page name for safe use as a filename.
        
        Args:
            page_name: The raw page name.

        Returns:
            str: The sanitized name.
        """
        return self.folder_builder._get_safe_slug(page_name)
    
    def get_report(self) -> Path | None:
        """
        Conclude the crawl and generate the final execution report.
        
        Returns:
            Path | None: The path to the generated report file.
        """
        return Logger.generate_final_report()
