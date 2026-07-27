import json
import re
from pathlib import Path
from typing import Any
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from .utils import Config, Logger, FolderStructureBuilder
from .text_cleaner import TextCleaner


class ContentExtractor:
    """
    Handles the extraction and sanitization of structured data from raw HTML content.
    
    Implements multiple strategies for identifying titles, main body text, 
    headings, links, and images, ensuring high-quality data for RAG systems.
    """
    
    def __init__(self, html_content: str, base_url: str, clean_text: bool = True) -> None:
        """
        Initialize the extractor with HTML content and source URL context.
        
        Args:
            html_content: The raw HTML string to be parsed.
            base_url: The source URL, used for resolving relative links.
            clean_text: Whether to apply the TextCleaner to extracted segments.
        """
        self.soup = BeautifulSoup(html_content, 'html.parser')
        self.base_url = base_url
        self.clean_text = clean_text
        self.text_cleaner = TextCleaner()
    
    def extract_title(self) -> str:
        """
        Identify and extract the main title of the page using prioritized strategies.
        
        Returns:
            str: The sanitized main title, or a default string if not found.
        """
        # Try multiple ways to get the title
        title: str | None = None
        
        # Try page title tag
        title_tag = self.soup.find('title')
        if title_tag:
            title = title_tag.get_text(strip=True)
        
        # Try h1 tag
        if not title:
            h1_tag = self.soup.find('h1')
            if h1_tag:
                title = h1_tag.get_text(strip=True)
        
        # Try og:title meta tag
        if not title:
            og_title = self.soup.find('meta', property='og:title')
            if og_title and isinstance(og_title.get('content'), str):
                title = og_title.get('content')
        
        result = title or "No Title Found"
        
        # Clean title if needed
        if self.clean_text:
            result = self.text_cleaner.clean_paragraph(result)
        
        return result
    
    def extract_paragraphs(self) -> list[str]:
        """
        Extract all relevant body text segments from the page.
        
        Utilizes multiple identification strategies, including content area detection,
        excluded pattern filtering, and div-based fallbacks.
        
        Returns:
            list[str]: A list of sanitized and unique paragraph strings.
        """
        paragraphs: list[str] = []
        seen_texts: set[str] = set()  # Track seen text to avoid duplicates
        
        # Strategy 1: Try to find main content area first
        content_area = None
        
        # Look for specific content containers
        for selector in ['[role="main"]', '.content', '.main-content', '.page-content', 
                         '.article-content', 'main', 'article']:
            if selector.startswith('.') or selector.startswith('['):
                if selector.startswith('['):
                    content_area = self.soup.find(attrs={'role': 'main'})
                else:
                    content_area = self.soup.find('div', class_=selector.replace('.', ''))
            else:
                content_area = self.soup.find(selector)
            
            if content_area:
                break
        
        # Strategy 2: If no container found, use the body
        if not content_area:
            content_area = self.soup.body or self.soup
        
        # Strategy 3: Extract ALL paragraphs from the page (not just container)
        # This is more reliable for this particular website structure
        all_p_tags = self.soup.find_all('p')
        
        # Filter out paragraphs from common non-content areas
        excluded_patterns = [
            'nav', 'header', 'footer', 'sidebar', 'menu', 'search', 'comments',
            'advertisement', 'ad', 'social', 'share', 'cookie'
        ]
        
        for p in all_p_tags:
            text = p.get_text(strip=True)
            
            # Skip if text is too short
            if not text or len(text) < 20:
                continue
            
            # Skip if already seen
            if text in seen_texts:
                continue
            
            # Skip if in excluded areas
            parent = p.parent
            skip = False
            for _ in range(5):  # Check up to 5 parents
                if parent:
                    parent_id = parent.get('id', '').lower() if isinstance(parent.get('id'), str) else ''
                    parent_classes = parent.get('class', [])
                    parent_class = ' '.join(parent_classes).lower() if isinstance(parent_classes, list) else str(parent_classes).lower()
                    
                    for pattern in excluded_patterns:
                        if pattern in parent_id or pattern in parent_class:
                            skip = True
                            break
                    if skip:
                        break
                    parent = parent.parent
                else:
                    break
            
            if skip:
                continue
            
            # Clean text if needed
            if self.clean_text:
                text = self.text_cleaner.clean_paragraph(text)
            
            # Skip if empty after cleaning
            if not text or len(text) < 20:
                continue
            
            # Add paragraph
            paragraphs.append(text)
            seen_texts.add(text)
        
        # Strategy 4: If still no paragraphs, try extracting from div elements
        if not paragraphs:
            for div in self.soup.find_all('div'):
                text = div.get_text(strip=True)
                
                # Only include divs with substantial content
                if not text or len(text) < 100:
                    continue
                
                # Skip if already seen
                if text in seen_texts:
                    continue
                
                # Skip nested content (divs within divs we already extracted)
                skip = False
                for existing_para in paragraphs:
                    if existing_para in text or text in existing_para:
                        skip = True
                        break
                
                if not skip:
                    # Clean text if needed
                    if self.clean_text:
                        text = self.text_cleaner.remove_template_lines(text)
                    
                    # Split by sentence-like patterns if very long
                    if len(text) > 500:
                        sentences = re.split(r'(?<=[.!?])\s+', text)
                        for sentence in sentences:
                            if len(sentence.strip()) > 50 and sentence.strip() not in seen_texts:
                                paragraphs.append(sentence.strip())
                                seen_texts.add(sentence.strip())
                    else:
                        paragraphs.append(text)
                        seen_texts.add(text)
        
        return paragraphs
    
    def extract_headings(self) -> list[str]:
        """
        Extract all hierarchical headings (h2-h6) from the page.
        
        Returns:
            list[str]: A list of sanitized unique heading strings.
        """
        headings: list[str] = []
        
        for h in self.soup.find_all(['h2', 'h3', 'h4', 'h5', 'h6']):
            text = h.get_text(strip=True)
            if text:
                # Clean heading if needed
                if self.clean_text:
                    text = self.text_cleaner.clean_paragraph(text)
                
                if text:
                    headings.append(text)
        
        return headings
    
    def extract_links(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """
        Discover and categorize all functional links on the page.
        
        Returns:
            tuple[list[dict], list[dict]]: Internal and external link lists.
        """
        internal_links: list[dict[str, str]] = []
        external_links: list[dict[str, str]] = []
        
        for link in self.soup.find_all('a', href=True):
            href = link['href']
            text = link.get_text(strip=True)
            
            if not text or not href:
                continue
            
            # Skip anchor-only links and javascript
            if href.startswith('#') or href.startswith('javascript:'):
                continue
            
            # Resolve relative URLs
            absolute_url = urljoin(self.base_url, href)
            
            # Skip download-file URLs
            if self._should_skip_url(absolute_url):
                continue
            
            # Determine if internal or external
            if self._is_internal_link(absolute_url):
                internal_links.append({
                    "text": text,
                    "url": absolute_url
                })
            else:
                external_links.append({
                    "text": text,
                    "url": absolute_url
                })
        
        return internal_links, external_links
    
    def extract_images(self) -> list[dict[str, str]]:
        """
        Extract all image metadata (alt text and source URLs) from the page.
        
        Returns:
            list[dict[str, str]]: A list of image detail dictionaries.
        """
        images: list[dict[str, str]] = []
        
        for img in self.soup.find_all('img'):
            src = img.get('src', '')
            alt = img.get('alt', '')
            
            if src:
                # Resolve relative URLs
                absolute_url = urljoin(self.base_url, src)
                images.append({
                    "alt": alt,
                    "src": absolute_url
                })
        
        return images
    
    def _should_skip_url(self, url: str) -> bool:
        """
        Apply exclusion rules to a URL string.
        
        Args:
            url: The URL to evaluate.
        
        Returns:
            bool: True if the URL should be ignored.
        """
        # Skip download-file URLs
        if 'download-file' in url:
            return True
        
        return False
    
    def _is_internal_link(self, url: str) -> bool:
        """
        Check if a URL belongs to the target domain or is a relative path.
        
        Args:
            url: The absolute or relative URL.

        Returns:
            bool: True if identified as internal.
        """
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        
        # Check if it's the main domain
        return Config.DOMAIN in domain or domain == ''


class FileGenerator:
    """
    Responsible for serializing extracted page content into JSON and Markdown files.
    """
    
    def __init__(self, output_dir: Path) -> None:
        """
        Initialize the generator with a target output path.
        
        Args:
            output_dir: The directory where generated files will be placed.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def save_as_json(self, 
                     url: str,
                     title: str,
                     category: str,
                     page_name: str,
                     content: dict[str, Any],
                     links: dict[str, Any],
                     images: list[dict[str, Any]]) -> Path | None:
        """
        Save the extracted page model into a structured JSON file.
        
        Args:
            url: The source URL of the page.
            title: The page title.
            category: The logical category path.
            page_name: The base filename to use.
            content: Dictionary containing text segments.
            links: Dictionary containing discovered links.
            images: List of image metadata.
        
        Returns:
            Path | None: The path to the saved JSON file, or None if failed.
        """
        data: dict[str, Any] = {
            "url": url,
            "title": title,
            "category": category,
            "page_name": page_name,
            "timestamp": datetime.now().isoformat(),
            "content": content,
            # links and images omitted to reduce size unless explicitly needed
        }
        
        filename = self.output_dir / f"{page_name}.json"
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            Logger.log_success(f"Saved JSON: {filename}")
            return filename
        except Exception as e:
            Logger.log_error(f"Failed to save JSON {filename}: {e}")
            return None
    
    def save_as_markdown(self,
                        url: str,
                        title: str,
                        category: str,
                        page_name: str,
                        content: dict[str, Any],
                        links: dict[str, Any],
                        images: list[dict[str, Any]]) -> Path | None:
        """
        Generate a human-readable Markdown archive of the page content.
        
        Args:
            url: The source URL.
            title: The page title.
            category: The category path.
            page_name: The target filename.
            content: Extracted content segments.
            links: Discovered links.
            images: Image metadata.
        
        Returns:
            Path | None: The path to the saved Markdown file, or None if failed.
        """
        markdown = f"# {title}\n\n"
        markdown += f"**Source:** [{url}]({url})\n"
        markdown += f"**Category:** {category}\n"
        markdown += f"**Scraped:** {datetime.now().isoformat()}\n\n"
        markdown += "---\n\n"
        
        if content.get('paragraphs'):
            markdown += "## Content\n\n"
            for para in content['paragraphs']:
                for line in para.split('\n'):
                    # Filter out navigation noise that sometimes slips through
                    if any(keyword in line for keyword in ["()hide listShow All+", "CentreHistoryGoverning", "CentreResearchPeoplePublicationsProjectsAdvanced"]):
                        continue
                    if line.strip():
                        markdown += f"{line.strip()}\n\n"
        
        filename = self.output_dir / f"{page_name}.md"
        
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(markdown)
            Logger.log_success(f"Saved Markdown: {filename}")
            return filename
        except Exception as e:
            Logger.log_error(f"Failed to save Markdown {filename}: {e}")
            return None
    
    def save_both(self,
                  url: str,
                  title: str,
                  category: str,
                  page_name: str,
                  content: dict[str, Any],
                  links: dict[str, Any],
                  images: list[dict[str, Any]]) -> tuple[Path | None, Path | None]:
        """
        Convenience method to execute both JSON and Markdown serialization.
        
        Returns:
            tuple[Path | None, Path | None]: Paths to the JSON and Markdown files.
        """
        json_path = self.save_as_json(url, title, category, page_name, content, links, images)
        md_path = self.save_as_markdown(url, title, category, page_name, content, links, images)
        return json_path, md_path
