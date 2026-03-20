"""
Document Processing Queue Service - Redis-based Background Processing

WHY THIS EXISTS:
FastAPI's BackgroundTasks runs in the same event loop as your API server.
When process_document_background runs heavy CPU work (hi_res PDF parsing, 
embedding API calls, etc.), it STARVES the event loop — blocking ALL other
HTTP requests including health checks on port 8080.

SOLUTION:
- API endpoint pushes a lightweight job descriptor to Redis
- A worker thread (started at app startup) pulls jobs via BRPOP
- Heavy processing runs in the worker's own event loop, never touching uvicorn's

ARCHITECTURE:
  [Client] → [FastAPI] → Redis LPUSH → [Worker Thread BRPOP] → ChromaDB
                ↑                              ↓
           Returns 202              Updates Supabase status
           immediately              (pending → completed/failed)
"""

import json
import logging
import asyncio
import threading
import time
import os
from typing import Optional, List
import redis
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger(__name__)


def _get_redis_client() -> redis.Redis:
    """Create Redis client from app settings (REDIS_HOST, REDIS_PORT, etc.)."""
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=settings.REDIS_PASSWORD or None,
        decode_responses=True,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )


# ============================================
# 1. QUEUE SERVICE (used by API endpoint)
# ============================================

class DocumentQueueService:
    """
    Thin Redis queue wrapper. The API endpoint calls enqueue() and returns 202.
    No heavy processing happens in the API process's event loop.
    """

    QUEUE_KEY = "syntra:doc_processing:queue"
    PROCESSING_KEY = "syntra:doc_processing:active"
    
    def __init__(self):
        self._redis: Optional[redis.Redis] = None
    
    @property
    def client(self) -> redis.Redis:
        if self._redis is None:
            self._redis = _get_redis_client()
            self._redis.ping()
            logger.info("✅ DocumentQueueService connected to Redis")
        return self._redis

    def enqueue(
        self,
        doc_id: str,
        agent_id: str,
        agent_name: str,
        organization_id: str,
        file_id: str,
        filename: str,
        bucket_name: str,
        temp_file_path: Optional[str] = None,
        storage_path: Optional[str] = None,
    ) -> bool:
        """
        Push a processing job to Redis. Returns True if queued successfully.

        temp_file_path: if provided, the worker reads from this local path instead
        of re-downloading from Supabase Storage, eliminating the redundant round-trip.
        Falls back to storage download automatically if the path no longer exists.

        storage_path: full path within the bucket (e.g. "folder_name/file_uuid").
        Used by the fallback download so files in sub-folders are fetched correctly.
        Defaults to file_id (root-level path) when not provided.
        """
        try:
            job = {
                "doc_id": doc_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "organization_id": organization_id,
                "file_id": file_id,
                "filename": filename,
                "bucket_name": bucket_name,
                "temp_file_path": temp_file_path,
                "storage_path": storage_path or file_id,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            
            self.client.lpush(self.QUEUE_KEY, json.dumps(job))
            queue_len = self.client.llen(self.QUEUE_KEY)
            
            logger.debug(f"queued: {filename} (depth={queue_len})")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to enqueue document: {e}")
            return False

    def get_queue_depth(self) -> int:
        """For monitoring/health checks."""
        try:
            return self.client.llen(self.QUEUE_KEY)
        except Exception:
            return -1


# ============================================
# 2. WORKER (runs in separate daemon thread)
# ============================================

class DocumentProcessingWorker:
    """
    Pulls jobs from Redis and processes them in a pool of worker threads.
    Never touches the FastAPI/uvicorn event loop.
    Each worker thread owns its own Redis client to avoid connection contention.
    """

    QUEUE_KEY = DocumentQueueService.QUEUE_KEY
    PROCESSING_KEY = DocumentQueueService.PROCESSING_KEY
    POLL_INTERVAL = 2  # seconds between Redis polls when queue is empty

    def __init__(self):
        self._running = False
        self._threads: List[threading.Thread] = []

    def start_in_thread(self):
        """Start N worker threads (configured by WORKER_CONCURRENCY env var)."""
        if self._threads and any(t.is_alive() for t in self._threads):
            logger.warning("⚠️ Document worker threads already running")
            return

        self._running = True
        concurrency = getattr(settings, "WORKER_CONCURRENCY", 3)

        for i in range(concurrency):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"doc-processing-worker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        logger.info(f"🚀 Document processing worker pool started ({concurrency} threads)")

    def stop(self):
        """Graceful shutdown — signal all threads to stop and wait."""
        self._running = False
        for t in self._threads:
            t.join(timeout=30)
        logger.info("🛑 Document processing worker pool stopped")

    def _worker_loop(self):
        """
        Main worker loop. Each thread runs this independently with its own Redis client.
        Uses BRPOP for efficient blocking wait (no busy-polling).
        """
        from supabase import create_client
        worker_name = threading.current_thread().name
        local_client = _get_redis_client()
        key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY
        local_supabase = create_client(settings.SUPABASE_URL, key)
        logger.debug(f"[{worker_name}] started")

        while self._running:
            try:
                result = local_client.brpop(self.QUEUE_KEY, timeout=self.POLL_INTERVAL)

                if result is None:
                    continue

                _, job_json = result
                job = json.loads(job_json)

                logger.info(f"[{worker_name}] → {job['filename']}")

                self._process_job(job, local_client, local_supabase)

            except Exception as e:
                logger.error(f"❌ [{worker_name}] Unexpected error in loop: {e}", exc_info=True)
                time.sleep(5)

    def _process_job(self, job: dict, redis_client: redis.Redis, supabase_client):
        """
        Process a single document job. Creates its own event loop
        since we're in a worker thread (not the uvicorn thread).
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._process_job_async(job, redis_client, supabase_client))
        except Exception as e:
            logger.error(f"❌ [DocWorker] Job failed for {job.get('filename')}: {e}", exc_info=True)
        finally:
            loop.close()
    
    async def _process_job_async(self, job: dict, redis_client: redis.Redis, supabase_client):
        """
        Async processing logic — same steps as the old process_document_background,
        but now runs in a dedicated thread with its own event loop.
        redis_client and supabase_client are per-thread connections owned by the calling worker.
        """
        from app.services.document_processor_v2 import get_document_processor, EmbeddingNotSupportedError
        from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2
        from app.utils.chunkingv2 import split_into_chunks_with_metadata
        from app.services.file_manager_service import get_file_manager_service  # ← BARU!
        
        # Extract job data
        doc_id = job["doc_id"]
        organization_id = job["organization_id"]
        file_id = job["file_id"]
        filename = job["filename"]
        bucket_name = job["bucket_name"]
        agent_id = job.get("agent_id")  # None untuk files
        
        # Detect table type
        table_name = "knowledge_documents" if (agent_id and agent_id != "file_manager") else "files"
        is_knowledge_doc = table_name == "knowledge_documents"
        
        logger.debug(f"[{threading.current_thread().name}] processing {filename}")

        supabase = supabase_client
        doc_processor = get_document_processor()

        _t0 = time.monotonic()

        try:
            # =============================================
            # STEP 0: READ FILE (temp path first, storage fallback)
            # =============================================
            temp_file_path = job.get("temp_file_path")
            file_content = None

            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    with open(temp_file_path, "rb") as tmp_f:
                        file_content = tmp_f.read()
                    os.unlink(temp_file_path)
                    logger.debug(f"📂 [{filename}] Read from temp file (skipped storage download)")
                except Exception as tmp_err:
                    logger.warning(f"⚠️ Temp file read failed, falling back to storage: {tmp_err}")
                    file_content = None
                    # Ensure temp file is cleaned up even if read failed mid-way
                    try:
                        os.unlink(temp_file_path)
                    except Exception:
                        pass
            elif not file_content:
                # Log why we're falling back so we can diagnose staging issues
                reason = "path=None" if not temp_file_path else f"not found on disk ({temp_file_path})"
                logger.warning(f"⚠️ [{filename}] temp file {reason} — falling back to storage download")

            if not file_content:
                # Use storage_path (full path including any folder prefix) so files
                # inside sub-folders are fetched from the correct location.
                # Falling back to file_id alone would fail for non-root uploads.
                dl_path = job.get("storage_path") or file_id
                # Always use the storage_service singleton (guaranteed SERVICE_ROLE_KEY)
                # instead of local_supabase whose auth state may be stale or wrong.
                # This is the same client that successfully generates signed URLs.
                from app.services.storage_service import get_storage_service as _get_storage_svc
                try:
                    file_content = _get_storage_svc().client.storage.from_(bucket_name).download(dl_path)
                except Exception as dl_err:
                    raise RuntimeError(
                        f"Storage fallback failed (bucket={bucket_name}, path={dl_path}): {dl_err}"
                    ) from dl_err

            if not file_content:
                raise RuntimeError("File is empty (temp and storage both failed)")


            # =============================================
            # STEP 1: COMMON PROCESSING (extract + quality)
            # =============================================
            clean_text, metrics = doc_processor.process_document(
                file_content, filename, bucket_name, organization_id, file_id
            )
            del file_content

            if not clean_text :
               raise ValueError("No text extracted")
            
            if is_knowledge_doc:
                doc_processor.validate_quality(clean_text, metrics, filename)
            
            # =============================================
            # STEP 2: DUPLICATE CHECK (hanya untuk knowledge_docs)
            # =============================================
            if is_knowledge_doc:
                existing_doc = await doc_processor.check_duplicate(
                    content_hash=metrics.content_hash,
                    agent_id=agent_id,
                    supabase=supabase,
                )
                if existing_doc:
                    raise ValueError(f"Duplicate: '{existing_doc}'")
            
            # =============================================
            # STEP 3: CHUNKING
            # =============================================
            agent_name = job.get("agent_name", "File Manager")
            chunks, chunk_metas = split_into_chunks_with_metadata(
                text=clean_text, filename=filename, file_id=file_id,
                agent_id=agent_id, agent_name=agent_name,
                organization_id=organization_id,
                size=512, overlap=100
            )
            del clean_text
            if not chunks and agent_id != "file_manager":
                raise ValueError("No chunks generated")
            
            # =============================================
            # STEP 4: EMBEDDING + STORAGE
            # =============================================
            chroma_service = get_crm_chroma_service_v2()
            success = await chroma_service.add_documents(
                agent_id=agent_id,  # None OK untuk files?
                texts=chunks, metadatas=chunk_metas,
                organization_id=organization_id,
                filename=filename
            )
            if not success and agent_id != "file_manager":
                raise Exception("ChromaDB failed")
            
            # =============================================
            # STEP 5: UPDATE DB (table-specific)
            # =============================================
            updated_meta = {
                "file_id": file_id,
                "bucket": bucket_name,
                "processor_version": "v2",
                "chunks": len(chunks),
                "content_hash": metrics.content_hash,
                "word_count": metrics.word_count,
                "token_count": metrics.token_count,
                "has_tables": metrics.has_tables,
            }
            
            if is_knowledge_doc:
                # Knowledge documents: update metadata.status
                supabase.table("knowledge_documents").update({
                    "metadata": updated_meta
                }).eq("id", doc_id).execute()
            else:
                # Files: update embedding_status + metadata
                supabase.table("files").update({
                    "embedding_status": "completed",
                    "embedded_at": datetime.utcnow().isoformat(),
                    "metadata": updated_meta if chunks and agent_id == "file_manager" else {}
                }).eq("id", doc_id).execute()
            
            _elapsed = round((time.monotonic() - _t0) * 1000)
            logger.info(f"[worker] ✅ {filename} — {len(chunks)} chunks in {_elapsed}ms")

            # =============================================
            # STEP 6: NOTIFICATION (table-specific type)
            # =============================================
            
            # 1. FAIL-SAFE URL GENERATION (Strictly isolated from CRM)
            file_url = None
            if not is_knowledge_doc:  # <--- Protects CRM: Only runs for File Manager
                try:
                    from app.services.storage_service import get_storage_service
                    storage = get_storage_service(supabase)
                    
                    # Fetch safely
                    file_record = supabase.table("files").select("storage_path").eq("id", doc_id).execute()
                    if file_record.data:
                        storage_path = file_record.data[0].get("storage_path", "")
                        folder_path = "/" + storage_path.rsplit("/", 1)[0] + "/" if "/" in storage_path else "/"
                        
                        file_url = storage.get_file_url(
                            organization_id=organization_id,
                            file_id=file_id,
                            folder_path=folder_path,
                            expires_in=3600
                        )
                except Exception as url_err:
                    # If URL generation fails, catch it and DO NOT crash the worker
                    logger.error(f"⚠️ [Safe URL Gen] Failed to generate URL, continuing: {url_err}")

            # 2. Build the standard notification payload
            notification_type = (
                "document_upload_completed" if is_knowledge_doc 
                else "file_upload_completed"
            )
            notification = {
                "type": notification_type,
                "organization_id": organization_id,
                "agent_id": agent_id,
                "doc_id": doc_id,
                "filename": filename,
                "status": "completed",
                "table": table_name
            }
            
            # 3. Only inject the URL if we successfully generated it
            if file_url:
                notification["url"] = file_url

            channel = f"ws_org_{organization_id}"
            redis_client.publish(channel, json.dumps(notification))
            logger.debug(f"published {notification_type} → {channel}")
            
        except EmbeddingNotSupportedError as e:
            # File type is valid for storage but not embeddable (e.g. .zip, .exe, .iso).
            # Mark as "not_supported" — file stays in storage, no error shown to user.
            _elapsed = round((time.monotonic() - _t0) * 1000)
            logger.info(f"[worker] ⏭ {filename} — skipped in {_elapsed}ms")
            temp_file_path = job.get("temp_file_path")
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    pass
            try:
                if is_knowledge_doc:
                    supabase.table("knowledge_documents").update({
                        "metadata": {"status": "not_supported", "reason": str(e)}
                    }).eq("id", doc_id).execute()
                else:
                    supabase.table("files").update({
                        "embedding_status": "not_supported",
                        "embedding_error": str(e)[:500],
                    }).eq("id", doc_id).execute()
            except Exception:
                pass
            try:
                redis_client.publish(f"ws_org_{organization_id}", json.dumps({
                    "type": "file_upload_warning" if not is_knowledge_doc else "document_upload_warning",
                    "organization_id": organization_id,
                    "doc_id": doc_id,
                    "filename": filename,
                    "status": "not_supported",
                    "message": str(e),
                    "table": table_name,
                }))
            except Exception as pub_err:
                logger.error(f"⚠️ Publish failed: {pub_err}")

        except Exception as e:
            _elapsed = round((time.monotonic() - _t0) * 1000)
            logger.error(f"[worker] ❌ {filename} — failed in {_elapsed}ms: {e}")

            # Error metadata
            error_meta = {
                "file_id": file_id,
                "bucket": bucket_name,
                "status": "failed",
                "error_detail": str(e)[:500],
            }

            try:
                if is_knowledge_doc:
                    supabase.table("knowledge_documents").update({
                        "metadata": error_meta
                    }).eq("id", doc_id).execute()
                else:
                    supabase.table("files").update({
                        "embedding_status": "failed",
                        "embedding_error": str(e)[:500],
                        "metadata": error_meta
                    }).eq("id", doc_id).execute()
            except Exception:
                pass

            # Error notification
            notification = {
                "type": "document_upload_failed" if is_knowledge_doc else "file_upload_failed",
                "organization_id": organization_id,
                "agent_id": agent_id,
                "doc_id": doc_id,
                "filename": filename,
                "status": "failed",
                "error": str(e)[:200],
                "table": table_name
            }
            try:
                redis_client.publish(f"ws_org_{organization_id}", json.dumps(notification))
            except Exception as pub_err:
                logger.error(f"⚠️ Publish failed: {pub_err}")
            
            # Cleanup temp file if still present
            temp_file_path = job.get("temp_file_path")
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    pass

            # Cleanup storage — knowledge documents only.
            # File manager files must NOT be deleted on embedding failure:
            # the file is the user's asset and must remain accessible even
            # if background processing fails.  Embedding status is already
            # marked "failed" in the DB above; the file stays in storage.
            if is_knowledge_doc:
                try:
                    supabase.storage.from_(bucket_name).remove([file_id])
                except Exception:
                    pass


# ============================================
# 3. SINGLETONS
# ============================================

_queue_service: Optional[DocumentQueueService] = None
_worker: Optional[DocumentProcessingWorker] = None

def get_document_queue_service() -> DocumentQueueService:
    global _queue_service
    if _queue_service is None:
        _queue_service = DocumentQueueService()
    return _queue_service

def get_document_worker() -> DocumentProcessingWorker:
    global _worker
    if _worker is None:
        _worker = DocumentProcessingWorker()
    return _worker