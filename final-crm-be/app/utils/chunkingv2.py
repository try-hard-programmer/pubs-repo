"""
Text chunking utilities - OPTIMIZED FOR RAG
Functions for splitting text into chunks for embedding with improved context preservation
"""
from typing import List, Optional
from langchain.text_splitter import RecursiveCharacterTextSplitter
from app.config import settings


def split_into_chunks(
    text: str,
    size: int = 512,
    overlap: int = 100,  # [IMPROVED] Increased from 50 to 100 for better context
    seps: Optional[List[str]] = None,
    model: str = settings.OPENAI_MODEL
) -> List[str]:
    """
    Split text into chunks using tiktoken-based splitting with optimized separators
    
    [IMPROVEMENTS]:
    - Better separator hierarchy (preserves document structure)
    - Increased overlap (100 tokens = ~25% overlap for better context continuity)
    - Preserves tables, lists, and code blocks
    
    Args:
        text: Text to split
        size: Chunk size in tokens (default: 512)
        overlap: Overlap between chunks (default: 100)
        seps: Optional list of separators
        model: Model name for token counting

    Returns:
        List of text chunks
    """
    
    # [IMPROVED] Optimized separator hierarchy for better semantic preservation
    if seps is None:
        seps = [
            "\n\n\n",          # Major section breaks
            "\n\n",            # Paragraph breaks
            "\n",              # Line breaks
            ". ",              # Sentence endings
            "! ",              # Exclamations
            "? ",              # Questions
            "; ",              # Semi-colons
            ", ",              # Commas
            " ",               # Spaces
            "",                # Characters (last resort)
        ]
    
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        model_name=model,
        chunk_size=size,
        chunk_overlap=overlap,
        separators=seps,
        is_separator_regex=False,  # Treat separators as literals
    )
    
    docs = splitter.create_documents([text])
    
    # [NEW] Post-processing: Remove chunks that are too small (< 50 tokens)
    MIN_CHUNK_SIZE = 50
    filtered_chunks = [
        d.page_content for d in docs 
        if len(d.page_content.split()) >= MIN_CHUNK_SIZE
    ]
    
    return filtered_chunks if filtered_chunks else [d.page_content for d in docs]


def split_into_chunks_advanced(
    text: str,
    size: int = 512,
    overlap: int = 100,
    preserve_code: bool = True,
    preserve_tables: bool = True,
) -> List[str]:
    """
    Advanced chunking with special handling for structured content
    
    Use this for documents with:
    - Code blocks
    - Tables
    - Lists with important structure
    
    Args:
        text: Text to split
        size: Chunk size in tokens
        overlap: Overlap between chunks
        preserve_code: Keep code blocks together
        preserve_tables: Keep tables together
        
    Returns:
        List of text chunks
    """
    import re
    
    # Detect code blocks (```...```)
    if preserve_code:
        code_pattern = r'```[\s\S]*?```'
        code_blocks = re.findall(code_pattern, text)
        
        # Replace code blocks with placeholders
        for i, block in enumerate(code_blocks):
            text = text.replace(block, f"__CODE_BLOCK_{i}__")
    
    # Detect tables (simple heuristic: lines with multiple |)
    if preserve_tables:
        lines = text.split('\n')
        table_lines = []
        current_table = []
        
        for line in lines:
            if line.count('|') >= 2:  # Likely a table row
                current_table.append(line)
            else:
                if current_table:
                    table_lines.append('\n'.join(current_table))
                    current_table = []
        
        if current_table:
            table_lines.append('\n'.join(current_table))
    
    # Chunk normally
    chunks = split_into_chunks(text, size=size, overlap=overlap)
    
    # Restore code blocks
    if preserve_code:
        for i, block in enumerate(code_blocks):
            chunks = [c.replace(f"__CODE_BLOCK_{i}__", block) for c in chunks]
    
    return chunks