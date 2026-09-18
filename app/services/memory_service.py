"""
Memory Service
P1-15 FIX: index setup failures now propagate (raise) instead of being
logged and swallowed, and _ensure_collection() no longer marks the
collection "up"/ready if either required index failed to build. Same for
a failed collection-list or collection-create call.
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
            return  # P1-15 FIX: don't fall through to marking "up" below

        if not exists:
            try:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=self.dimension, distance=Distance.COSINE),
                )
                logger.info(
                    "Created Qdrant collection %s (dim=%d)", self.collection_name, self.dimension
                )
            except Exception:
                logger.exception("Failed to create Qdrant collection")
                dependency_health.set_status("qdrant", "down", "Failed to create collection")
                return  # P1-15 FIX

        # P1-15 FIX: these two now RAISE on a real failure instead of
        # logging and returning None. _ensure_collection() relies on that
        # to avoid marking Qdrant "up" after a genuine index failure -
        # previously it always reached the unconditional "up" below
        # regardless of what these two did.
        try:
            self._ensure_user_id_index()
            self._ensure_type_index()
        except Exception:
            # Specifics already logged with set_status("down", ...) inside
            # the helpers below - nothing more to do here except bail out
            # without marking ready.
            return

        dependency_health.set_status("qdrant", "up")
        self._ready = True

    def _ensure_user_id_index(self):
        """
        Idempotent: creating an index that already exists is a no-op
        (treated as success, returns normally).

        P1-15 FIX: any OTHER error is now RAISED, not swallowed. The
        caller (_ensure_collection) depends on this propagating so it
        can't mark Qdrant "up" after a real index failure.
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

                # Only "already exists" is a success path.
                if "already" in error_str and "exists" in error_str:
                    logger.info("✅ Payload index on user_id already exists (OK)")
                    dependency_health.set_status("qdrant", "up")
                    return

                # Any other error is a REAL FAILURE - propagate it.
                error_msg = f"{type(exc).__name__}: {str(exc)}"
                dependency_health.set_status("qdrant", "down", error_msg)
                logger.error(f"❌ Failed to create/verify user_id index: {error_msg}")
                raise RuntimeError(f"user_id index setup failed: {error_msg}") from exc

    def _ensure_type_index(self):
        """
        Create index on 'type' field for filtering by memory type.
        Idempotent + fail-loud like _ensure_user_id_index above.
        """
        from qdrant_client.models import PayloadSchemaType

        for kwargs in (
            {"wait": True},
            {},
        ):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name="type",
                    field_schema=PayloadSchemaType.KEYWORD,
                    **kwargs,
                )
                logger.info("✅ Payload index on type created successfully")
                return
            except TypeError:
                continue
            except Exception as exc:
                error_str = str(exc).lower()

                if "already" in error_str and "exists" in error_str:
                    logger.info("✅ Payload index on type already exists (OK)")
                    return

                error_msg = f"{type(exc).__name__}: {str(exc)}"
                dependency_health.set_status("qdrant", "down", error_msg)
                logger.error(f"❌ Failed to create/verify type index: {error_msg}")
                raise RuntimeError(f"type index setup failed: {error_msg}") from exc

    @staticmethod
    def _point_id(user_id: int, content: str) -> str:
        return str(uuid.uuid5(_NAMESPACE, f"{user_id}::{content}"))

    # ------------------------------------------------------------------ write
    def _store_sync(
        self,
        user_id: int,
        content: str,
        vector: List[float],
        memory_type: str = "user_note"
    ) -> bool:
        from qdrant_client.models import PointStruct

        self._ensure_collection()
        point = PointStruct(
            id=self._point_id(user_id, content),
            vector=vector,
            payload={
                "user_id": user_id,
                "content": content,
                "type": memory_type,  # "summary" | "preference" | "user_note"
            },
        )
        self.client.upsert(collection_name=self.collection_name, points=[point])

        dependency_health.set_status("qdrant", "up")
        return True

    async def store_memory(
        self,
        user_id: int,
        content: str,
        vector: Optional[List[float]],
        memory_type: str = "user_note"
    ) -> bool:
        """Store a memory with type classification"""
        if not self.client or not vector or not (content or "").strip():
            return False
        if len(vector) != self.dimension:
            logger.error(
                "Refusing to store a %d-dim vector in a %d-dim collection",
                len(vector), self.dimension,
            )
            return False
        try:
            result = await asyncio.to_thread(
                self._store_sync, user_id, content, vector, memory_type
            )
            if result:
                dependency_health.set_status("qdrant", "up")
            return result
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            dependency_health.set_status("qdrant", "down", error_msg)
            logger.error(f"❌ Qdrant upsert failed for user {user_id}: {error_msg}")
            return False

    async def store_summary(
        self,
        user_id: int,
        summary_text: str,
        vector: List[float]
    ) -> bool:
        """Store rolling conversation summary (replaces previous)"""
        return await self.store_memory(user_id, summary_text, vector, memory_type="summary")

    async def store_preference(
        self,
        user_id: int,
        pref_text: str,
        vector: List[float]
    ) -> bool:
        """Store user preference (premium only)"""
        return await self.store_memory(user_id, pref_text, vector, memory_type="preference")

    # ------------------------------------------------------------------- read
    def _search_sync(
        self,
        user_id: int,
        vector: List[float],
        top_k: int,
        memory_type: Optional[str] = None
    ) -> List[str]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        self._ensure_collection()

        # Build filter: user_id (required) + type (optional)
        conditions = [FieldCondition(key="user_id", match=MatchValue(value=user_id))]

        if memory_type:
            conditions.append(FieldCondition(key="type", match=MatchValue(value=memory_type)))

        flt = Filter(must=conditions)
        threshold = settings.MEMORY_SIMILARITY_THRESHOLD

        try:
            query = getattr(self.client, "query_points", None)
            if callable(query):
                result = query(
                    collection_name=self.collection_name,
                    query=vector,
                    query_filter=flt,
                    limit=top_k,
                    score_threshold=threshold,
                    with_payload=True,
                )
                points = getattr(result, "points", result)
            else:
                points = self.client.search(
                    collection_name=self.collection_name,
                    query_vector=vector,
                    query_filter=flt,
                    limit=top_k,
                    score_threshold=threshold,
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
                extra={
                    "user_id": user_id,
                    "type": memory_type or "all",
                    "threshold": threshold,
                    "requested": top_k
                }
            )

            return out

        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            dependency_health.set_status("qdrant", "down", error_msg)
            logger.error(f"❌ Qdrant search failed for user {user_id}: {error_msg}")
            return []

    async def search(
        self,
        user_id: int,
        vector: Optional[List[float]],
        top_k: int = 5,
        memory_type: Optional[str] = None
    ) -> List[str]:
        """
        Search memories by similarity.
        memory_type: None (all), "summary", "preference", "user_note"
        """
        if not self.client or not vector:
            return []

        if dependency_health.get_status("qdrant") == "down":
            logger.warning(
                f"⚠️  Qdrant is down, memory unavailable",
                extra={"user_id": user_id}
            )
            return []

        try:
            return await asyncio.to_thread(
                self._search_sync, user_id, vector, top_k, memory_type
            )
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            dependency_health.set_status("qdrant", "down", error_msg)
            logger.error(f"❌ Memory search failed for user {user_id}: {error_msg}")
            return []

    def _get_latest_sync(self, user_id: int, memory_type: str) -> Optional[str]:
        """Get single most recent memory of a specific type"""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        self._ensure_collection()

        conditions = [
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            FieldCondition(key="type", match=MatchValue(value=memory_type))
        ]
        flt = Filter(must=conditions)

        try:
            query = getattr(self.client, "query_points", None)
            if callable(query):
                result = query(
                    collection_name=self.collection_name,
                    query_filter=flt,
                    limit=1,
                    with_payload=True,
                )
                points = getattr(result, "points", result)
            else:
                points = self.client.search(
                    collection_name=self.collection_name,
                    query_filter=flt,
                    limit=1,
                    with_payload=True,
                )

            if points:
                payload = getattr(points[0], "payload", None) or {}
                content = payload.get("content")
                if content:
                    logger.info(
                        f"✅ Retrieved latest {memory_type}",
                        extra={"user_id": user_id}
                    )
                    return content

            logger.info(
                f"⚠️  No {memory_type} found",
                extra={"user_id": user_id}
            )
            return None

        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            logger.error(f"❌ Failed to fetch latest {memory_type}: {error_msg}")
            return None

    async def get_latest_by_type(
        self,
        user_id: int,
        memory_type: str
    ) -> Optional[str]:
        """
        Fetch the most recent memory of a specific type.
        Used to get previous summary before generating new one.
        """
        if not self.client:
            return None

        try:
            return await asyncio.to_thread(
                self._get_latest_sync, user_id, memory_type
            )
        except Exception as e:
            logger.warning(
                f"Could not fetch previous {memory_type} for user {user_id}: {e}"
            )
            return None


memory_service = MemoryService()