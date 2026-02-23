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
import redis
from typing import Optional
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
    ) -> bool:
        """
        Push a processing job to Redis. Returns True if queued successfully.
        
        NOTE: We do NOT pass file_content through Redis.
        The worker downloads the file from Supabase Storage using bucket_name + file_id.
        This keeps Redis lightweight (just job descriptors, not file bytes).
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
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
            
            self.client.lpush(self.QUEUE_KEY, json.dumps(job))
            queue_len = self.client.llen(self.QUEUE_KEY)
            
            logger.info(
                f"📬 Queued document processing: {filename} "
                f"(doc_id={doc_id}, queue_depth={queue_len})"
            )
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
    Pulls jobs from Redis and processes them in its own thread.
    Never touches the FastAPI/uvicorn event loop.
    """
    
    QUEUE_KEY = DocumentQueueService.QUEUE_KEY
    PROCESSING_KEY = DocumentQueueService.PROCESSING_KEY
    POLL_INTERVAL = 2  # seconds between Redis polls when queue is empty
    
    def __init__(self):
        self._redis: Optional[redis.Redis] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
    
    @property
    def client(self) -> redis.Redis:
        if self._redis is None:
            self._redis = _get_redis_client()
        return self._redis

    def start_in_thread(self):
        """Start the worker loop in a daemon thread (called from FastAPI lifespan)."""
        if self._thread and self._thread.is_alive():
            logger.warning("⚠️ Document worker thread already running")
            return
        
        self._running = True
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="doc-processing-worker",
            daemon=True,  # Dies when main process exits
        )
        self._thread.start()
        logger.info("🚀 Document processing worker started (background thread)")
    
    def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=30)
            logger.info("🛑 Document processing worker stopped")
    
    def _worker_loop(self):
        """
        Main worker loop. Runs in a separate thread.
        Uses BRPOP for efficient blocking wait (no busy-polling).
        """
        logger.info("👷 Document worker loop started, waiting for jobs...")
        
        while self._running:
            try:
                # BRPOP blocks for up to POLL_INTERVAL seconds, then returns None
                result = self.client.brpop(self.QUEUE_KEY, timeout=self.POLL_INTERVAL)
                
                if result is None:
                    continue  # Timeout, no jobs — loop back
                
                _, job_json = result
                job = json.loads(job_json)
                
                logger.info(f"⚙️ [DocWorker] Picked up job: {job['filename']} (doc_id={job['doc_id']})")
                
                # Track active job for monitoring
                self.client.set(
                    self.PROCESSING_KEY, 
                    json.dumps({**job, "started_at": datetime.now(timezone.utc).isoformat()}),
                    ex=3600  # Auto-expire after 1 hour (safety net)
                )
                
                # Process the document (heavy work — in THIS thread, not event loop)
                self._process_job(job)
                
                # Clear active tracking
                self.client.delete(self.PROCESSING_KEY)
                
            except Exception as e:
                logger.error(f"❌ [DocWorker] Unexpected error in loop: {e}", exc_info=True)
                time.sleep(5)  # Back off on errors
    
    def _process_job(self, job: dict):
        """
        Process a single document job. Creates its own event loop 
        since we're in a worker thread (not the uvicorn thread).
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            loop.run_until_complete(self._process_job_async(job))
        except Exception as e:
            logger.error(f"❌ [DocWorker] Job failed for {job.get('filename')}: {e}", exc_info=True)
        finally:
            loop.close()
    
    async def _process_job_async(self, job: dict):
        """
        Async processing logic — same steps as the old process_document_background,
        but now runs in a dedicated thread with its own event loop.
        """
        from app.services.document_processor_v2 import DocumentProcessorV2
        from app.services.crm_chroma_service_v2 import get_crm_chroma_service_v2
        from app.utils.chunkingv2 import split_into_chunks_with_metadata
        from supabase import create_client
        
        doc_id = job["doc_id"]
        agent_id = job["agent_id"]
        agent_name = job["agent_name"]
        organization_id = job["organization_id"]
        file_id = job["file_id"]
        filename = job["filename"]
        bucket_name = job["bucket_name"]
        
        # Fresh Supabase client for this thread
        key = settings.SUPABASE_SERVICE_KEY or settings.SUPABASE_KEY
        supabase = create_client(settings.SUPABASE_URL, key)
        doc_processor = DocumentProcessorV2()
        
        try:
            logger.info(f"⚙️ [DocWorker] Processing started for {filename}")
            
            # =============================================
            # STEP 0: DOWNLOAD FILE FROM SUPABASE STORAGE
            # =============================================
            try:
                file_content = supabase.storage.from_(bucket_name).download(file_id)
            except Exception as dl_err:
                raise RuntimeError(f"Failed to download file from storage: {dl_err}")
            
            if not file_content:
                raise RuntimeError("Downloaded file is empty")
            
            logger.info(f"📥 [DocWorker] Downloaded {filename} ({len(file_content) // 1024}KB)")
            
            # =============================================
            # STEP 1: EXTRACT & QUALITY CHECK
            # (synchronous — fine because we're in our own thread)
            # =============================================
            clean_text, metrics = doc_processor.process_document(
                file_content, filename, bucket_name, organization_id, file_id
            )
            
            if not clean_text:
                raise ValueError("No text could be extracted from the document.")
            
            doc_processor.validate_quality(clean_text, metrics, filename)
            
            # =============================================
            # STEP 2: DUPLICATE CHECK
            # =============================================
            existing_doc = await doc_processor.check_duplicate(
                content_hash=metrics.content_hash,
                agent_id=agent_id,
                supabase=supabase,
            )
            if existing_doc:
                raise ValueError(f"Duplicate content. Already uploaded as '{existing_doc}'.")
            
            # =============================================
            # STEP 3: CHUNKING
            # =============================================
            chunks, chunk_metas = split_into_chunks_with_metadata(
                text=clean_text, filename=filename, file_id=file_id,
                agent_id=agent_id, agent_name=agent_name, 
                organization_id=organization_id,
                size=512, overlap=100
            )
            if not chunks:
                raise ValueError("No valid chunks generated from document.")
            
            # =============================================
            # STEP 4: EMBEDDING + CHROMADB STORAGE
            # =============================================
            chroma_service = get_crm_chroma_service_v2()
            success = await chroma_service.add_documents(
                agent_id=agent_id, texts=chunks, metadatas=chunk_metas, 
                organization_id=organization_id
            )
            if not success:
                raise Exception("ChromaDB failed to save vectors.")
            
            # =============================================
            # STEP 5: MARK AS COMPLETED IN POSTGRES
            # =============================================
            updated_meta = {
                "file_id": file_id,
                "bucket": bucket_name,
                "processor_version": "v2",
                "status": "completed",
                "chunks": len(chunks),
                "content_hash": metrics.content_hash,
                "word_count": metrics.word_count,
                "token_count": metrics.token_count,
                "has_tables": metrics.has_tables,
            }
            supabase.table("knowledge_documents")\
                .update({"metadata": updated_meta})\
                .eq("id", doc_id)\
                .execute()
            
            logger.info(f"✅ [DocWorker] Done: {filename} → {len(chunks)} chunks embedded")

            try:
                notification = {
                    "type": "document_upload_completed",
                    "organization_id": organization_id,
                    "agent_id": agent_id,
                    "doc_id": doc_id,
                    "filename": filename,
                    "status": "completed"
                }
                channel = f"ws_org_{organization_id}"
                self.client.publish(channel, json.dumps(notification))
                logger.info(f"📢 Notification published to {channel}")
            except Exception as pub_err:
                logger.error(f"⚠️ Failed to publish success notification: {pub_err}")
        
        except Exception as e:
            logger.error(f"❌ [DocWorker] Failed: {filename} — {e}")
            
            # Update DB state to failed
            error_meta = {
                "file_id": file_id,
                "bucket": bucket_name,
                "processor_version": "v2",
                "status": "failed",
                "error_detail": str(e)[:500],
            }
            try:
                supabase.table("knowledge_documents")\
                    .update({"metadata": error_meta})\
                    .eq("id", doc_id)\
                    .execute()
            except Exception:
                pass
            
            # 📢 NEW: BROADCAST FAILURE VIA REDIS PUB/SUB
            try:
                notification = {
                    "type": "document_upload_failed",
                    "organization_id": organization_id,
                    "agent_id": agent_id,
                    "doc_id": doc_id,
                    "filename": filename,
                    "status": "failed",
                    "error": str(e)[:200]
                }
                channel = f"ws_org_{organization_id}"
                self.client.publish(channel, json.dumps(notification))
            except Exception as pub_err:
                logger.error(f"⚠️ Failed to publish error notification: {pub_err}")
            
            # Cleanup orphaned storage file
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