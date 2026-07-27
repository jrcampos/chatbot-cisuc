"""
Unit tests for ContentExtractor module
Tests HTML content extraction with proper link classification and media handling
"""

import pytest
from ingestion.cisuc_scraper.extractors.content_extractor import ContentExtractor


class TestContentExtractorTitle:
    """Test ContentExtractor.extract_title() method"""

    def test_extract_title_from_title_tag(self, sample_html_content, base_url):
        """Test extraction of title from <title> tag"""
        extractor = ContentExtractor(sample_html_content, base_url)
        title = extractor.extract_title()

        assert title == "Test Page Title"

    def test_extract_title_from_h1_when_no_title_tag(self):
        """Test extraction of title from h1 when title tag is missing"""
        html = "<html><body><h1>H1 Title Here</h1></body></html>"
        extractor = ContentExtractor(html, "https://example.com")
        title = extractor.extract_title()

        assert title == "H1 Title Here"

    def test_extract_title_from_og_title_meta(self):
        """Test extraction of title from og:title meta tag"""
        html = '<html><head><meta property="og:title" content="OG Title"></head></body></html>'
        extractor = ContentExtractor(html, "https://example.com")
        title = extractor.extract_title()

        assert title == "OG Title"

    def test_no_title_found(self):
        """Test handling when no title is found"""
        html = "<html><body>No title here</body></html>"
        extractor = ContentExtractor(html, "https://example.com")
        title = extractor.extract_title()

        assert title == "No Title Found"


class TestContentExtractorParagraphs:
    """Test ContentExtractor.extract_paragraphs() method"""

    def test_extract_paragraphs(self, sample_html_content, base_url):
        """Test extraction of paragraphs from HTML"""
        extractor = ContentExtractor(sample_html_content, base_url)
        paragraphs = extractor.extract_paragraphs()

        assert len(paragraphs) > 0
        assert "first paragraph" in paragraphs[0].lower()

    def test_skip_short_paragraphs(self):
        """Test that short paragraphs are skipped"""
        html = """
        <html><body>
        <p>Short</p>
        <p>This is a long paragraph with substantial content</p>
        </body></html>
        """
        extractor = ContentExtractor(html, "https://example.com")
        paragraphs = extractor.extract_paragraphs()

        # "Short" should be filtered out
        assert "Short" not in [p for p in paragraphs if len(p) < 10]

    def test_remove_duplicates(self, sample_html_content, base_url):
        """Test that duplicate paragraphs are removed"""
        extractor = ContentExtractor(sample_html_content, base_url)
        paragraphs = extractor.extract_paragraphs()

        # Check that duplicated content appears only once
        unique_paragraphs = set(paragraphs)
        assert len(unique_paragraphs) == len(paragraphs)

    def test_exclude_navigation_content(self):
        """Test that navigation, header, footer content is excluded"""
        html = """
        <html><body>
        <nav>Navigation Menu</nav>
        <p>Main content paragraph</p>
        <footer>Footer text here</footer>
        </body></html>
        """
        extractor = ContentExtractor(html, "https://example.com")
        paragraphs = extractor.extract_paragraphs()

        # Should include main content but exclude nav/footer
        assert any("Main content" in p for p in paragraphs)
        assert not any("Navigation" in p for p in paragraphs)


class TestContentExtractorHeadings:
    """Test ContentExtractor.extract_headings() method"""

    def test_extract_headings(self, sample_html_content, base_url):
        """Test extraction of headings from HTML"""
        extractor = ContentExtractor(sample_html_content, base_url)
        headings = extractor.extract_headings()

        assert len(headings) > 0
        assert "Section 1" in headings

    def test_extract_multiple_heading_levels(self):
        """Test extraction of different heading levels (h2-h6)"""
        html = """
        <html><body>
        <h2>Heading 2</h2>
        <h3>Heading 3</h3>
        <h4>Heading 4</h4>
        </body></html>
        """
        extractor = ContentExtractor(html, "https://example.com")
        headings = extractor.extract_headings()

        assert len(headings) == 3
        assert "Heading 2" in headings
        assert "Heading 3" in headings


class TestContentExtractorLinks:
    """Test ContentExtractor.extract_links() method"""

    def test_extract_internal_and_external_links(self, sample_html_content, base_url):
        """Test classification of links as internal or external"""
        extractor = ContentExtractor(sample_html_content, base_url)
        internal, external = extractor.extract_links()

        assert len(internal) > 0
        assert len(external) > 0

    def test_resolve_relative_urls(self):
        """Test resolution of relative URLs to absolute"""
        html = '<html><body><a href="/page">Link</a></body></html>'
        base = "https://www.cisuc.uc.pt/en/about"
        extractor = ContentExtractor(html, base)
        internal, external = extractor.extract_links()

        assert len(internal) > 0
        assert "https://www.cisuc.uc.pt/page" in internal[0]["url"]

    def test_skip_anchor_links(self):
        """Test that anchor-only links are skipped"""
        html = '<html><body><a href="#section">Anchor</a></body></html>'
        extractor = ContentExtractor(html, "https://example.com")
        internal, external = extractor.extract_links()

        # Should not include anchor-only links
        assert len(internal) == 0 and len(external) == 0

    def test_skip_javascript_links(self):
        """Test that javascript: links are skipped"""
        html = '<html><body><a href="javascript:void(0)">JS Link</a></body></html>'
        extractor = ContentExtractor(html, "https://example.com")
        internal, external = extractor.extract_links()

        assert len(internal) == 0 and len(external) == 0

    def test_skip_download_file_urls(self):
        """Test that download-file URLs are skipped"""
        html = '<html><body><a href="https://cisuc.uc.pt/download-file/123">Download</a></body></html>'
        base = "https://cisuc.uc.pt"
        extractor = ContentExtractor(html, base)
        internal, external = extractor.extract_links()

        # Download links should be skipped
        assert len(internal) == 0 and len(external) == 0


class TestContentExtractorImages:
    """Test ContentExtractor.extract_images() method"""

    def test_extract_images(self, sample_html_content, base_url):
        """Test extraction of images from HTML"""
        extractor = ContentExtractor(sample_html_content, base_url)
        images = extractor.extract_images()

        assert len(images) > 0
        assert "alt" in images[0]
        assert "src" in images[0]

    def test_resolve_relative_image_urls(self):
        """Test resolution of relative image URLs"""
        html = '<html><body><img src="/images/test.jpg" alt="Test"></body></html>'
        base = "https://www.cisuc.uc.pt/en/about"
        extractor = ContentExtractor(html, base)
        images = extractor.extract_images()

        assert len(images) == 1
        assert "https://www.cisuc.uc.pt/images/test.jpg" in images[0]["src"]

    def test_extract_image_alt_text(self):
        """Test extraction of image alt text"""
        html = '<html><body><img src="test.jpg" alt="Alt Text"></body></html>'
        extractor = ContentExtractor(html, "https://example.com")
        images = extractor.extract_images()

        assert images[0]["alt"] == "Alt Text"


class TestContentExtractorUrlValidation:
    """Test URL validation and classification methods"""

    def test_internal_link_classification(self):
        """Test correct classification of internal links"""
        html = (
            '<html><body><a href="https://www.cisuc.uc.pt/page">Link</a></body></html>'
        )
        extractor = ContentExtractor(html, "https://www.cisuc.uc.pt")

        # This tests the _is_internal_link method indirectly
        internal, external = extractor.extract_links()
        assert len(internal) > 0

    def test_external_link_classification(self):
        """Test correct classification of external links"""
        html = '<html><body><a href="https://external.com/page">Link</a></body></html>'
        extractor = ContentExtractor(html, "https://www.cisuc.uc.pt")
        internal, external = extractor.extract_links()

        assert len(external) > 0


class TestContentExtractorWithCleaningDisabled:
    """Test ContentExtractor behavior when text cleaning is disabled"""

    def test_extract_without_cleaning(self):
        """Test that text cleaning is not applied when disabled"""
        html = "<html><body><p>Text with {{ template }} syntax</p></body></html>"
        extractor = ContentExtractor(html, "https://example.com", clean_text=False)
        paragraphs = extractor.extract_paragraphs()

        # Template syntax should still be present
        assert any("{{" in p for p in paragraphs)
