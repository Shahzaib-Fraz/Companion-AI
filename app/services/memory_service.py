"""
Memory Service - Issue #29 Fixed
Proper exception handling for index creation
"""

import asyncio
import logging
import uuid
from typing import List, Optional

from app.core.config import settings
from app.services.embedding_service import DIMENSION
from app.services.dependency_health import dependency_health

logger = logging.getLogger(__name__)

COLLECTION = "ai_companion_memories"
_NAMESPACE = uuid.UUID("6f1a2b1e-6f0e-4f2a-9a3c-3f5a1d9b7c11")


class MemoryService:
    def __init__(self):
        self.collection_name = COLLECTION
        self.dimension = DIMENSION
        self._ready = False
        self.client = None
        try:
            from qdrant_client import QdrantClient
            self.client = QdrantClient(
                url=settings.QDRANT_URL,
                api_key=settings.QDRANT_API_KEY,
                timeout=15,
            )
        except Exception:
            logger.exception("Qdrant client could not be created; premium memory disabled")
            dependency_health.set_status("qdrant", "down", "Client initialization failed")

    # ------------------------------------------------------------- bootstrap
    def _ensure_collection(self):
        if self._ready or not self.client:
            return
        from qdrant_client.models import Distance, VectorParams

        exists = False
        try:
            checker = getattr(self.client, "collection_exists", None)
            if callable(checker):
                exists = checker(self.collection_name)
            else:
                names = {c.name for c in self.client.get_collections().collections}
                exists = self.collection_name in names
        except Exception:
            logger.exception("Could not list Qdrant collections")
            dependency_health.set_status("qdrant", "down", "Failed to list collections")

        if not exists:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.dimension, distance=Distance.COSINE),
            )
            logger.info(
                "Created Qdrant collection %s (dim=%d)", self.collection_name, self.dimension
            )

        # REQUIRED, not optional. Qdrant Cloud runs in strict mode, which
        # refuses to filter on an unindexed payload field:
        #   400 Bad request: Index required but not found for "user_id"
        # Upserts do not filter, so without this writes succeed and only
        # retrieval fails - premium memory looks like it works and silently
        # never recalls anything. Runs for existing collections too; Qdrant
        # indexes the points already stored.
        self._ensure_user_id_index()

        dependency_health.set_status("qdrant", "up")
        self._ready = True

    def _ensure_user_id_index(self):
        """
        Idempotent: creating an index that already exists is a no-op.
        
        ✅ FIXED #29: Properly distinguish between "already exists" (OK)
        and real errors (Connection refused, Auth failed, etc.)
        """
        from qdrant_client.models import PayloadSchemaType

        for kwargs in (
            {"wait": True},   # newer clients
            {},               # older clients without `wait`
        ):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="user_id",
                    field_schema=PayloadSchemaType.INTEGER,
                    **kwargs,
                )
                logger.info("✅ Payload index on user_id created successfully")
                dependency_health.set_status("qdrant", "up")
                return
            except TypeError:
                continue      # unsupported kwarg, try the simpler call
            except Exception as exc:
                error_str = str(exc).lower()
                
                # ✅ FIXED #29: Only suppress "already exists" errors
                # Check if error message contains both "already" AND "exists"
                if "already" in error_str and "exists" in error_str:
                    logger.info("✅ Payload index on user_id already exists (OK)")
                    dependency_health.set_status("qdrant", "up")
                    return
                
                # ❌ Any other error is a REAL FAILURE, not just idempotency
                error_msg = f"{type(exc).__name__}: {str(exc)}"
                dependency_health.set_status("qdrant", "down", error_msg)
                logger.error(f"❌ Failed to create/verify user_id index: {error_msg}")
                return

    @staticmethod
    def _point_id(user_id: int, content: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{user_id}::{content}"))

    # ------------------------------------------------------------------ write
    def _store_sync(self, user_id: int, content: str, vector: List[float]) -> bool:
        from qdrant_client.models import PointStruct

        self._ensure_collection()
        point = PointStruct(
            id=self._point_id(user_id, content),
            vector=vector,
            payload={"user_id": user_id, "content": content},
        )
        self.client.upsert(collection_name=self.collection_name, points=[point])
        
        dependency_health.set_status("qdrant", "up")
        return True

    async def store_memory(self, user_id: int, content: str, vector: Optional[List[float]]) -> bool:
        if not self.client or not vector or not (content or "").strip():
            return False
        if len(vector) != self.dimension:
            logger.error(
                "Refusing to store a %d-dim vector in a %d-dim collection",
                len(vector), self.dimension,
            )
            return False
        try:
            result = await asyncio.to_thread(self._store_sync, user_id, content, vector)
            if result:
                dependency_health.set_status("qdrant", "up")
            return result
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            dependency_health.set_status("qdrant", "down", error_msg)
            logger.error(f"❌ Qdrant upsert failed for user {user_id}: {error_msg}")
            return False

    # ------------------------------------------------------------------- read
    def _search_sync(self, user_id: int, vector: List[float], top_k: int) -> List[str]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        self._ensure_collection()
        flt = Filter(must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))])

        # ✅ FIXED Issue #23: Apply similarity threshold from config
        threshold = settings.MEMORY_SIMILARITY_THRESHOLD

        try:
            query = getattr(self.client, "query_points", None)
            if callable(query):
                result = query(
                    collection_name=self.collection_name,
                    query=vector,
                    query_filter=flt,
                    limit=top_k,
                    score_threshold=threshold,  # ✅ Only retrieve >= threshold
                    with_payload=True,
                )
                points = getattr(result, "points", result)
            else:                                    # older qdrant-client
                points = self.client.search(
                    collection_name=self.collection_name,
                    query_vector=vector,
                    query_filter=flt,
                    limit=top_k,
                    score_threshold=threshold,  # ✅ Only retrieve >= threshold
                    with_payload=True,
                )

            out = []
            for p in points or []:
                payload = getattr(p, "payload", None) or {}
                content = payload.get("content")
                if content:
                    out.append(content)

            dependency_health.set_status("qdrant", "up")

            logger.info(
                f"🔍 Memory search retrieved {len(out)} results",
                extra={"user_id": user_id, "threshold": threshold, "requested": top_k}
            )

            return out
        
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            dependency_health.set_status("qdrant", "down", error_msg)
            logger.error(f"❌ Qdrant search failed for user {user_id}: {error_msg}")
            return []

    async def search(self, user_id: int, vector: Optional[List[float]], top_k: int = 5) -> List[str]:
        if not self.client or not vector:
            return []
        
        if dependency_health.get_status("qdrant") == "down":
            logger.warning(
                f"⚠️  Qdrant is down, memory unavailable",
                extra={"user_id": user_id}
            )
            return []
        
        try:
            return await asyncio.to_thread(self._search_sync, user_id, vector, top_k)
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            dependency_health.set_status("qdrant", "down", error_msg)
            logger.error(f"❌ Qdrant search failed for user {user_id}: {error_msg}")
            return []


memory_service = MemoryService()