"""
Text processing utilities
Functions for cleaning and processing text content
"""
import re
from typing import List
from unstructured.documents.elements import Element


def clean_extra_whitespace(s: str) -> str:
    """
    Clean extra whitespace from text

    Args:
        s: Input text

    Returns:
        Cleaned text with normalized whitespace
    """
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def group_broken_paragraphs(s: str) -> str:
    """
    Group broken paragraphs by joining lines that don't end with sentence terminators

    Args:
        s: Input text

    Returns:
        Text with broken paragraphs joined
    """
    # If line ends with letter and next line starts with lowercase, join with space
    s = re.sub(r"(?<![.\?\!:])\n(?!\n)", " ", s)
    return s


def to_clean_text_from_strs(texts: List[str]) -> str:
    """
    Convert list of text strings to clean, joined text

    Args:
        texts: List of text strings

    Returns:
        Cleaned and joined text
    """
    cleaned = []
    for s in texts:
        if s:
            t = group_broken_paragraphs(s)
            t = clean_extra_whitespace(t)
            if t.strip():
                cleaned.append(t.strip())
    return "\n\n".join(cleaned)


def elements_to_clean_text(elements: List[Element]) -> str:
    """
    Convert unstructured document elements to clean text

    Args:
        elements: List of document elements

    Returns:
        Cleaned text from elements
    """
    texts: List[str] = []
    for el in elements:
        if getattr(el, "text", None):
            t = group_broken_paragraphs(el.text)
            t = clean_extra_whitespace(t)
            if t.strip():
                texts.append(t.strip())
    return "\n\n".join(texts)
