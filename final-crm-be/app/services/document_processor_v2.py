"""
Document Processor Service V2 - OPTIMIZED FOR RAG
Enhanced text extraction and preprocessing for better RAG quality
"""
import io
import tempfile
import logging
import requests
import base64
import mimetypes
import asyncio
import re

from typing import Tuple, List, Optional, TYPE_CHECKING
from unstructured.partition.auto import partition
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

class DocumentProcessorV2:
    """Service for processing documents with enhanced text quality for RAG"""

    def __init__(self, storage_service: Optional['StorageService'] = None):
        self.storage_service = storage_service
        self.logger = logging.getLogger(__name__)
        self.logger.info("📦 Using Document Processor V2 (Optimized for RAG)")
        
        self.chroma_service = get_crm_chroma_service_v2()
        self.credit_service = get_credit_service()
        
        base = getattr(settings, "PROXY_BASE_URL", "http://localhost:6657")
        self.proxy_base_url = base.rstrip("/") if base else "http://localhost:6657"
        
        self.logger.info(f"🔗 V2 Proxy Target: {self.proxy_base_url}")

    def _normalize_text(self, text: str) -> str:
        """
        [NEW] Normalize text for consistent RAG quality across file types
        
        Fixes common issues:
        - Excessive whitespace from DOCX/PDF
        - Control characters
        - Inconsistent line endings
        - Table/list artifacts
        """
        if not text:
            return ""
        
        # 1. Remove control characters (except newlines, tabs)
        text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f-\x9f]', '', text)
        
        # 2. Normalize line endings
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        
        # 3. Remove excessive whitespace
        text = re.sub(r' {2,}', ' ', text)  # Multiple spaces → single space
        text = re.sub(r'\n{4,}', '\n\n\n', text)  # Max 3 newlines
        
        # 4. Fix common DOCX/PDF artifacts
        text = text.replace('\u200b', '')  # Zero-width space
        text = text.replace('\ufeff', '')  # BOM marker
        text = text.replace('\xa0', ' ')   # Non-breaking space
        
        # 5. Preserve important structures
        # Keep bullet points: •, -, *, numbers
        # Keep error codes: RC 503, ERR_404, etc. (already handled by regex in query)
        
        # 6. Remove page numbers if isolated (common in PDFs)
        text = re.sub(r'\n\d+\n', '\n', text)
        
        return text.strip()

    def process_document(
        self,
        content: bytes,
        filename: str,
        folder_path: str,
        organization_id: str,
        file_id: str
    ) -> Tuple[str, List[dict]]:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # 1. Handle Audio (Proxy V2)
        if ext in ("mp3", "wav", "m4a", "ogg", "flac"):
            return self._process_audio(content, filename, folder_path, organization_id, file_id)

        # 2. Handle Images (Proxy V2)
        if ext in ("jpg", "jpeg", "png", "webp", "tiff", "bmp", "gif"):
             return self._process_image(content, filename, folder_path, organization_id, file_id)

        # 3. Handle Documents (Local)
        elements = self._partition_by_type(content, filename, ext)
        
        # [IMPROVED] Clean + Normalize text
        clean_text = elements_to_clean_text(elements)
        normalized_text = self._normalize_text(clean_text)
        
        elements_json = self._elements_to_json(elements)

        return normalized_text, elements_json

    async def process_and_embed(self, agent_id: str, organization_id: str, file_content: bytes, file_type: str, filename: str):
        """
        Orchestrates: Parse -> Normalize -> Chunk -> Embed -> Save -> Bill
        """
        try:
            self.logger.info(f"⚙️ Processing file: {filename}")
            
            # 1. Parse File (Extract Text)
            text = self._extract_text(file_content, filename)
            if not text:
                return {"success": False, "error": "No text extracted"}
            
            # [NEW] 2. Normalize text (improves consistency)
            text = self._normalize_text(text)
            
            if len(text) < 50:
                return {"success": False, "error": "Document too short after normalization"}

            # 3. Chunk Text with improved overlap
            chunks = split_into_chunks(text, size=512, overlap=100)
            
            self.logger.info(f"✂️ Generated {len(chunks)} chunks (avg: {len(text)//len(chunks)} chars/chunk)")
            
            # 4. Prepare Metadata
            metadatas = [{"source": filename, "doc_id": filename} for _ in chunks]

            # 5. Save to Chroma
            result = await self.chroma_service.add_documents(
                agent_id=agent_id, 
                texts=chunks, 
                metadatas=metadatas
            )

            # 6. Handle Billing
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
        """
        Helper to extract raw text from various file types using partition logic.
        """
        try:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            elements = self._partition_by_type(content, filename, ext)
            raw_text = elements_to_clean_text(elements)
            
            # [IMPROVED] Apply normalization during extraction
            return self._normalize_text(raw_text)
            
        except Exception as e:
            self.logger.error(f"Text extraction failed for {filename}: {e}")
            return ""

    def _partition_by_type(self, content: bytes, filename: str, ext: str) -> List[Element]:
        self.logger.info(f"📄 Processing {filename} locally")
        fobj = io.BytesIO(content)

        try:
            if ext == "csv": 
                return partition_csv(file=fobj)
            elif ext in ("xlsx", "xls"): 
                return partition_xlsx(file=fobj)
            elif ext == "pdf": 
                # [IMPROVED] Better PDF strategy
                return partition(
                    file=fobj, 
                    file_filename=filename, 
                    strategy="fast",  # Use "hi_res" for scanned PDFs with OCR
                    skip_infer_table_types=["true"]
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
            
            # [IMPROVED] Normalize OCR text
            text = self._normalize_text(text)
            
            element = Text(text=text)
            return text, self._elements_to_json([element])

        except Exception as e:
            self.logger.error(f"❌ Image OCR failed: {e}")
            return "", []

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