"""
Document Processor Service
Handles document parsing and preprocessing for various file formats using local Unstructured library
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
from unstructured.documents.elements import Text, Element

from app.config import settings
from app.utils.text_processing import elements_to_clean_text

# Avoid circular import - only import for type checking
if TYPE_CHECKING:
    from app.services.storage_service import StorageService


class DocumentProcessor:
    """Service for processing various document formats using local Unstructured library"""

    def __init__(self, storage_service: Optional['StorageService'] = None):
        """
        Initialize DocumentProcessor with optional storage service.

        Args:
            storage_service: Storage service for handling file uploads (needed for audio processing)
        """
        self.storage_service = storage_service
        self.logger = logging.getLogger(__name__)
        self.logger.info("📦 Using local document processing with Unstructured library")

    def process_document(
        self,
        content: bytes,
        filename: str,
        folder_path: str,
        organization_id: str,
        file_id: str
    ) -> Tuple[str, List[dict]]:
        """
        Process document content using local Unstructured library

        Args:
            content: File content as bytes
            filename: Original filename
            folder_path: Folder path for storage
            organization_id: Organization ID
            file_id: File ID

        Returns:
            Tuple of (clean_text, elements_json)
        """
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # Handle audio files separately (uses transcription API)
        if ext in ("mp3", "wav", "m4a", "ogg", "flac"):
            return self._process_audio(content, filename, folder_path, organization_id, file_id)

        # Handle image files separately (uses OCR API)
        # if ext in ("jpg", "jpeg", "png", "webp", "tiff", "bmp", "gif"):
        #     return self._process_image(content, filename, folder_path, organization_id, file_id)

        # Process document locally
        elements = self._partition_by_type(content, filename, ext)

        # Convert to clean text and JSON
        clean_text = elements_to_clean_text(elements)
        elements_json = self._elements_to_json(elements)

        return clean_text, elements_json

    def _partition_by_type(self, content: bytes, filename: str, ext: str) -> List[Element]:
        """
        Partition document based on file type using local Unstructured library

        Args:
            content: File content as bytes
            filename: Original filename
            ext: File extension

        Returns:
            List of document elements
        """
        self.logger.info(f"📄 Processing {filename} locally")

        fobj = io.BytesIO(content)

        try:
            # Route by extension
            if ext == "csv":
                return partition_csv(file=fobj)
            elif ext in ("xlsx", "xls"):
                return partition_xlsx(file=fobj)
            elif ext == "pdf":
                return partition(
                    file=fobj,
                    file_filename=filename,
                    strategy="fast",
                    skip_infer_table_types=["true"],
                )
            elif ext == "docx":
                return partition_docx(file=fobj)
            elif ext in ("html", "htm"):
                return partition_html(file=fobj)
            elif ext in ("md", "markdown"):
                return partition_md(file=fobj)
            elif ext in ("txt", "log"):
                return partition_text(file=fobj)
            elif ext in ("pptx", "ppt"):
                return partition_pptx(file=fobj)
            else:
                # Auto-detect for unknown file types
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
        """
        Process audio file using transcription API

        Args:
            content: Audio file content
            filename: Original filename
            folder_path: Folder path for storage
            organization_id: Organization ID
            file_id: File ID

        Returns:
            Tuple of (transcribed_text, elements_json)
        """
        # Audio file processing - requires storage service
        if not self.storage_service:
            raise RuntimeError("Storage service is required for audio file processing")

        try:
            self.logger.info(f"🔈 Processing audio file: {filename}")

            # Get public URL for audio file
            audio_url = self.storage_service.get_public_url(
                organization_id=organization_id,
                file_id=file_id,
                folder_path=folder_path
            )

            # Call transcription API
            api_url = "https://proxy.aigent.id/v1/audio"
            headers = {
                "Authorization": "Bearer " + settings.OPENAI_API_KEY,
                "Content-Type": "application/json"
            }
            data = {
                "url": audio_url,
                "model": "v3-large"
            }

            resp = requests.post(api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            result = resp.json()
            text = result.get("output", {}).get("result", "")

            self.logger.info(f"✅ Transcribed audio: {len(text)} characters")

            # Create element
            element = Text(text=text)
            elements = [element]

            # Convert to JSON
            elements_json = self._elements_to_json(elements)

            return text, elements_json

        except Exception as e:
            self.logger.error(f"❌ Audio transcription failed: {e}")
            # Return empty text on failure
            return "", []

    def _process_image(
            self,
            content: bytes,
            filename: str,
            folder_path: str,
            organization_id: str,
            file_id: str
    ) -> Tuple[str, List[dict]]:
        """
        Process audio file using transcription API

        Args:
            content: Audio file content
            filename: Original filename
            folder_path: Folder path for storage
            organization_id: Organization ID
            file_id: File ID

        Returns:
            Tuple of (transcribed_text, elements_json)
        """
        # Audio file processing - requires storage service
        if not self.storage_service:
            raise RuntimeError("Storage service is required for image file processing")

        try:
            self.logger.info(f"🌆 Processing image file: {filename}")

            # image url is base64 encoded
            mime, _ = mimetypes.guess_type(filename)
            if not mime or not mime.startswith("image/"):
                mime = "image/png"  # fallback

            b64 = base64.b64encode(content).decode("ascii")
            image_url = f"data:{mime};base64,{b64}"

            # Call transcription API
            api_url = "https://proxy.aigent.id/v1/image/ocr"
            headers = {
                "Authorization": "Bearer " + settings.OPENAI_API_KEY,
                "Content-Type": "application/json"
            }
            data = {
                "image_url": image_url,
            }

            resp = requests.post(api_url, headers=headers, json=data, timeout=60)
            resp.raise_for_status()
            result = resp.json()
            text = result.get("content", "")

            self.logger.info(f"✅ OCR Extraction leng: {len(text)} characters")
            self.logger.info(f"✅ OCR Extraction result: {text} characters")

            # Create element
            element = Text(text=text)
            elements = [element]

            # Convert to JSON
            elements_json = self._elements_to_json(elements)

            return text, elements_json

        except Exception as e:
            self.logger.error(f"❌ Audio transcription failed: {e}")
            # Return empty text on failure
            return "", []

    @staticmethod
    def _elements_to_json(elements: List[Element]) -> List[dict]:
        """
        Convert elements to JSON-serializable format

        Args:
            elements: List of document elements

        Returns:
            List of element dictionaries
        """
        result = []
        for el in elements:
            element_dict = {
                "category": getattr(el, "category", None),
                "text": getattr(el, "text", None),
            }

            # Handle metadata
            metadata = getattr(el, "metadata", None)
            if metadata:
                if hasattr(metadata, "to_dict"):
                    element_dict["metadata"] = metadata.to_dict()
                elif isinstance(metadata, dict):
                    element_dict["metadata"] = metadata
                else:
                    element_dict["metadata"] = None
            else:
                element_dict["metadata"] = None

            result.append(element_dict)

        return result
