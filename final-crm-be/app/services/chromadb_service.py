"""
ChromaDB Service
Handles all ChromaDB operations including collection management and querying.
Supports organization-specific collections for multi-tenant isolation.
"""
import uuid
import logging
from typing import List, Dict, Any, Optional, Set
import chromadb
from chromadb.utils import embedding_functions
from app.config import settings

logger = logging.getLogger(__name__)


class ChromaDBService:
    """
    Service for managing ChromaDB operations.

    Multi-tenant Support:
    - Each organization has its own ChromaDB collection
    - Collection name format: org_{organization_id}
    - Documents are isolated by organization
    """

    def __init__(self):
        """Initialize ChromaDB client (without creating collection)"""
        # Initialize client based on configuration
        if settings.is_chromadb_cloud_configured:
            # Use Chroma Cloud
            logger.info(f"🌐 Connecting to Chroma Cloud (Tenant: {settings.CHROMADB_CLOUD_TENANT}, Database: {settings.CHROMADB_CLOUD_DATABASE})")
            self.client = chromadb.CloudClient(
                tenant=settings.CHROMADB_CLOUD_TENANT,
                database=settings.CHROMADB_CLOUD_DATABASE,
                api_key=settings.CHROMADB_CLOUD_API_KEY
            )
        else:
            # Use self-hosted ChromaDB (legacy)
            logger.info(f"🏠 Connecting to self-hosted ChromaDB ({settings.CHROMADB_HOST}:{settings.CHROMADB_PORT})")
            self.client = chromadb.HttpClient(
                host=settings.CHROMADB_HOST,
                port=settings.CHROMADB_PORT
            )

        # OpenAI Embedding Function with Custom Proxy
        self.embedding_function = embedding_functions.OpenAIEmbeddingFunction(
            api_key=settings.OPENAI_API_KEY,
            model_name=settings.OPENAI_MODEL,
            # api_base=settings.OPENAI_BASE_URL
            api_base=settings.PROXY_BASE_URL
        )

    def _get_collection_name(self, organization_id: str) -> str:
        """
        Get collection name for organization.

        Args:
            organization_id: Organization UUID

        Returns:
            Collection name format: org_{organization_id}
        """
        return f"org_{organization_id}"

    def create_organization_collection(self, organization_id: str) -> Dict[str, Any]:
        """
        Create a new ChromaDB collection for an organization.

        Called automatically when a new organization is created.

        Args:
            organization_id: Organization UUID

        Returns:
            Dict with collection info
        """
        collection_name = self._get_collection_name(organization_id)

        try:
            collection = self.client.get_or_create_collection(
                name=collection_name,
                embedding_function=self.embedding_function,
                metadata={
                    "hnsw:space": "cosine",
                    "organization_id": organization_id,
                    "created_by": "system"
                }
            )

            logger.info(f"✅ Created ChromaDB collection: {collection_name}")

            return {
                "collection_name": collection_name,
                "organization_id": organization_id,
                "status": "created"
            }

        except Exception as e:
            logger.error(f"Failed to create collection {collection_name}: {e}")
            raise RuntimeError(f"Failed to create ChromaDB collection: {str(e)}")

    def get_organization_collection(self, organization_id: str):
        """
        Get collection for an organization.

        Args:
            organization_id: Organization UUID

        Returns:
            ChromaDB collection object

        Raises:
            ValueError: If collection doesn't exist
        """
        collection_name = self._get_collection_name(organization_id)

        try:
            collection = self.client.get_collection(
                name=collection_name,
                embedding_function=self.embedding_function
            )
            return collection

        except Exception as e:
            logger.error(f"Collection {collection_name} not found: {e}")
            raise ValueError(f"Organization collection not found. Please ensure organization is properly set up.")

    def get_or_create_organization_collection(self, organization_id: str):
        """
        Get or create collection for an organization (findOrCreate pattern).

        This method will automatically create the collection if it doesn't exist.
        Used by embedding and query operations to ensure collection availability.

        Args:
            organization_id: Organization UUID

        Returns:
            ChromaDB collection object
        """
        collection_name = self._get_collection_name(organization_id)

        try:
            # Try to get existing collection first
            collection = self.client.get_collection(
                name=collection_name,
                embedding_function=self.embedding_function
            )
            logger.debug(f"Found existing collection: {collection_name}")
            return collection

        except Exception as e:
            # Collection doesn't exist, create it
            logger.info(f"Collection {collection_name} not found, creating it automatically...")

            try:
                collection = self.client.get_or_create_collection(
                    name=collection_name,
                    embedding_function=self.embedding_function,
                    metadata={
                        "hnsw:space": "cosine",
                        "organization_id": organization_id,
                        "created_by": "auto_created",
                        "created_reason": "embedding_process"
                    }
                )

                logger.info(f"✅ Auto-created ChromaDB collection: {collection_name}")
                return collection

            except Exception as create_error:
                logger.error(f"Failed to auto-create collection {collection_name}: {create_error}")
                raise RuntimeError(f"Failed to create ChromaDB collection: {str(create_error)}")

    def add_chunks(
        self,
        chunks: List[str],
        filename: str,
        organization_id: str,
        file_id: Optional[str] = None,
        batch_size: int = 256,
        email: Optional[str] = None
    ) -> str:
        """
        Add text chunks to organization-specific ChromaDB collection.

        Auto-creates collection if it doesn't exist (findOrCreate pattern).

        Args:
            chunks: List of text chunks
            filename: Original filename
            organization_id: Organization UUID (REQUIRED)
            file_id: Unique file identifier
            batch_size: Batch size for uploads
            email: User email

        Returns:
            File ID used for the chunks

        Raises:
            ValueError: If organization_id is missing
            RuntimeError: If collection creation fails
        """
        if not organization_id:
            raise ValueError("organization_id is required for adding documents")

        # Get or create organization-specific collection (findOrCreate)
        collection = self.get_or_create_organization_collection(organization_id)

        file_id = file_id or uuid.uuid4().hex

        # Prepare IDs and metadata for each chunk
        all_ids = [f"{file_id}-{i}" for i in range(len(chunks))]
        all_metas = [
            {
                "file_id": file_id,
                "filename": filename,
                "chunk_index": i,
                "email": email,
                "organization_id": organization_id,
                "is_trashed": False  # ⭐ Default: file is not trashed
            }
            for i in range(len(chunks))
        ]

        # Add to ChromaDB in batches
        for start in range(0, len(chunks), batch_size):
            end = start + batch_size
            batch_docs = chunks[start:end]
            batch_ids = all_ids[start:end]
            batch_metas = all_metas[start:end]
            collection.add(
                documents=batch_docs,
                ids=batch_ids,
                metadatas=batch_metas
            )

        logger.info(f"Added {len(chunks)} chunks to collection org_{organization_id}")

        return file_id

    def query_documents(
        self,
        query: str,
        organization_id: str,
        email: str,
        top_k: int = 5,
        where: Optional[Dict[str, Any]] = None,
        include_distances: bool = True,
        include_embeddings: bool = False
    ) -> Dict[str, Any]:
        """
        Query documents from organization-specific ChromaDB collection.

        Auto-creates collection if it doesn't exist (returns empty results for new collections).

        Args:
            query: Search query
            organization_id: Organization UUID (REQUIRED)
            email: User email for filtering
            top_k: Number of results to return
            where: Additional filter conditions
            include_distances: Include distance scores
            include_embeddings: Include embeddings

        Returns:
            Query results (empty if collection is new)

        Raises:
            ValueError: If organization_id is missing
            RuntimeError: If collection creation fails
        """
        if not organization_id:
            raise ValueError("organization_id is required for querying documents")

        collection = self.get_or_create_organization_collection(organization_id)

        filters_list = [
            {"organization_id": {"$eq": organization_id}},
            {"is_trashed": {"$eq": False}}
        ]

        if where:
            filters_list.append(where)

        final_where = { "$and": filters_list }

        logger.info(f"🔎 Final Query filter: {final_where}, organization_id: {organization_id}")

        include = ["metadatas", "documents"]
        if include_distances:
            include.append("distances")
        if include_embeddings:
            include.append("embeddings")

        q_emb = self.embedding_function([query])

        results = collection.query(
            query_embeddings=q_emb,
            n_results=top_k,
            where=final_where, 
            include=include,
        )

        num_results = len(results.get('documents', [[]])[0])
        logger.info(f"✅ Queried org_{organization_id}: found {num_results} results")

        return results

    def delete_documents(
        self,
        organization_id: str,
        email: str,
        filename: str
    ) -> bool:
        """
        Delete documents by email and filename from organization collection.

        Auto-creates collection if it doesn't exist (no-op for new collections).

        Args:
            organization_id: Organization UUID (REQUIRED)
            email: User email
            filename: Filename to delete

        Returns:
            True if successful

        Raises:
            ValueError: If organization_id is missing
            RuntimeError: If collection creation fails
        """
        if not organization_id:
            raise ValueError("organization_id is required for deleting documents")

        # Get or create organization-specific collection (findOrCreate)
        collection = self.get_or_create_organization_collection(organization_id)

        where = {
            "$and": [
                {"email": {"$eq": email}},
                {"filename": {"$eq": filename}}
            ]
        }
        collection.delete(where=where)

        logger.info(f"Deleted documents from org_{organization_id}: {filename}")

        return True

    def update_document_metadata_by_file_id(
        self,
        organization_id: str,
        file_id: str,
        metadata_updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update metadata for all document chunks of a specific file.

        This is used to update is_trashed flag when moving files to/from trash.

        Args:
            organization_id: Organization UUID (REQUIRED)
            file_id: File UUID to update metadata for
            metadata_updates: Dict of metadata fields to update (e.g., {"is_trashed": True})

        Returns:
            Dict with update stats

        Raises:
            ValueError: If organization_id or file_id is missing
            RuntimeError: If update fails
        """
        if not organization_id:
            raise ValueError("organization_id is required for updating documents")

        if not file_id:
            raise ValueError("file_id is required for updating documents")

        try:
            # Get or create organization-specific collection
            collection = self.get_or_create_organization_collection(organization_id)

            # Get all document IDs for this file
            docs = collection.get(
                where={"file_id": {"$eq": file_id}},
                include=["metadatas"]
            )

            doc_ids = docs.get("ids", [])
            existing_metas = docs.get("metadatas", [])

            if not doc_ids:
                logger.warning(f"⚠️  No documents found for file_id: {file_id}")
                return {
                    "organization_id": organization_id,
                    "file_id": file_id,
                    "updated_chunks": 0,
                    "status": "no_documents"
                }

            # Update metadata for each chunk
            updated_metas = []
            for meta in existing_metas:
                if meta:
                    # Merge existing metadata with updates
                    updated_meta = {**meta, **metadata_updates}
                    updated_metas.append(updated_meta)
                else:
                    updated_metas.append(metadata_updates)

            # Update in ChromaDB
            collection.update(
                ids=doc_ids,
                metadatas=updated_metas
            )

            logger.info(f"✅ Updated metadata for {len(doc_ids)} chunks (file_id: {file_id}, updates: {metadata_updates})")

            return {
                "organization_id": organization_id,
                "file_id": file_id,
                "updated_chunks": len(doc_ids),
                "metadata_updates": metadata_updates,
                "status": "updated"
            }

        except Exception as e:
            logger.error(f"Failed to update document metadata: {e}")
            raise RuntimeError(f"Failed to update ChromaDB metadata: {str(e)}")

    def delete_documents_by_file_id(
        self,
        organization_id: str,
        file_id: str
    ) -> Dict[str, Any]:
        """
        Delete documents by file_id metadata from organization collection.

        This is the RECOMMENDED way to delete embeddings as it's more reliable
        than using filename (which can have duplicates or be renamed).

        Args:
            organization_id: Organization UUID (REQUIRED)
            file_id: File UUID to delete embeddings for

        Returns:
            Dict with deletion stats

        Raises:
            ValueError: If organization_id or file_id is missing
            RuntimeError: If collection creation fails
        """
        if not organization_id:
            raise ValueError("organization_id is required for deleting documents")

        if not file_id:
            raise ValueError("file_id is required for deleting documents")

        try:
            # Get or create organization-specific collection (findOrCreate)
            collection = self.get_or_create_organization_collection(organization_id)

            # First, get count of documents to be deleted
            try:
                docs_before = collection.get(
                    where={"file_id": {"$eq": file_id}},
                    include=["metadatas"]
                )
                count_before = len(docs_before.get("ids", []))
            except:
                count_before = 0

            # Delete by file_id metadata
            where = {"file_id": {"$eq": file_id}}
            collection.delete(where=where)

            logger.info(f"✅ Deleted {count_before} document chunks from org_{organization_id} for file_id: {file_id}")

            return {
                "organization_id": organization_id,
                "file_id": file_id,
                "deleted_chunks": count_before,
                "status": "deleted"
            }

        except Exception as e:
            logger.error(f"Failed to delete documents by file_id: {e}")
            raise RuntimeError(f"Failed to delete ChromaDB documents: {str(e)}")

    def get_collection_items(
        self,
        name: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        include_embeddings: bool = False,
        source: Optional[str] = None,
        contains: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get items from a collection

        Args:
            name: Collection name
            limit: Maximum number of results
            offset: Offset for pagination
            include_embeddings: Include embeddings
            source: Filter by source metadata
            contains: Filter by document content

        Returns:
            Collection items formatted as dict
        """
        col = self.client.get_collection(name)
        include = ["documents", "metadatas"]
        if include_embeddings:
            include.append("embeddings")

        where = {"source": {"$eq": source}} if source else None
        where_doc = {"$contains": contains} if contains else None

        raw = col.get(
            include=include,
            where=where,
            where_document=where_doc,
            limit=limit,
            offset=offset,
        )

        return self._format_chroma_get(raw, name)

    @staticmethod
    def _format_chroma_get(raw: dict, collection_name: str) -> Dict[str, Any]:
        """Format ChromaDB get results"""
        ids = raw.get("ids", []) or []
        docs = raw.get("documents", []) or []
        metas = raw.get("metadatas", []) or []

        n = min(len(ids), len(docs) if docs else len(ids), len(metas) if metas else len(ids))
        items = []
        for i in range(n):
            items.append({
                "id": ids[i],
                "document": docs[i] if i < len(docs) else None,
                "metadata": metas[i] if i < len(metas) else None,
            })

        return {
            "collection": collection_name,
            "count": len(ids),
            "items": items,
        }

    @staticmethod
    def extract_unique_file_ids(metadatas: List[Dict[str, Any]]) -> Set[str]:
        """
        Extract unique file IDs from metadata

        Args:
            metadatas: List of metadata dicts

        Returns:
            Set of unique file IDs
        """
        file_ids = set()

        if not metadatas:
            return file_ids

        for metadata in metadatas:
            if isinstance(metadata, dict):
                # Try various possible keys for file_id
                possible_keys = ["file_id", "source", "filename", "file_name", "document_id", "doc_id"]

                for key in possible_keys:
                    value = metadata.get(key)
                    if value:
                        file_ids.add(str(value))
                        break

        return file_ids
