"""
Text Cleaner

This module provides the TextCleaner class, which contains static methods for 
sanitizing extracted web content. It specializes in removing dynamic template 
syntax (e.g., Handlebars, Jinja2), HTML comments, and normalizing whitespace.
"""

import re


class TextCleaner:
    """
    Utility class for cleaning and deduplicating extracted text.
    """
    
    @staticmethod
    def clean_paragraph(text: str) -> str:
        """
        Remove template markers and normalize whitespace within a single string.
        
        Args:
            text: The raw string to clean.
            
        Returns:
            str: The sanitized string.
        """
        if not text:
            return ""
        
        # Remove Handlebars/Angular template syntax {{ ... }}
        text = re.sub(r'\{\{[^}]*\}\}', '', text)
        
        # Remove Jinja2 template syntax {% ... %}
        text = re.sub(r'\{%[^%]*%\}', '', text)
        
        # Remove Django template syntax [[ ... ]]
        text = re.sub(r'\[\[[^\]]*\]\]', '', text)
        
        # Remove HTML comments
        text = re.sub(r'<!--[^-]*-->', '', text)
        
        # Remove excessive whitespace (multiple spaces, tabs, newlines)
        text = re.sub(r'\s+', ' ', text)
        
        # Strip leading/trailing whitespace
        text = text.strip()
        
        return text
    
    @staticmethod
    def clean_paragraphs(paragraphs: list[str]) -> list[str]:
        """
        Clean and deduplicate a list of strings, filtering out very short segments.
        
        Args:
            paragraphs: A list of raw strings.
            
        Returns:
            list[str]: A list of cleaned, non-empty, and unique strings.
        """
        cleaned: list[str] = []
        seen: set[str] = set()  # Track duplicates
        
        for para in paragraphs:
            cleaned_para = TextCleaner.clean_paragraph(para)
            
            # Skip if empty or too short after cleaning
            if len(cleaned_para) < 20:
                continue
            
            # Skip duplicates
            if cleaned_para in seen:
                continue
            
            cleaned.append(cleaned_para)
            seen.add(cleaned_para)
        
        return cleaned
    
    @staticmethod
    def clean_headings(headings: list[str]) -> list[str]:
        """
        Sanitize a list of heading strings, ensuring uniqueness.
        
        Args:
            headings: A list of raw heading strings.
            
        Returns:
            list[str]: A list of cleaned and unique heading strings.
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        
        for heading in headings:
            cleaned_heading = TextCleaner.clean_paragraph(heading)
            
            # Skip if empty
            if not cleaned_heading:
                continue
            
            # Skip duplicates
            if cleaned_heading in seen:
                continue
            
            cleaned.append(cleaned_heading)
            seen.add(cleaned_heading)
        
        return cleaned
    
    @staticmethod
    def is_template_content(text: str) -> bool:
        """
        Heuristically determine if a string is primarily composed of dynamic template markers.
        
        Args:
            text: The string to evaluate.
            
        Returns:
            bool: True if template markers represent more than 50% of the character count.
        """
        # Count template markers
        template_markers = len(re.findall(r'\{\{|\{%|\[\[', text))
        
        # If more than 50% of the text is template markers, consider it template content
        if len(text) > 0:
            template_ratio = template_markers / len(text)
            return template_ratio > 0.5
        
        return False
    
    @staticmethod
    def remove_template_lines(text: str) -> str:
        """
        Process a multi-line string to exclude lines that are identified as template-only.
        
        Args:
            text: A string potentially containing multiple lines.
            
        Returns:
            str: The combined string with template lines removed.
        """
        lines = text.split('\n')
        cleaned_lines: list[str] = []
        
        for line in lines:
            if not TextCleaner.is_template_content(line):
                cleaned_line = TextCleaner.clean_paragraph(line)
                if cleaned_line:  # Only add non-empty lines
                    cleaned_lines.append(cleaned_line)
        
        return '\n'.join(cleaned_lines)
