import asyncio

import numpy as np
from sentence_transformers import SentenceTransformer

from server.config import settings

_model: SentenceTransformer | None = None

# One shared model, and its forward pass is NOT thread-safe. nomic-bert caches
# the rotary sin/cos tables ON THE MODULE and rebuilds them during forward, so
# two concurrent encodes race on that cache and one of them reads it mid-swap:
#
#   File "modeling_hf_nomic_bert.py", in apply_rotary_emb
#       sin[offset : offset + x.shape[1]],
#   TypeError: 'NoneType' object is not subscriptable
#
# Observed on prod 2026-07-25 with 8 concurrent writers: 2 of 8 returned 500.
# It reaches searches too — every search embeds its query — so any two agents
# working at once could trip it. Serialising encode is the honest fix: the
# alternative is a model instance per worker, which multiplies a ~270MB model
# to buy throughput this service does not need. Failure here is loud (500) and
# was never silent corruption, but a memory service that randomly refuses
# concurrent writes is still broken.
_encode_lock = asyncio.Lock()


async def init_client() -> None:
    global _model
    _model = await asyncio.to_thread(
        lambda: SentenceTransformer(
            settings.embed_model,
            trust_remote_code=True,
            revision=settings.embed_model_revision or None,
        )
    )


async def close_client() -> None:
    global _model
    _model = None


async def embed(text: str) -> np.ndarray:
    """Get embedding vector for a text string. Returns 768-dim numpy array."""
    if _model is None:
        raise RuntimeError("Embedding client not initialized")
    if not text or not text.strip():
        raise ValueError("Cannot embed empty text")
    async with _encode_lock:
        result = await asyncio.to_thread(_model.encode, text)
    return np.array(result, dtype=np.float32)


async def embed_batch(texts: list[str]) -> list[np.ndarray]:
    """Get embeddings for multiple texts in a single call."""
    if _model is None:
        raise RuntimeError("Embedding client not initialized")
    async with _encode_lock:
        results = await asyncio.to_thread(_model.encode, texts)
    return [np.array(v, dtype=np.float32) for v in results]


async def check_health() -> bool:
    """Check if the embedding model is loaded and ready."""
    return _model is not None
