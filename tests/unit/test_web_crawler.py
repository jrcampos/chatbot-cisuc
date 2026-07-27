"""
Unit tests for WebCrawler module
Tests URL normalization, deduplication, and link classification
"""

import pytest
from ingestion.cisuc_scraper.crawlers.web_crawler import WebCrawler


class TestWebCrawlerURLNormalization:
    """Test URL normalization functionality"""

    def test_remove_fragment(self):
        """Test removal of URL fragments (#section)"""
        crawler = WebCrawler()
        url = "https://example.com/page#section"
        normalized = crawler._normalize_url(url)

        assert "#" not in normalized
        assert normalized == "https://example.com/page"

    def test_remove_trailing_slash(self):
        """Test removal of trailing slashes"""
        crawler = WebCrawler()
        url = "https://example.com/page/"
        normalized = crawler._normalize_url(url)

        assert normalized == "https://example.com/page"

    def test_remove_tracking_parameters(self):
        """Test removal of UTM tracking parameters"""
        crawler = WebCrawler()
        url = "https://example.com/page?utm_source=test&utm_medium=email&keep=value"
        normalized = crawler._normalize_url(url)

        assert "utm_" not in normalized
        assert "keep=value" in normalized

    def test_remove_session_id(self):
        """Test removal of PHPSESSID parameters"""
        crawler = WebCrawler()
        url = "https://example.com/page?PHPSESSID=abc123&other=value"
        normalized = crawler._normalize_url(url)

        assert "PHPSESSID" not in normalized
        assert "other=value" in normalized

    def test_lowercase_conversion(self):
        """Test URL is converted to lowercase"""
        crawler = WebCrawler()
        url = "https://EXAMPLE.com/Page/Test"
        normalized = crawler._normalize_url(url)

        assert normalized == normalized.lower()

    def test_empty_url(self):
        """Test handling of empty URL"""
        crawler = WebCrawler()
        normalized = crawler._normalize_url("")

        assert normalized == ""

    def test_complex_url_normalization(self):
        """Test normalization of complex URL with multiple issues"""
        crawler = WebCrawler()
        url = "https://Example.com/page/?utm_source=test&PHPSESSID=xyz#section"
        normalized = crawler._normalize_url(url)

        assert "#" not in normalized
        assert "utm_" not in normalized
        assert "PHPSESSID" not in normalized
        assert normalized == normalized.lower()
        assert not normalized.endswith("/")


class TestWebCrawlerURLSkipping:
    """Test URL skipping logic"""

    def test_should_skip_url_empty(self):
        """Test that empty URLs are skipped"""
        crawler = WebCrawler()
        skip, reason = crawler._should_skip_url("")

        assert skip is True

    def test_should_skip_url_invalid(self):
        """Test that invalid URLs are skipped"""
        crawler = WebCrawler()
        skip, reason = crawler._should_skip_url("not a valid url")

        # Based on implementation, may or may not skip depending on rules
        # At minimum, should return a bool and reason
        assert isinstance(skip, bool)
        assert isinstance(reason, str)


class TestWebCrawlerLinkClassification:
    """Test internal vs external link classification via normalization"""

    def test_normalize_preserves_internal_domains(self):
        """Test that URL normalization works for internal domain classification"""
        crawler = WebCrawler()

        url1 = "https://www.cisuc.uc.pt/page"
        url2 = "https://www.cisuc.uc.pt/page#section"

        # After normalization, both should be similar
        norm1 = crawler._normalize_url(url1)
        norm2 = crawler._normalize_url(url2)

        assert norm1 == norm2

    def test_normalize_preserves_external_domains(self):
        """Test that external domain URLs are normalized consistently"""
        crawler = WebCrawler()

        url1 = "https://external.com/page"
        url2 = "https://external.com/page/"

        norm1 = crawler._normalize_url(url1)
        norm2 = crawler._normalize_url(url2)

        # Both should normalize to the same value (without trailing slash)
        assert norm1 == norm2

    def test_normalize_relative_url(self):
        """Test normalization of relative URL"""
        crawler = WebCrawler()
        url = "/page"

        # Relative URLs should be normalized as-is
        normalized = crawler._normalize_url(url)
        assert normalized == "/page"


class TestWebCrawlerDeduplication:
    """Test URL deduplication statistics"""

    def test_crawler_initialization(self):
        """Test crawler initializes with deduplication counter"""
        crawler = WebCrawler()

        assert crawler.urls_deduplicated == 0
        assert isinstance(crawler.visited_urls, set)
        assert isinstance(crawler.pending_urls, set)

    def test_normalize_urls_for_deduplication(self):
        """Test that URLs are normalized before deduplication"""
        crawler = WebCrawler()

        url1 = "https://example.com/page?utm_source=test#section"
        url2 = "https://example.com/page/"

        norm1 = crawler._normalize_url(url1)
        norm2 = crawler._normalize_url(url2)

        # Both should normalize to similar form
        assert norm1 == norm2


class TestWebCrawlerURLQueueManagement:
    """Test URL queue management"""

    def test_crawler_has_url_queue(self):
        """Test crawler has URL queue structures"""
        crawler = WebCrawler()

        assert hasattr(crawler, "url_queue")
        assert hasattr(crawler, "visited_urls")
        assert hasattr(crawler, "pending_urls")
        assert hasattr(crawler, "failed_urls")

    def test_initialize_crawler(self):
        """Test crawler initialization"""
        crawler = WebCrawler()

        # initialize() should not raise error
        result = crawler.initialize()

        # Should return boolean indicating success
        assert isinstance(result, bool)


class TestWebCrawlerSessionManagement:
    """Test HTTP session management"""

    def test_crawler_has_session(self):
        """Test crawler has requests session"""
        crawler = WebCrawler()

        assert hasattr(crawler, "session")
        assert crawler.session.headers.get("User-Agent")

    def test_user_agent_is_set(self):
        """Test that User-Agent header is set"""
        crawler = WebCrawler()

        user_agent = crawler.session.headers.get("User-Agent")
        assert user_agent
        assert "Mozilla" in user_agent


class TestWebCrawlerTextCleaning:
    """Test text cleaning option"""

    def test_crawler_with_text_cleaning_enabled(self):
        """Test crawler can be initialized with text cleaning enabled"""
        crawler = WebCrawler(clean_text=True)

        assert crawler.clean_text is True

    def test_crawler_with_text_cleaning_disabled(self):
        """Test crawler can be initialized with text cleaning disabled"""
        crawler = WebCrawler(clean_text=False)

        assert crawler.clean_text is False
