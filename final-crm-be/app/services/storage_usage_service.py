"""
Storage Service
Handles storage usage tracking in KB units (decimal base: 1000 bytes = 1 KB).
"""
import uuid
import logging
from typing import Dict, Any, Optional, Literal
from supabase import create_client
from app.config import settings


logger = logging.getLogger(__name__)


class StorageService:
    """
    Service for managing storage usage in table storage_usages.
    
    KB Convention:
    - All sizes stored in KB (decimal: 1000 bytes = 1 KB)
    - Kuota 10 GB = 10,000,000 KB
    """

    def __init__(self):
        """Initialize Supabase client."""
        self.client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

    def _bytes_to_kb(self, bytes_size: int) -> int:
        """Convert bytes to KB (decimal: 1000 bytes = 1 KB)."""
        return bytes_size // 1000
    
    def _empty_documents_storage(self) -> Dict[str, Any]:
        """Return empty documents_storage template."""
        return {
            "document": {"total": 0, "size": 0},
            "image": {"total": 0, "size": 0},
            "audio": {"total": 0, "size": 0},
            "video": {"total": 0, "size": 0}
        }

    def _mime_to_bucket(self, mime_type: Optional[str]) -> str:
        """Categorize MIME type to storage bucket."""
        if not mime_type:
            return "document"
        mime = mime_type.lower()
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
        return "document"

    def _get_usage_row(self, organization_id: str) -> Optional[Dict[str, Any]]:
        """Get existing usage row (safe pattern)."""
        try:
            resp = (
                self.client.table("storage_usages")
                .select("*")
                .eq("organization_id", organization_id)
                .single()  # Expect 0 atau 1 row
                .execute()
            )
            return resp.data  # Dict kalau ada
        except Exception:  # APIError, ValidationError, dll
            return None  # 0 row → return None
    
    def _get_files_row(self, organization_id: str) -> Optional[Dict[str, Any]]:
        """Get existing files row (maybe_single pattern)."""
        resp = (
            self.client.table("files")
            .select("*")
            .eq("organization_id", organization_id)
            .maybe_single()
            .execute()
        )
        return resp.data
    
    def recalculate_from_files(self, organization_id: str) -> Dict[str, Any]:
        """
        Recalculate storage_usages dari files table (termasuk trashed files).
        File trashed masih pakai storage space sampai permanent delete.
        """
        # Ambil SEMUA files (trashed pun dihitung)
        resp = (
            self.client.table("files")
            .select("mime_type, size")
            .eq("organization_id", organization_id)
            .execute()
        )
        
        files = resp.data or []
        
        # Agregasi manual (Python-side)
        ds = self._empty_documents_storage()
        total_file = 0
        total_kb = 0
        
        if files:
            for f in files:
                mime = f.get("mime_type")
                size_bytes = int(f.get("size") or 0)
                if size_bytes <= 0:
                    continue
                
                bucket = self._mime_to_bucket(mime)
                size_kb = self._bytes_to_kb(size_bytes)
                
                ds[bucket]["total"] += 1
                ds[bucket]["size"] += size_kb
                total_file += 1
                total_kb += size_kb
            logger.info(f"No files found for {organization_id}")
        
        # Buat payload dengan nilai nol kalau tidak ada files
        payload = {
            "documents_storage": ds,
            "total_file": total_file,
            "total_storage_usage": total_kb
        }
        
        # Update/insert ke storage_usages (selalu ada row)
        row = self._get_usage_row(organization_id)
        if row:
            resp = (
                self.client.table("storage_usages")
                .update(payload)
                .eq("id", row["id"])
                .execute()
            )
            logger.info(f"✅ Updated storage for {organization_id}: {total_file} files, {total_kb} KB")
        else:
            payload["id"] = str(uuid.uuid4())
            payload["organization_id"] = organization_id
            resp = self.client.table("storage_usages").insert(payload).execute()
            logger.info(f"✅ Created storage for {organization_id}: {total_file} files, {total_kb} KB")
        
        return {"status": "recalculated", "data": payload}

    def track_file_usage(
        self,
        organization_id: str,
        mime_type: Optional[str],
        file_bytes: int,
        operation: Literal["add", "remove"] = "add"
    ) -> Dict[str, Any]:
        """
        Track file usage: add/remove to storage_usages.

        Args:
            organization_id: Organization UUID (REQUIRED)
            mime_type: MIME type for categorization
            file_bytes: File size in bytes
            operation: "add" or "remove"

        Returns:
            Dict with status and updated data
        """

        bucket = self._mime_to_bucket(mime_type)
        size_kb = self._bytes_to_kb(file_bytes)
        delta = 1 if operation == "add" else -1

        # Get or create row
        row = self._get_usage_row(organization_id)
        if not row:
            return self.recalculate_from_files(organization_id)
        
        # Update existing row
        ds = row.get("documents_storage") or {}
        ds[bucket] = ds.get(bucket, {"total": 0, "size": 0})
        ds[bucket]["total"] = max(0, ds[bucket]["total"] + delta)
        ds[bucket]["size"] = max(0, ds[bucket]["size"] + delta * size_kb)

        total_file = max(0, row.get("total_file", 0) + delta)
        total_usage = max(0, row.get("total_storage_usage", 0) + delta * size_kb)

        payload = {
            "documents_storage": ds,
            "total_file": total_file,
            "total_storage_usage": total_usage
        }
        resp = (
            self.client.table("storage_usages")
            .update(payload)
            .eq("id", row["id"])
            .execute()
        )
        logger.info(f"✅ Updated storage usage for {organization_id}: {size_kb} KB {operation}")
        return {"status": "updated", "data": resp.data, "size_kb": size_kb}

    def get_storage_usage(self, organization_id: str) -> Optional[Dict[str, Any]]:
        """Get storage usage for organization."""
        row = self._get_usage_row(organization_id)
        return row
