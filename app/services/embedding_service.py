import asyncio
import logging
import threading
from typing import List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# ✅ FIXED Issue #25: Now properly uses settings (declared in config)
MODEL_NAME: str = settings.EMBEDDING_MODEL
DIMENSION: int = settings.EMBEDDING_DIMENSION

KNOWN_DIMENSIONS = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "intfloat/multilingual-e5-base": 768,
    "BAAI/bge-m3": 1024,
}


class EmbeddingServiceError(Exception):
    pass


class EmbeddingService:
    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self.model_name = MODEL_NAME
        self.dimension = DIMENSION

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading embedding model %s", self.model_name)
                model = SentenceTransformer(self.model_name)

                actual = model.get_sentence_embedding_dimension()
                
                # ✅ FIXED Issue #26: Fail hard if dimension mismatch (don't continue with broken state)
                if actual != self.dimension:
                    error_msg = (
                        f"❌ EMBEDDING DIMENSION MISMATCH:\n"
                        f"   Model: {self.model_name}\n"
                        f"   Expected: {self.dimension}\n"
                        f"   Actual: {actual}\n"
                        f"   Fix: Set EMBEDDING_DIMENSION={actual} in .env "
                        f"or use a model with {self.dimension}-dim output"
                    )
                    logger.critical(error_msg)
                    raise EmbeddingServiceError(error_msg)
                
                self._model = model
                logger.info("✅ Embedding model ready (%d dims)", actual)
        return self._model

    def _encode(self, texts: List[str]) -> List[List[float]]:
        model = self._load()
        vectors = model.encode(texts, normalize_embeddings=True, batch_size=8)
        return [v.tolist() for v in vectors]

    async def embed(self, text: str) -> Optional[List[float]]:
        text = (text or "").strip()
        if not text:
            return None
        vectors = await asyncio.to_thread(self._encode, [text])
        return vectors[0] if vectors else None

    async def embed_many(self, texts: List[str]) -> List[Optional[List[float]]]:
        texts = texts or []
        cleaned = [(i, t.strip()) for i, t in enumerate(texts) if (t or "").strip()]
        if not cleaned:
            return [None] * len(texts)

        vectors = await asyncio.to_thread(self._encode, [t for _, t in cleaned])

        out: List[Optional[List[float]]] = [None] * len(texts)
        for (i, _), vector in zip(cleaned, vectors):
            out[i] = vector
        return out

    def warmup(self):
        try:
            self._load()
        except EmbeddingServiceError:
            logger.critical("❌ Embedding model failed to load - check EMBEDDING_DIMENSION")
            raise
        except Exception:
            logger.exception("❌ Embedding model warmup failed")
            raise


embedding_service = EmbeddingService()