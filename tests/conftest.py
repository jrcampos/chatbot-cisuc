"""
Pytest configuration and shared fixtures for tests
"""

import pytest
from pathlib import Path
import tempfile
import json


@pytest.fixture
def sample_html_content():
    """Provide sample HTML content for testing ContentExtractor"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Test Page Title</title>
        <meta property="og:title" content="OG Page Title">
    </head>
    <body>
        <nav>Navigation Menu</nav>
        <h1>Main Heading</h1>
        <h2>Section 1</h2>
        <p>This is the first paragraph with substantial content that should be extracted.</p>
        <h2>Section 2</h2>
        <p>This is the second paragraph with more content here.</p>
        <p>Short</p>
        <p>This is a third paragraph that is duplicated</p>
        <p>This is a third paragraph that is duplicated</p>
        <footer>Footer content</footer>
        <a href="/internal-page">Internal Link</a>
        <a href="https://external.com/page">External Link</a>
        <img src="/images/test.jpg" alt="Test Image">
        <img src="https://external.com/image.jpg" alt="External Image">
    </body>
    </html>
    """


@pytest.fixture
def sample_json_api_data():
    """Provide sample JSON data for testing JSONtoMarkdownConverter"""
    return {
        "users": [
            {"id": 1, "name": "John Doe", "email": "john@example.com", "active": True},
            {
                "id": 2,
                "name": "Jane Smith",
                "email": "jane@example.com",
                "active": False,
            },
        ]
    }


@pytest.fixture
def sample_json_list_data():
    """Provide sample JSON list data for testing"""
    return [
        {
            "id": 1,
            "title": "Item 1",
            "description": "First item description",
            "tags": ["tag1", "tag2"],
        },
        {
            "id": 2,
            "title": "Item 2",
            "description": "Second item description",
            "tags": ["tag3"],
        },
    ]


@pytest.fixture
def mock_config():
    """Provide a mock configuration dictionary"""
    return {
        "sources": {
            "static": {
                "enabled": True,
                "base_url": "https://www.cisuc.uc.pt",
                "output_dir": "data/static",
            },
            "api": {
                "enabled": True,
                "output_dir": "data/api",
                "endpoints": ["api-users", "api-projects"],
            },
        },
        "output": {"format": "json", "pretty_print": True},
        "api": {"timeout": 30},
    }


@pytest.fixture
def temp_dir():
    """Provide a temporary directory for file operations"""
    with tempfile.TemporaryDirectory() as temp_path:
        yield Path(temp_path)


@pytest.fixture
def sample_yaml_config(temp_dir):
    """Create a temporary YAML config file for testing"""
    yaml_content = """
sources:
  static:
    enabled: true
    base_url: "https://www.cisuc.uc.pt"
    output_dir: "data/static"

output:
  format: "json"
  pretty_print: true

crawler:
  max_workers: 5
  timeout: 30
"""
    config_file = temp_dir / "test_config.yaml"
    config_file.write_text(yaml_content)
    return config_file


@pytest.fixture
def sample_html_with_templates():
    """Provide HTML with template syntax for testing TextCleaner"""
    return """
    <html>
    <body>
    <p>This is {{variable}} and {% tag %} content [[ django ]]</p>
    <p>Clean content with extra    spaces    and
    newlines</p>
    <!-- This is a comment -->
    <p>After comment</p>
    </body>
    </html>
    """


@pytest.fixture
def base_url():
    """Provide a base URL for relative URL resolution"""
    return "https://www.cisuc.uc.pt/en/about"
