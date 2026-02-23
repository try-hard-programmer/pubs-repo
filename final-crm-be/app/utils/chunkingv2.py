from typing import List, Optional, Dict, Any
from langchain.text_splitter import RecursiveCharacterTextSplitter
import tiktoken
import re
from app.config import settings

STRUCTURE_AWARE_SEPARATORS = [
    # === DOCUMENT STRUCTURE (highest priority) ===
    "\n# ",          # H1 — top-level heading
    "\n## ",         # H2 — section heading (most common from hi_res)
    "\n### ",        # H3 — subsection
    "\n#### ",       # H4 — sub-subsection
    "\n##### ",      # H5 — deep nesting (rare but some docs use it)
    "\n###### ",     # H6 — deepest heading level
    
    # === STRUCTURED BLOCKS (from processor markers) ===
    "\n\n[Table]\n", # Table blocks (keep tables intact)
    "\n\n[Figure:",  # Figure blocks (keep with context)
    "\n\n---\n",     # Horizontal rule / section divider
    
    # === NATURAL TEXT BOUNDARIES ===
    "\n\n\n",        # Major section breaks
    "\n\n",          # Paragraph breaks
    "\n• ",          # Bullet list items (from docx processor)
    "\n- ",          # Dash list items (markdown style)
    "\n* ",          # Asterisk list items (markdown style)
    "\n",            # Line breaks
    ". ",            # Sentence endings
    "! ",            # Exclamations
    "? ",            # Questions
    "; ",            # Semi-colons
    ", ",            # Commas
    " ",             # Spaces
    "",              # Characters (last resort)
]

DEFAULT_SEPARATORS = [
    "\n\n\n",
    "\n\n",
    "\n",
    ". ",
    "! ",
    "? ",
    "; ",
    ", ",
    " ",
    "",
]

def _get_tokenizer(model: str = None) -> tiktoken.Encoding:
    """Get tiktoken tokenizer, with fallback."""
    model_name = model or getattr(settings, "OPENAI_MODEL", "gpt-4")
    try:
        return tiktoken.encoding_for_model(model_name)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")

def _presplit_structured_blocks(text: str, max_tokens: int, tokenizer: tiktoken.Encoding) -> List[str]:
    """
    Pre-split text into logical sections BEFORE the recursive splitter runs.
    
    This handles cases where [Table] or [Figure] blocks are larger than max_tokens
    and need special treatment, and ensures headings stay with their content.
    
    Returns a list of text segments that can each be independently chunked.
    """
    if not text or not text.strip():
        return []
    
    # Split on heading boundaries first
    # Pattern: split on lines starting with # through ###### (all markdown heading levels)
    sections = re.split(r'(?=\n#{1,6} )', text)
    
    result = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        
        token_count = len(tokenizer.encode(section))
        
        if token_count <= max_tokens:
            # Section fits in one chunk — keep it whole
            result.append(section)
        else:
            # Section is too large — split further but keep tables/figures intact
            # First, try splitting on table/figure boundaries within the section
            sub_parts = re.split(r'(?=\n\n\[Table\]\n|\n\n\[Figure:)', section)
            
            for part in sub_parts:
                part = part.strip()
                if part:
                    result.append(part)
    
    return result if result else [text]

def split_into_chunks(
    text: str,
    size: int = 512,
    overlap: int = 100,
    model: str = None,
    min_chunk_tokens: int = 30,
) -> List[str]:
    """
    Split text into chunks optimized for embedding + retrieval.
    
    Structure-aware: respects ## Headings, [Table], [Figure:] markers
    from DocumentProcessorV2 output.
    
    Args:
        text: Text to split
        size: Target chunk size in TOKENS (default 512)
        overlap: Overlap in tokens (default 100 — ~20% overlap for context continuity)
        model: Tiktoken model name (defaults to settings.OPENAI_MODEL)
        min_chunk_tokens: Minimum tokens per chunk; smaller chunks are discarded
        
    Returns:
        List of text chunks (strings)
    """
    if not text or not text.strip():
        return []
    
    tokenizer = _get_tokenizer(model)
    model_name = model or getattr(settings, "OPENAI_MODEL", "gpt-4")
    
    # Detect if text has structure markers from the new processor
    has_structure = bool(
        re.search(r'\n#{1,6} ', text) 
        or "[Table]" in text 
        or "[Figure:" in text
    )
    
    if has_structure:
        # STRUCTURE-AWARE PATH
        # Step 1: Pre-split into logical sections
        sections = _presplit_structured_blocks(text, max_tokens=size, tokenizer=tokenizer)
        
        # Step 2: Run recursive splitter on each section independently
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            model_name=model_name,
            chunk_size=size,
            chunk_overlap=overlap,
            separators=STRUCTURE_AWARE_SEPARATORS,
            is_separator_regex=False,
        )
        
        all_chunks = []
        for section in sections:
            section_tokens = len(tokenizer.encode(section))
            
            if section_tokens <= size:
                # Section fits — add as single chunk
                if section_tokens >= min_chunk_tokens:
                    all_chunks.append(section.strip())
            else:
                # Section needs splitting — use recursive splitter
                docs = splitter.create_documents([section])
                for doc in docs:
                    content = doc.page_content.strip()
                    if not content:
                        continue
                    token_count = len(tokenizer.encode(content))
                    if token_count >= min_chunk_tokens:
                        all_chunks.append(content)
        
        # Step 3: Re-attach orphaned headings
        # If a heading got split into its own tiny chunk, merge it with the next chunk
        merged_chunks = []
        i = 0
        while i < len(all_chunks):
            chunk = all_chunks[i]
            chunk_tokens = len(tokenizer.encode(chunk))
            
            # Check if this is a heading-only chunk (very short, starts with any # level)
            is_heading_only = (
                chunk_tokens < 50 
                and re.match(r'^#{1,6} ', chunk)
                and "\n" not in chunk.strip()
            )
            
            if is_heading_only and i + 1 < len(all_chunks):
                # Merge heading with next chunk
                next_chunk = all_chunks[i + 1]
                combined = f"{chunk}\n\n{next_chunk}"
                combined_tokens = len(tokenizer.encode(combined))
                
                if combined_tokens <= size * 1.1:  # Allow 10% overflow for heading merge
                    merged_chunks.append(combined)
                    i += 2
                    continue
            
            merged_chunks.append(chunk)
            i += 1
        
        return merged_chunks if merged_chunks else all_chunks
    
    else:
        # PLAIN TEXT PATH (backward compatible — same behavior as V1)
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            model_name=model_name,
            chunk_size=size,
            chunk_overlap=overlap,
            separators=DEFAULT_SEPARATORS,
            is_separator_regex=False,
        )
        
        docs = splitter.create_documents([text])
        
        chunks = []
        for doc in docs:
            content = doc.page_content.strip()
            if not content:
                continue
            token_count = len(tokenizer.encode(content))
            if token_count >= min_chunk_tokens:
                chunks.append(content)
        
        return chunks

def split_into_chunks_with_metadata(
    text: str,
    filename: str,
    file_id: str,
    agent_id: str,
    agent_name: str,
    organization_id: str,
    size: int = 512,
    overlap: int = 100,
    min_chunk_tokens: int = 30,
) -> tuple[List[str], List[Dict[str, Any]]]:
    """
    Split text and generate rich metadata per chunk.
    
    Returns:
        (chunks, metadatas) — parallel lists ready for ChromaDB.
        
    Each metadata dict includes:
        - file_id, filename, agent_id, agent_name, organization_id
        - chunk_index, total_chunks, token_count
        - has_table, has_figure, has_heading (content flags)
        - section_title (extracted from nearest heading if available)
        - processor version tag
    """
    if not text or not text.strip():
        return [], []
    
    model_name = getattr(settings, "OPENAI_MODEL", "gpt-4")
    tokenizer = _get_tokenizer(model_name)
    
    # Split
    raw_chunks = split_into_chunks(
        text=text, 
        size=size, 
        overlap=overlap, 
        min_chunk_tokens=min_chunk_tokens
    )
    
    if not raw_chunks:
        return [], []
    
    # Build metadata
    chunks = []
    metadatas = []
    
    for idx, chunk_text in enumerate(raw_chunks):
        token_count = len(tokenizer.encode(chunk_text))
        
        # Detect content type flags
        has_table = "[Table]" in chunk_text
        has_figure = "[Figure:" in chunk_text
        has_heading = bool(re.match(r'^#{1,6} ', chunk_text))
        
        # Extract section title from heading if present
        section_title = ""
        heading_match = re.match(r'^#{1,4}\s+(.+?)(?:\n|$)', chunk_text)
        if heading_match:
            section_title = heading_match.group(1).strip()
        
        chunks.append(chunk_text)
        metadatas.append({
            "file_id": file_id,
            "filename": filename,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "organization_id": organization_id,
            "chunk_index": idx,
            "total_chunks": len(raw_chunks),
            "token_count": token_count,
            "doc_id": file_id,
            "processor": "v2",
            "has_table": has_table,
            "has_figure": has_figure,
            "has_heading": has_heading,
            "section_title": section_title,
        })
    
    return chunks, metadatas