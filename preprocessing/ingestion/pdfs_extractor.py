"""
PDF Extractor

This module provides utilities for extracting structured Markdown content from 
PDF documents using the docling library.
"""

from docling.document_converter import DocumentConverter


def extract_pdf_to_markdown(source_url: str) -> str:
    """
    Convert a PDF document from a URL or local path into Markdown format.
    
    Args:
        source_url: The URL or filesystem path to the PDF document.
        
    Returns:
        str: The extracted content in Markdown format.
    """
    converter = DocumentConverter()
    result = converter.convert(source_url)
    return result.document.export_to_markdown()


if __name__ == "__main__":
    # Example usage with a sample research paper
    pdf_source = "https://arxiv.org/pdf/2408.09869"
    markdown_output = extract_pdf_to_markdown(pdf_source)
    print(markdown_output)
