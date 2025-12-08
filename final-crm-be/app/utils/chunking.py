"""
Text chunking utilities
Functions for splitting text into chunks for embedding
"""
from typing import List, Optional
from langchain.text_splitter import RecursiveCharacterTextSplitter
from app.config import settings


def split_into_chunks(
    text: str,
    size: int,
    overlap: int,
    seps: Optional[List[str]] = None,
    model: str = settings.OPENAI_MODEL
) -> List[str]:
    """
    Split text into chunks using tiktoken-based splitting

    Args:
        text: Text to split
        size: Chunk size in tokens
        overlap: Overlap between chunks
        seps: Optional list of separators
        model: Model name for token counting

    Returns:
        List of text chunks
    """
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        model_name=model,
        chunk_size=size,
        chunk_overlap=overlap,
        separators=seps,
    )
    docs = splitter.create_documents([text])
    return [d.page_content for d in docs]
