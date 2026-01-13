"""
Document Processor Service V2
Handles document parsing, preprocessing, and ORCHESTRATES BILLING.
Hardcoded for Local V2 Proxy (HTTP).
"""
import io
import tempfile
import logging
import requests
import base64
import mimetypes
import asyncio

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

# [UPDATED] Import your high-quality chunker
from app.utils.chunking import split_into_chunks
from app.utils.text_processing import elements_to_clean_text
from app.config import settings
from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2

# [PRIORITY 2 FIX] Import Credit Service here (The Manager handles the money)
from app.services.credit_service import get_credit_service, CreditTransactionCreate, TransactionType

if TYPE_CHECKING:
    from app.services.storage_service import StorageService

logger = logging.getLogger(__name__)

class DocumentProcessorV2:
    """Service for processing documents using Local V2 Proxy (HTTP) + Unstructured"""

    def __init__(self, storage_service: Optional['StorageService'] = None):
        self.storage_service = storage_service
        self.logger = logging.getLogger(__name__)
        self.logger.info("📦 Using Document Processor V2 (HTTP Localhost)")
        
        self.chroma_service = get_crm_chroma_service_v2()
        
        # [PRIORITY 2 FIX] Initialize Credit Service
        self.credit_service = get_credit_service()
        
        base = getattr(settings, "PROXY_BASE_URL", "http://localhost:6657")
        self.proxy_base_url = base.rstrip("/") if base else "http://localhost:6657"
        
        self.logger.info(f"🔗 V2 Proxy Target: {self.proxy_base_url}")

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

        clean_text = elements_to_clean_text(elements)
        elements_json = self._elements_to_json(elements)

        return clean_text, elements_json

    async def process_and_embed(self, agent_id: str, organization_id: str, file_content: bytes, file_type: str, filename: str):
        """
        Orchestrates: Parse -> Chunk -> Embed -> Save -> Bill
        """
        try:
            self.logger.info(f"⚙️ Processing file: {filename}")
            
            # 1. Parse File (Extract Text)
            text = self._extract_text(file_content, filename)
            if not text:
                return {"success": False, "error": "No text extracted"}

            # 2. Chunk Text (Using your LangChain utility)
            # 512 tokens ~= 2000 chars. Good balance for RAG.
            chunks = split_into_chunks(text, size=512, overlap=50)
            
            self.logger.info(f"✂️ Generated {len(chunks)} chunks.")
            
            # 3. Prepare Metadata
            metadatas = [{"source": filename, "doc_id": filename} for _ in chunks]

            # 4. Save to Chroma (Async Call)
            # The service now returns Usage Stats, NOT a boolean
            result = await self.chroma_service.add_documents(
                agent_id=agent_id, 
                texts=chunks, 
                metadatas=metadatas
                # [NOTE] organization_id removed from here, we handle billing below
            )

            # 5. [PRIORITY 2 FIX] Handle Billing Logic Here
            if result.get("success"):
                try:
                    usage = result.get("usage", {})
                    cost = 0.0
                    
                    # Calculate cost based on usage report
                    if "cost_usd" in usage:
                        cost = float(usage["cost_usd"])
                    elif "total_tokens" in usage:
                        # Fallback calculation if Proxy didn't send cost
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
                    # Don't fail the upload if billing fails, but log it LOUDLY
                    self.logger.error(f"🚨 BILLING FAILURE for {organization_id}: {bill_err}")

                return {"success": True, "chunks": len(chunks)}
            
            else:
                return {"success": False, "error": result.get("error", "Chroma storage failed")}

        except Exception as e:
            self.logger.error(f"❌ Processing Failed: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    # --- HELPER METHODS ---

    def _extract_text(self, content: bytes, filename: str) -> str:
        """
        Helper to extract raw text from various file types using partition logic.
        """
        try:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            elements = self._partition_by_type(content, filename, ext)
            return elements_to_clean_text(elements)
        except Exception as e:
            self.logger.error(f"Text extraction failed for {filename}: {e}")
            return ""

    def _partition_by_type(self, content: bytes, filename: str, ext: str) -> List[Element]:
        self.logger.info(f"📄 Processing {filename} locally")
        fobj = io.BytesIO(content)

        try:
            if ext == "csv": return partition_csv(file=fobj)
            elif ext in ("xlsx", "xls"): return partition_xlsx(file=fobj)
            elif ext == "pdf": return partition(file=fobj, file_filename=filename, strategy="fast", skip_infer_table_types=["true"])
            elif ext == "docx": return partition_docx(file=fobj)
            elif ext in ("html", "htm"): return partition_html(file=fobj)
            elif ext in ("md", "markdown"): return partition_md(file=fobj)
            elif ext in ("txt", "log"): return partition_text(file=fobj)
            elif ext == "pptx": return partition_pptx(file=fobj)
            elif ext == "ppt": return partition_ppt(file=fobj)
            else:
                with tempfile.NamedTemporaryFile(delete=True, suffix=f".{ext}") as tmp:
                    tmp.write(content)
                    tmp.flush()
                    return partition(filename=tmp.name, strategy="auto")
        except Exception as e:
            self.logger.error(f"❌ Local processing failed for {filename}: {e}")
            raise

    def _process_audio(self, content: bytes, filename: str, folder_path: str, organization_id: str, file_id: str) -> Tuple[str, List[dict]]:
        if not self.storage_service:
            raise RuntimeError("Storage service is required for audio file processing")

        try:
            self.logger.info(f"🔈 Processing audio file via V2: {filename}")
            audio_url = self.storage_service.get_public_url(organization_id, file_id, folder_path)

            api_url = f"{self.proxy_base_url}/audio"
            headers = {"Content-Type": "application/json"}
            data = {"url": audio_url}

            resp = requests.post(api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            
            result = resp.json()
            text = result.get("output", {}).get("result", "") or result.get("text", "")
            
            element = Text(text=text)
            return text, self._elements_to_json([element])

        except Exception as e:
            self.logger.error(f"❌ Audio transcription failed: {e}")
            return "", []

    def _process_image(self, content: bytes, filename: str, folder_path: str, organization_id: str, file_id: str) -> Tuple[str, List[dict]]:
        try:
            self.logger.info(f"🌆 Processing image file via V2: {filename}")
            mime, _ = mimetypes.guess_type(filename)
            if not mime or not mime.startswith("image/"): mime = "image/png"

            b64 = base64.b64encode(content).decode("ascii")
            image_url = f"data:{mime};base64,{b64}"

            api_url = f"{self.proxy_base_url}/image/ocr"
            headers = {"Content-Type": "application/json"}
            data = {"image_url": image_url}

            resp = requests.post(api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            
            result = resp.json()
            text = result.get("content", "") or result.get("text", "")
            
            element = Text(text=text)
            return text, self._elements_to_json([element])

        except Exception as e:
            self.logger.error(f"❌ Image OCR failed: {e}")
            return "", []

    @staticmethod
    def _elements_to_json(elements: List[Element]) -> List[dict]:
        result = []
        for el in elements:
            element_dict = {"category": getattr(el, "category", None), "text": getattr(el, "text", None)}
            metadata = getattr(el, "metadata", None)
            element_dict["metadata"] = metadata.to_dict() if hasattr(metadata, "to_dict") else (metadata if isinstance(metadata, dict) else None)
            result.append(element_dict)
        return result