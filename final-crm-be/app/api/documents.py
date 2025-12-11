"""
Document management API endpoints
Handles file upload, deletion, and querying with organization-specific collections
"""
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Depends
from typing import Optional
import pandas as pd
import logging

from app.models import DeleteItem, Item
from app.models.user import User
from app.services import ChromaDBService, DocumentProcessor, OpenAIService
from app.services.file_manager_service import get_file_manager_service
from app.services.storage_service import get_storage_service
from app.services.organization_service import get_organization_service
from app.utils import (
    split_into_chunks,
    to_clean_text_from_strs,
    process_audio_file
)
from app.config import settings
from app.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

# Initialize services
# DocumentProcessor needs storage_service for audio file processing
storage_service = get_storage_service()
document_processor = DocumentProcessor(storage_service=storage_service)
openai_service = OpenAIService()

# Image MIME types mapping
IMAGE_MIMES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "tiff": "image/tiff"
}


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    file_id: str = Form(None),
    current_user: User = Depends(get_current_user)
):
    """
    Upload and process a document to organization-specific ChromaDB collection.

    Supports: PDF, DOCX, CSV, XLSX, Images, Audio files

    **Requirements:**
    - User must belong to an organization
    - Documents are isolated per organization

    **Authentication:** Requires valid JWT token

    Args:
        file: File to upload
        file_id: Optional file identifier
        current_user: Authenticated user from JWT token

    Returns:
        Upload confirmation with file details

    Raises:
        400: If user has no organization
        500: If upload fails
    """
    # Get user's organization
    org_service = get_organization_service()
    user_org = await org_service.get_user_organization(current_user.user_id)

    if not user_org:
        raise HTTPException(
            status_code=400,
            detail="User must belong to an organization to upload documents"
        )

    organization_id = user_org.id
    logger.info(f"Uploading document to organization: {organization_id}")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File kosong atau gagal dibaca")

    filename = file.filename or "uploaded_file"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # Process based on file type
    if ext in settings.MUSIC_EXTENSIONS:
        text = await _process_audio(content, filename)
    elif ext in settings.IMAGE_EXTENSIONS:
        text = _process_image(content, ext)
    else:
        document_processor.process_document(content, filename, "/", organization_id, file_id)
        text, _ = document_processor.process_document(content, filename)

    # Split into chunks based on file type
    chunks = _get_chunks_by_type(text, ext)

    # ⭐ Add to organization-specific ChromaDB collection
    chromadb_service = ChromaDBService()
    actual_file_id = chromadb_service.add_chunks(
        chunks=chunks,
        filename=filename,
        organization_id=organization_id,  # Organization-specific collection
        file_id=file_id,
        batch_size=settings.DEFAULT_BATCH_SIZE,
        email=current_user.email
    )

    logger.info(f"Uploaded {len(chunks)} chunks to org_{organization_id}")

    return {
        "file_id": actual_file_id,
        "filename": filename,
        "chunks_length": len(chunks),
        "user_email": current_user.email,
        "organization_id": organization_id
    }


@router.delete("/delete")
async def delete_document(
    item: DeleteItem,
    current_user: User = Depends(get_current_user)
):
    """
    Delete document chunks by filename from organization-specific collection.

    **Requirements:**
    - User must belong to an organization
    - Only deletes documents from user's organization

    **Authentication:** Requires valid JWT token

    Args:
        item: Delete request with filename
        current_user: Authenticated user from JWT token

    Returns:
        Deletion confirmation

    Raises:
        400: If user has no organization
    """
    # Get user's organization
    org_service = get_organization_service()
    user_org = await org_service.get_user_organization(current_user.user_id)

    if not user_org:
        raise HTTPException(
            status_code=400,
            detail="User must belong to an organization"
        )

    organization_id = user_org.id

    # ⭐ Delete from organization-specific ChromaDB collection
    chromadb_service = ChromaDBService()
    chromadb_service.delete_documents(
        organization_id=organization_id,
        email=current_user.email,
        filename=item.filename
    )

    logger.info(f"Deleted {item.filename} from org_{organization_id}")

    return {
        "status": "ok",
        "organization_id": organization_id,
        "filter": {
            "$and": [
                {"email": {"$eq": current_user.email}},
                {"filename": {"$eq": item.filename}}
            ]
        },
        "deleted": True
    }


@router.post("/query")
async def query_documents(
    item: Item,
    current_user: User = Depends(get_current_user)
):
    """
    Query documents using semantic search within organization-specific collection.

    Returns unique file IDs that match the query.

    **Requirements:**
    - User must belong to an organization
    - Only searches documents within user's organization

    **Authentication:** Requires valid JWT token

    Args:
        item: Query request with search parameters
        current_user: Authenticated user from JWT token

    Returns:
        List of matching file IDs

    Raises:
        400: If user has no organization
    """
    # Get user's organization
    org_service = get_organization_service()
    user_org = await org_service.get_user_organization(current_user.user_id)

    if not user_org:
        raise HTTPException(
            status_code=400,
            detail="User must belong to an organization"
        )

    organization_id = user_org.id

    # ⭐ Query organization-specific ChromaDB collection
    chromadb_service = ChromaDBService()
    results = chromadb_service.query_documents(
        query=item.query,
        organization_id=organization_id,  # Organization-specific collection
        email=current_user.email,
        top_k=item.top_k or 5,
        where=item.where,
        include_distances=item.include_distances,
        include_embeddings=item.include_embeddings
    )

    metadatas = results.get("metadatas", [[]])[0] or []

    if not metadatas:
        return {"file_id": [], "organization_id": organization_id}

    # Extract unique file IDs
    file_ids = chromadb_service.extract_unique_file_ids(metadatas)
    logger.info(f"Found {len(file_ids)} unique files in org_{organization_id}")

    fm_service = get_file_manager_service()
    result = fm_service._search_file(item.query, organization_id)
    
    file_ids_from_search = [file['id'] for file in result.data]
    file_ids.update(file_ids_from_search)

    return {
        "file_id": list(file_ids),
        "organization_id": organization_id
    }


@router.get("/collections/{name}")
def list_collection_items(
    name: str,
    limit: Optional[int] = Query(None, ge=1, le=1000),
    offset: Optional[int] = Query(None, ge=0),
    include_embeddings: bool = False,
    source: Optional[str] = None,
    contains: Optional[str] = None,
):
    """
    List items from a ChromaDB collection
    """
    chromadb_service = ChromaDBService()
    return chromadb_service.get_collection_items(
        name=name,
        limit=limit,
        offset=offset,
        include_embeddings=include_embeddings,
        source=source,
        contains=contains
    )


# Helper functions

async def _process_audio(content: bytes, filename: str = "audio.mp3") -> str:
    """Process audio file using external transcription API"""
    transcribed_text = await process_audio_file(content, filename)

    if not transcribed_text:
        raise HTTPException(
            status_code=500,
            detail="Gagal melakukan transcribe audio"
        )

    return to_clean_text_from_strs([transcribed_text])


def _process_image(content: bytes, ext: str) -> str:
    """Process image file using GPT-4 Vision"""
    mime = IMAGE_MIMES.get(ext.lower(), "image/jpeg")
    ocr_text = openai_service.extract_text_from_image(content, mime)
    return to_clean_text_from_strs([ocr_text])


def _get_chunks_by_type(text: str, ext: str) -> list:
    """Get text chunks based on file type"""
    if ext == "pdf":
        return split_into_chunks(
            text, size=500, overlap=75,
            seps=["\n\n", "\n", " ", ""]
        )
    elif ext == "docx":
        return split_into_chunks(
            text, size=400, overlap=60,
            seps=["\n\n", "\n", " ", ""]
        )
    elif ext in {"md", "markdown", "html", "htm"}:
        return split_into_chunks(
            text, size=400, overlap=60,
            seps=["\n```", "\n# ", "\n## ", "\n- ", "\n\n", "\n", " ", ""]
        )
    elif ext in {"csv", "xlsx", "xls"}:
        if isinstance(text, pd.DataFrame):
            CHUNK_ROWS = 150
            chunks = []
            for start in range(0, len(text), CHUNK_ROWS):
                part = text.iloc[start:start + CHUNK_ROWS]
                md = part.to_markdown(index=False)
                chunks.append(md)
            return chunks
        else:
            return split_into_chunks(text, size=400, overlap=40)
    else:
        return split_into_chunks(
            text,
            size=settings.DEFAULT_CHUNK_SIZE,
            overlap=settings.DEFAULT_CHUNK_OVERLAP
        )
