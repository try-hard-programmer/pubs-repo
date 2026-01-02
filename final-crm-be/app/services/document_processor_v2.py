"""
Document Processor Service V2
Handles document parsing and preprocessing.
Hardcoded for Local V2 Proxy (HTTP).
"""
import io
import tempfile
import logging
import requests
import base64
import mimetypes

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

from app.utils.text_processing import elements_to_clean_text

if TYPE_CHECKING:
    from app.services.storage_service import StorageService

class DocumentProcessorV2:
    """Service for processing documents using Local V2 Proxy (HTTP) + Unstructured"""

    def __init__(self, storage_service: Optional['StorageService'] = None):
        self.storage_service = storage_service
        self.logger = logging.getLogger(__name__)
        self.logger.info("📦 Using Document Processor V2 (HTTP Localhost)")

        # [CHANGE] http instead of https
        self.proxy_base_url = "http://localhost:6657/v2"
        
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

    def _process_audio(
        self,
        content: bytes,
        filename: str,
        folder_path: str,
        organization_id: str,
        file_id: str
    ) -> Tuple[str, List[dict]]:
        if not self.storage_service:
            raise RuntimeError("Storage service is required for audio file processing")

        try:
            self.logger.info(f"🔈 Processing audio file via V2: {filename}")

            audio_url = self.storage_service.get_public_url(
                organization_id=organization_id,
                file_id=file_id,
                folder_path=folder_path
            )

            api_url = f"{self.proxy_base_url}/audio"
            headers = {"Content-Type": "application/json"}
            data = {"url": audio_url}

            # [CHANGE] No verify=False needed
            resp = requests.post(api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            
            result = resp.json()
            text = result.get("output", {}).get("result", "") or result.get("text", "")

            self.logger.info(f"✅ Transcribed audio: {len(text)} characters")

            element = Text(text=text)
            return text, self._elements_to_json([element])

        except Exception as e:
            self.logger.error(f"❌ Audio transcription failed: {e}")
            return "", []

    def _process_image(
            self,
            content: bytes,
            filename: str,
            folder_path: str,
            organization_id: str,
            file_id: str
    ) -> Tuple[str, List[dict]]:
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

            # [CHANGE] No verify=False needed
            resp = requests.post(api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            
            result = resp.json()
            text = result.get("content", "") or result.get("text", "")

            self.logger.info(f"✅ OCR Extraction length: {len(text)} characters")

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