import io
import tempfile
import logging
import requests
import base64
import mimetypes
import asyncio
import re
import hashlib

from typing import Tuple, List, Optional, Dict, Any, TYPE_CHECKING

# === LIGHTWEIGHT PARSERS (all in requirements.txt) ===
import pdfplumber
from docx import Document as DocxDocument
import pandas as pd
from charset_normalizer import detect as detect_encoding
import tiktoken
from bs4 import BeautifulSoup

# === UNSTRUCTURED (hi_res strategy for complex PDFs) ===
from unstructured.partition.auto import partition
from unstructured.partition.pdf import partition_pdf
from unstructured.partition.csv import partition_csv
from unstructured.partition.xlsx import partition_xlsx
from unstructured.partition.docx import partition_docx
from unstructured.partition.html import partition_html
from unstructured.partition.text import partition_text
from unstructured.partition.md import partition_md
from unstructured.partition.pptx import partition_pptx
from unstructured.partition.ppt import partition_ppt
from unstructured.documents.elements import Text, Element

from app.utils.chunkingv2 import split_into_chunks
from app.utils.text_processing import elements_to_clean_text
from app.config import settings
from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType

if TYPE_CHECKING:
    from app.services.storage_service import StorageService

logger = logging.getLogger(__name__)


# ============================================
# CONSTANTS
# ============================================

ALLOWED_KNOWLEDGE_EXTENSIONS = {"pdf", "docx", "txt", "csv"}
MAX_FILE_SIZE_MB = 10
MIN_TEXT_LENGTH = 50
MIN_WORD_COUNT = 20
MIN_ALPHA_RATIO = 0.3


# ============================================
# QUALITY METRICS
# ============================================

class DocumentQualityMetrics:
    """Tracks extraction quality for logging, validation, and metadata."""
    def __init__(self):
        self.char_count: int = 0
        self.word_count: int = 0
        self.token_count: int = 0
        self.content_hash: str = ""
        self.has_tables: bool = False
        self.has_images: bool = False
        self.extraction_method: str = ""
        self.page_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "char_count": self.char_count,
            "word_count": self.word_count,
            "token_count": self.token_count,
            "content_hash": self.content_hash,
            "has_tables": self.has_tables,
            "has_images": self.has_images,
            "extraction_method": self.extraction_method,
            "page_count": self.page_count,
        }


# ============================================
# MAIN CLASS
# ============================================

class DocumentProcessorV2:
    """Service for processing documents with enhanced text quality for RAG"""

    def __init__(self, storage_service: Optional['StorageService'] = None):
        self.storage_service = storage_service
        self.logger = logging.getLogger(__name__)
        self.logger.info("📦 Using Document Processor V2 (Improved)")

        self.chroma_service = get_crm_chroma_service_v2()
        self.credit_service = get_credit_service()

        base = getattr(settings, "PROXY_BASE_URL", "http://localhost:6657")
        self.proxy_base_url = base.rstrip("/") if base else "http://localhost:6657"

        # Tokenizer for accurate token counting
        try:
            self.tokenizer = tiktoken.encoding_for_model(settings.OPENAI_MODEL)
        except Exception:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        self.logger.info(f"🔗 V2 Proxy Target: {self.proxy_base_url}")

    # ============================================
    # VALIDATION (called from endpoint before processing)
    # ============================================

    @staticmethod
    def validate_knowledge_file(filename: str, content: bytes) -> str:
        if not filename or "." not in filename:
            raise ValueError(f"Invalid filename: '{filename}'. Must have an extension.")

        ext = filename.rsplit(".", 1)[-1].lower()

        if ext not in ALLOWED_KNOWLEDGE_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type: '.{ext}'. "
                f"Allowed: {', '.join(f'.{e}' for e in sorted(ALLOWED_KNOWLEDGE_EXTENSIONS))}"
            )

        size_mb = len(content) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            raise ValueError(
                f"File '{filename}' is {size_mb:.1f}MB. Maximum allowed: {MAX_FILE_SIZE_MB}MB."
            )

        return ext

    def validate_quality(self, text: str, metrics: DocumentQualityMetrics, filename: str):
        issues = []

        if metrics.char_count < MIN_TEXT_LENGTH:
            issues.append(f"Too short ({metrics.char_count} chars, min {MIN_TEXT_LENGTH})")

        if metrics.word_count < MIN_WORD_COUNT:
            issues.append(f"Too few words ({metrics.word_count}, min {MIN_WORD_COUNT})")

        if metrics.char_count > 0:
            alpha_ratio = sum(c.isalnum() for c in text) / metrics.char_count
            if alpha_ratio < MIN_ALPHA_RATIO:
                issues.append(f"Low content quality ({alpha_ratio:.0%} alphanumeric, min {MIN_ALPHA_RATIO:.0%})")

        if not text.strip():
            issues.append("Empty after normalization")

        if issues:
            msg = f"Document '{filename}' failed quality check: {'; '.join(issues)}"
            self.logger.warning(f"⚠️ {msg}")
            raise ValueError(msg)

    def generate_content_hash(self, text: str) -> str:
        normalized = re.sub(r'\s+', ' ', text.lower().strip())
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    async def check_duplicate(self, content_hash: str, agent_id: str, supabase) -> Optional[str]:
        try:
            response = supabase.table("knowledge_documents") \
                .select("name, metadata") \
                .eq("agent_id", agent_id) \
                .execute()

            if response.data:
                for doc in response.data:
                    existing_hash = (doc.get("metadata") or {}).get("content_hash")
                    if existing_hash and existing_hash == content_hash:
                        return doc.get("name", "unknown")
            return None
        except Exception as e:
            self.logger.warning(f"⚠️ Duplicate check failed (non-fatal): {e}")
            return None

    # ============================================
    # NORMALIZATION
    # ============================================

    def _normalize_text(self, text: str) -> str:
        if not text:
            return ""

        text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]', '', text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r' {2,}', ' ', text)
        text = re.sub(r'\n{4,}', '\n\n\n', text)
        text = text.replace('\u200b', '')
        text = text.replace('\ufeff', '')
        text = text.replace('\xa0', ' ')
        text = re.sub(r'\n\s*\d{1,3}\s*\n', '\n', text)

        return text.strip()

    # ============================================
    # MAIN ENTRY: process_document
    # ============================================

    def process_document(
        self,
        content: bytes,
        filename: str,
        folder_path: str,
        organization_id: str,
        file_id: str
    ) -> Tuple[str, DocumentQualityMetrics]:

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        metrics = DocumentQualityMetrics()

        if ext in ("mp3", "wav", "m4a", "ogg", "flac", "jpg", "jpeg", "png", "webp"):
            return "", metrics

        if ext in ("pdf", "docx", "csv", "txt"):
            raw_text, metrics = self._extract_lightweight(content, ext)
            normalized_text = self._normalize_text(raw_text)

            metrics.char_count = len(normalized_text)
            metrics.word_count = len(normalized_text.split())
            try:
                metrics.token_count = len(self.tokenizer.encode(normalized_text))
            except Exception:
                metrics.token_count = metrics.word_count
            metrics.content_hash = self.generate_content_hash(normalized_text)

            self.logger.info(
                f"✅ Extracted '{filename}': {metrics.word_count} words, "
                f"{metrics.token_count} tokens, method={metrics.extraction_method}"
            )

            return normalized_text, metrics

        elements = self._partition_by_type(content, filename, ext)
        clean_text = elements_to_clean_text(elements)
        normalized_text = self._normalize_text(clean_text)

        metrics.char_count = len(normalized_text)
        metrics.word_count = len(normalized_text.split())
        try:
            metrics.token_count = len(self.tokenizer.encode(normalized_text))
        except Exception:
            metrics.token_count = metrics.word_count
        metrics.content_hash = self.generate_content_hash(normalized_text)
        metrics.extraction_method = "unstructured_fallback"

        if any(el.category == "Table" for el in elements):
            metrics.has_tables = True

        return normalized_text, metrics

    # ============================================
    # LIGHTWEIGHT PARSERS
    # ============================================

    def _extract_lightweight(self, content: bytes, ext: str) -> Tuple[str, DocumentQualityMetrics]:
        if ext == "pdf":
            return self._extract_pdf(content)
        elif ext == "docx":
            return self._extract_docx(content)
        elif ext == "csv":
            return self._extract_csv(content)
        elif ext == "txt":
            return self._extract_txt(content)
        else:
            raise ValueError(f"No lightweight parser for '.{ext}'")

    # ============================================
    # PDF EXTRACTION (hi_res with pdfplumber fallback)
    # ============================================

    def _extract_pdf(self, content: bytes) -> Tuple[str, DocumentQualityMetrics]:
        """
        PDF extraction pipeline:
        1. Try unstructured hi_res (layout detection + table inference + image OCR)
        2. Fallback to pdfplumber if hi_res fails

        hi_res handles: multi-column layouts, complex tables, embedded images,
        charts with text, scanned pages, merged cells, equations (as text).
        """
        metrics = DocumentQualityMetrics()

        try:
            self.logger.info("📄 PDF: Using hi_res strategy (layout + tables + images)")

            elements = partition_pdf(
                file=io.BytesIO(content),
                strategy="hi_res",
                infer_table_structure=True,
                extract_images_in_pdf=True,
                extract_image_block_types=["Image", "Table"],
                languages=["ind", "eng"],
                max_partition=None,
            )

            if not elements:
                self.logger.warning("⚠️ hi_res returned no elements, falling back to pdfplumber")
                return self._extract_pdf_pdfplumber(content)

            text_blocks = []
            element_counts = {"text": 0, "table": 0, "image": 0, "title": 0, "other": 0}

            for el in elements:
                category = getattr(el, "category", "").lower() if hasattr(el, "category") else ""
                el_text = getattr(el, "text", "") or ""
                el_metadata = getattr(el, "metadata", None)

                # --- TITLE / HEADER ---
                if category in ("title", "header"):
                    element_counts["title"] += 1
                    if el_text.strip():
                        text_blocks.append(f"\n## {el_text.strip()}\n")

                # --- TABLE ---
                elif category == "table":
                    element_counts["table"] += 1
                    metrics.has_tables = True

                    html_table = None
                    if el_metadata:
                        html_table = getattr(el_metadata, "text_as_html", None)

                    if html_table:
                        readable = self._html_table_to_text(html_table)
                        if readable.strip():
                            text_blocks.append(f"\n[Table]\n{readable}\n")
                        elif el_text.strip():
                            text_blocks.append(f"\n[Table]\n{el_text.strip()}\n")
                    elif el_text.strip():
                        text_blocks.append(f"\n[Table]\n{el_text.strip()}\n")

                # --- IMAGE ---
                elif category == "image":
                    element_counts["image"] += 1
                    metrics.has_images = True

                    if el_text.strip() and len(el_text.strip()) > 5:
                        text_blocks.append(f"\n[Figure: {el_text.strip()}]\n")

                # --- NARRATIVE TEXT / LIST / OTHER ---
                else:
                    if el_text.strip():
                        element_counts["text"] += 1
                        text_blocks.append(el_text.strip())

            # Count pages
            page_numbers = set()
            for el in elements:
                el_metadata = getattr(el, "metadata", None)
                if el_metadata:
                    pn = getattr(el_metadata, "page_number", None)
                    if pn:
                        page_numbers.add(pn)

            metrics.page_count = len(page_numbers) if page_numbers else 0
            metrics.extraction_method = "unstructured_hi_res"

            self.logger.info(
                f"📄 PDF hi_res results: {metrics.page_count} pages, "
                f"{element_counts['text']} text blocks, "
                f"{element_counts['table']} tables, "
                f"{element_counts['image']} images, "
                f"{element_counts['title']} titles"
            )

            result = "\n\n".join(text_blocks)

            if len(result.strip()) < MIN_TEXT_LENGTH:
                self.logger.warning(
                    f"⚠️ hi_res extracted only {len(result.strip())} chars, "
                    f"falling back to pdfplumber"
                )
                return self._extract_pdf_pdfplumber(content)

            return result, metrics

        except Exception as e:
            self.logger.error(f"⚠️ hi_res extraction failed: {e}")
            self.logger.info("🔄 Falling back to pdfplumber...")
            return self._extract_pdf_pdfplumber(content)

    def _extract_pdf_pdfplumber(self, content: bytes) -> Tuple[str, DocumentQualityMetrics]:
        """Fallback PDF extraction using pdfplumber. No image/layout support."""
        metrics = DocumentQualityMetrics()
        metrics.extraction_method = "pdfplumber_fallback"
        text_blocks = []

        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                metrics.page_count = len(pdf.pages)

                for page in pdf.pages:
                    page_parts = []

                    tables = page.extract_tables()
                    if tables:
                        metrics.has_tables = True
                        for table in tables:
                            if not table:
                                continue
                            headers = table[0] if table[0] else []
                            for row_idx, row in enumerate(table):
                                if not row:
                                    continue
                                cells = [str(c).strip() if c else "" for c in row]
                                if row_idx == 0:
                                    page_parts.append(" | ".join(cells))
                                else:
                                    if headers and len(headers) == len(cells):
                                        row_text = ", ".join(
                                            f"{str(h).strip()}: {c}"
                                            for h, c in zip(headers, cells) if c
                                        )
                                    else:
                                        row_text = " | ".join(cells)
                                    if row_text.strip():
                                        page_parts.append(row_text)

                    page_text = page.extract_text(layout=False)
                    if page_text and page_text.strip():
                        page_parts.append(page_text.strip())

                    if page_parts:
                        text_blocks.append("\n".join(page_parts))

            self.logger.info(f"📄 pdfplumber fallback: {metrics.page_count} pages extracted")
            return "\n\n".join(text_blocks), metrics

        except Exception as e:
            self.logger.error(f"PDF extraction failed: {e}")
            raise ValueError(f"Failed to extract text from PDF: {e}")

    # ============================================
    # HTML TABLE CONVERTER
    # ============================================

    def _html_table_to_text(self, html: str) -> str:
        """Convert HTML table to column-aware text for RAG."""
        try:
            soup = BeautifulSoup(html, "html.parser")
            table = soup.find("table")
            if not table:
                rows = soup.find_all("tr")
            else:
                rows = table.find_all("tr")

            if not rows:
                return soup.get_text(separator=" ", strip=True)

            header_cells = rows[0].find_all(["th", "td"])
            headers = [cell.get_text(strip=True) for cell in header_cells]

            lines = []

            if headers and any(h for h in headers):
                lines.append(" | ".join(headers))

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if not any(c for c in cells):
                    continue

                if headers and len(headers) == len(cells):
                    line = ", ".join(
                        f"{h}: {c}" for h, c in zip(headers, cells) if c
                    )
                else:
                    line = " | ".join(cells)

                if line.strip():
                    lines.append(line)

            return "\n".join(lines)

        except Exception as e:
            self.logger.warning(f"⚠️ HTML table parse failed: {e}")
            try:
                return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
            except Exception:
                return html

    # ============================================
    # DOCX EXTRACTION (Enhanced with image OCR)
    # ============================================

    def _extract_docx(self, content: bytes) -> Tuple[str, DocumentQualityMetrics]:
        """
        DOCX extraction using python-docx + unstructured for images.
        1. python-docx: text with heading hierarchy + tables
        2. If images found: unstructured partition_docx for OCR
        """
        metrics = DocumentQualityMetrics()
        metrics.extraction_method = "python_docx"
        text_blocks = []

        try:
            doc = DocxDocument(io.BytesIO(content))

            # --- Check if document has images ---
            has_images = False
            try:
                for rel in doc.part.rels.values():
                    if "image" in str(getattr(rel, 'reltype', '')).lower():
                        has_images = True
                        break
            except Exception:
                pass

            # --- Extract paragraphs with heading hierarchy ---
            for para in doc.paragraphs:
                text = para.text.strip()
                if not text:
                    continue

                style_name = para.style.name if para.style else ""
                if style_name.startswith("Heading"):
                    try:
                        level = int(style_name.split()[-1])
                    except (ValueError, IndexError):
                        level = 1
                    prefix = "#" * min(level, 4)
                    text_blocks.append(f"\n{prefix} {text}\n")
                elif style_name == "List Paragraph" or style_name.startswith("List"):
                    text_blocks.append(f"• {text}")
                else:
                    text_blocks.append(text)

            # --- Extract tables with column context ---
            if doc.tables:
                metrics.has_tables = True
                for table in doc.tables:
                    rows = table.rows
                    if not rows:
                        continue

                    headers = [cell.text.strip() for cell in rows[0].cells]

                    # Deduplicate headers (merged cells produce duplicates)
                    seen_headers = []
                    for h in headers:
                        if h not in seen_headers:
                            seen_headers.append(h)
                        else:
                            seen_headers.append("")
                    headers = seen_headers

                    table_lines = ["\n[Table]"]
                    table_lines.append(" | ".join(h for h in headers if h))

                    for row in rows[1:]:
                        cells = [cell.text.strip() for cell in row.cells]

                        clean_cells = []
                        for i, c in enumerate(cells):
                            if i == 0 or c != cells[i - 1]:
                                clean_cells.append(c)
                            else:
                                clean_cells.append("")
                        cells = clean_cells

                        if headers and len(headers) == len(cells):
                            row_text = ", ".join(
                                f"{h}: {c}" for h, c in zip(headers, cells) if c and h
                            )
                        else:
                            row_text = " | ".join(c for c in cells if c)

                        if row_text.strip():
                            table_lines.append(row_text)

                    text_blocks.extend(table_lines)

            # --- Extract image text via unstructured ---
            if has_images:
                metrics.has_images = True
                try:
                    self.logger.info("🖼️ DOCX contains images, running OCR via unstructured...")
                    elements = partition_docx(
                        file=io.BytesIO(content),
                        infer_table_structure=False,
                    )

                    for el in elements:
                        category = getattr(el, "category", "").lower() if hasattr(el, "category") else ""
                        el_text = getattr(el, "text", "") or ""

                        if category == "image" and el_text.strip() and len(el_text.strip()) > 5:
                            text_blocks.append(f"\n[Figure: {el_text.strip()}]\n")

                    self.logger.info("✅ DOCX image OCR complete")
                    metrics.extraction_method = "python_docx+unstructured_ocr"

                except Exception as img_err:
                    self.logger.warning(f"⚠️ DOCX image OCR failed (non-fatal): {img_err}")

            return "\n".join(text_blocks), metrics

        except Exception as e:
            self.logger.error(f"DOCX extraction failed: {e}")
            raise ValueError(f"Failed to extract text from DOCX: {e}")

    # CSV EXTRACTION (unchanged)
    def _extract_csv(self, content: bytes) -> Tuple[str, DocumentQualityMetrics]:
        metrics = DocumentQualityMetrics()
        metrics.has_tables = True
        metrics.extraction_method = "pandas_csv"

        try:
            # ★ FIX: charset_normalizer returns ResultObject, not dict
            detected = detect_encoding(content)
            if isinstance(detected, dict):
                encoding = detected.get("encoding", "utf-8") or "utf-8"
            elif hasattr(detected, "best"):
                best = detected.best()
                encoding = best.encoding if best else "utf-8"
            else:
                encoding = "utf-8"
            

            df = pd.read_csv(io.BytesIO(content), encoding=encoding)
            if df.empty:
                return "", metrics

            columns = list(df.columns)
            
            # ★ Build a rich schema header that RAG can actually find
            schema_lines = [
                f"## CSV Data Overview",
                f"Total rows: {len(df)}. Total columns: {len(columns)}.",
                f"Column names: {', '.join(columns)}.",
            ]
            
            # ★ Add column types + sample values so LLM understands the data
            for col in columns:
                non_null = df[col].dropna()
                if non_null.empty:
                    schema_lines.append(f"- {col}: all empty")
                    continue
                dtype = str(df[col].dtype)
                sample = str(non_null.iloc[0])[:80]
                unique_count = non_null.nunique()
                schema_lines.append(f"- {col} ({dtype}): {unique_count} unique values, example: {sample}")
            
            text_blocks = ["\n".join(schema_lines)]

            # ★ FIX: Section markers now cover ALL rows including first batch
            ROWS_PER_SECTION = 50
            total_rows = len(df)
            
            for idx, (_, row) in enumerate(df.iterrows()):
                # Insert section header at the START of each batch
                if idx % ROWS_PER_SECTION == 0:
                    section_end = min(idx + ROWS_PER_SECTION, total_rows)
                    text_blocks.append(f"\n## Rows {idx + 1}-{section_end}\n")
                
                parts = []
                for col in columns:
                    val = row[col]
                    if pd.notna(val):
                        val_str = str(val).strip()
                        if val_str:
                            parts.append(f"{col}: {val_str}")
                if parts:
                    text_blocks.append(". ".join(parts))

            return "\n".join(text_blocks), metrics

        except Exception as e:
            self.logger.error(f"CSV extraction failed: {e}")
            raise ValueError(f"Failed to extract text from CSV: {e}")
    
    # ============================================
    # TXT EXTRACTION (unchanged)
    # ============================================

    def _extract_txt(self, content: bytes) -> Tuple[str, DocumentQualityMetrics]:
        metrics = DocumentQualityMetrics()
        metrics.extraction_method = "charset_txt"
        try:
            detected = detect_encoding(content)
            if isinstance(detected, dict):
                encoding = detected.get("encoding", "utf-8") or "utf-8"
            elif hasattr(detected, "best"):
                best = detected.best()
                encoding = best.encoding if best else "utf-8"
            else:
                encoding = "utf-8"
            
            return content.decode(encoding, errors="replace"), metrics
        except Exception:
            return content.decode("utf-8", errors="ignore"), metrics

    # ============================================
    # UNSTRUCTURED FALLBACK (for html, md, pptx, xlsx, etc.)
    # ============================================

    def _partition_by_type(self, content: bytes, filename: str, ext: str) -> List[Element]:
        self.logger.info(f"📄 Processing {filename} via unstructured")
        fobj = io.BytesIO(content)

        try:
            if ext == "csv":
                return partition_csv(file=fobj)
            elif ext in ("xlsx", "xls"):
                return partition_xlsx(file=fobj)
            elif ext == "pdf":
                return partition_pdf(
                    file=fobj,
                    strategy="hi_res",
                    infer_table_structure=True,
                    extract_images_in_pdf=True,
                    languages=["ind", "eng"],
                )
            elif ext == "docx":
                return partition_docx(file=fobj)
            elif ext in ("html", "htm"):
                return partition_html(file=fobj)
            elif ext in ("md", "markdown"):
                return partition_md(file=fobj)
            elif ext in ("txt", "log"):
                return partition_text(file=fobj)
            elif ext == "pptx":
                return partition_pptx(file=fobj)
            elif ext == "ppt":
                return partition_ppt(file=fobj)
            else:
                with tempfile.NamedTemporaryFile(delete=True, suffix=f".{ext}") as tmp:
                    tmp.write(content)
                    tmp.flush()
                    return partition(filename=tmp.name, strategy="auto")

        except Exception as e:
            self.logger.error(f"❌ Local processing failed for {filename}: {e}")
            raise

    # ============================================
    # PROXY HANDLERS (image OCR + audio)
    # ============================================

    def _process_image(self, content: bytes, filename: str, folder_path: str, organization_id: str, file_id: str) -> Tuple[str, List[dict]]:
        try:
            self.logger.info(f"🌆 Processing image file via V2: {filename}")
            mime, _ = mimetypes.guess_type(filename)
            if not mime or not mime.startswith("image/"):
                mime = "image/png"

            b64 = base64.b64encode(content).decode("ascii")
            image_url = f"data:{mime};base64,{b64}"

            api_url = f"{self.proxy_base_url}/image/ocr"
            headers = {"Content-Type": "application/json"}
            data = {"image_url": image_url}

            resp = requests.post(api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()

            result = resp.json()
            text = result.get("content", "") or result.get("text", "")
            text = self._normalize_text(text)

            element = Text(text=text)
            return text, self._elements_to_json([element])

        except Exception as e:
            self.logger.error(f"❌ Image OCR failed: {e}")
            return "", []

    def _process_audio(self, content: bytes, filename: str, folder_path: str, organization_id: str, file_id: str) -> Tuple[str, List[dict]]:
        try:
            self.logger.warning(f"⚠️ Audio processing not fully implemented for {filename}")
            return "", []
        except Exception as e:
            self.logger.error(f"❌ Audio transcription failed: {e}")
            return "", []

    # ============================================
    # LEGACY METHODS (kept for backward compat)
    # ============================================

    async def process_and_embed(self, agent_id: str, organization_id: str, file_content: bytes, file_type: str, filename: str):
        try:
            self.logger.info(f"⚙️ Processing file: {filename}")

            text = self._extract_text(file_content, filename)
            if not text:
                return {"success": False, "error": "No text extracted"}

            text = self._normalize_text(text)

            if len(text) < 50:
                return {"success": False, "error": "Document too short after normalization"}

            chunks = split_into_chunks(text, size=512, overlap=100)

            self.logger.info(f"✂️ Generated {len(chunks)} chunks (avg: {len(text) // max(len(chunks), 1)} chars/chunk)")

            metadatas = [{"source": filename, "doc_id": filename} for _ in chunks]

            result = await self.chroma_service.add_documents(
                agent_id=agent_id,
                texts=chunks,
                metadatas=metadatas
            )

            if result.get("success"):
                try:
                    usage = result.get("usage", {})
                    cost = 0.0

                    if "cost_usd" in usage:
                        cost = float(usage["cost_usd"])
                    elif "total_tokens" in usage:
                        cost = usage["total_tokens"] * 0.0000002

                    if cost > 0 and organization_id:
                        self.logger.info(f"💸 Deducting ${cost:.6f} for embedding {len(chunks)} chunks...")
                        await self.credit_service.add_transaction(CreditTransactionCreate(
                            organization_id=organization_id,
                            amount=-cost,
                            description=f"Knowledge Embedding ({len(chunks)} chunks)",
                            transaction_type=TransactionType.USAGE,
                            metadata={"agent_id": agent_id, "file": filename}
                        ))
                    else:
                        self.logger.info("🆓 Embedding cost was $0.00.")

                except Exception as bill_err:
                    self.logger.error(f"🚨 BILLING FAILURE for {organization_id}: {bill_err}")

                return {"success": True, "chunks": len(chunks)}

            else:
                return {"success": False, "error": result.get("error", "Chroma storage failed")}

        except Exception as e:
            self.logger.error(f"❌ Processing Failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _extract_text(self, content: bytes, filename: str) -> str:
        try:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            elements = self._partition_by_type(content, filename, ext)
            raw_text = elements_to_clean_text(elements)
            return self._normalize_text(raw_text)
        except Exception as e:
            self.logger.error(f"Text extraction failed for {filename}: {e}")
            return ""

    @staticmethod
    def _elements_to_json(elements: List[Element]) -> List[dict]:
        result = []
        for el in elements:
            element_dict = {
                "category": getattr(el, "category", None),
                "text": getattr(el, "text", None)
            }
            metadata = getattr(el, "metadata", None)
            element_dict["metadata"] = (
                metadata.to_dict() if hasattr(metadata, "to_dict")
                else (metadata if isinstance(metadata, dict) else None)
            )
            result.append(element_dict)
        return result