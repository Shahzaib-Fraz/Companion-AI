
import asyncio
import logging
import threading
from typing import List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Fully-qualified HF repo id. This is the model you already have cached, so
# dropping this file in changes nothing about your stored vectors - no Qdrant
# wipe needed.
#
# IT IS ENGLISH ONLY. Your onboarding accepts ur, hi, ar, id, bn, and for those
# users MiniLM maps text to near-arbitrary points in the vector space, so
# Qdrant returns its nearest neighbours anyway and the "THINGS YOU REMEMBER"
# block fills with irrelevant memories. When you are ready to fix that, put
# this in .env - same 384 dims, so no collection resize:
#
#   EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
#
# and DELETE the Qdrant collection when you do. Same dimension is not the same
# vector space; vectors written by this model are not comparable to the
# multilingual one, so old memories would become noise. The collection is
# recreated automatically, index included.
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

MODEL_NAME: str = getattr(settings, "EMBEDDING_MODEL", None) or DEFAULT_MODEL

# Output size of each model. The two MiniLM options are both 384, which is why
# swapping between them needs no Qdrant resize — only a wipe.
KNOWN_DIMENSIONS = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "intfloat/multilingual-e5-base": 768,
    "BAAI/bge-m3": 1024,
}

DIMENSION: int = int(
    getattr(settings, "EMBEDDING_DIMENSION", None)
    or KNOWN_DIMENSIONS.get(MODEL_NAME, 384)
)


class EmbeddingService:
    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self.model_name = MODEL_NAME
        self.dimension = DIMENSION

    # ------------------------------------------------------------------ model
    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer  # local import
                logger.info("Loading embedding model %s from Hugging Face", self.model_name)
                model = SentenceTransformer(self.model_name)

                actual = model.get_sentence_embedding_dimension()
                if actual != self.dimension:
                    # Loud, because memory_service already built (or will build)
                    # the Qdrant collection at self.dimension and every write
                    # will be rejected until this is reconciled.
                    logger.critical(
                        "Embedding dimension mismatch: %s produces %d dims but the "
                        "app is configured for %d. Set EMBEDDING_DIMENSION=%d and "
                        "recreate the Qdrant collection.",
                        self.model_name, actual, self.dimension, actual,
                    )
                self._model = model
                logger.info("Embedding model ready (%d dims)", actual)
        return self._model

    # ----------------------------------------------------------------- encode
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
        """
        One pass for several strings. Use this when you have both sides of a
        turn to store — noticeably faster than two embed() calls on CPU.
        Returns None in the slots where the input was blank, so the output
        lines up positionally with the input.
        """
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
        """Call from a FastAPI startup hook so the first premium chat is not slow."""
        try:
            self._load()
        except Exception:
            logger.exception("Embedding model warmup failed")


embedding_service = EmbeddingService()