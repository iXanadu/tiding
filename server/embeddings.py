import asyncio

import numpy as np
from sentence_transformers import SentenceTransformer

from server.config import settings

_model: SentenceTransformer | None = None


async def init_client() -> None:
    global _model
    _model = await asyncio.to_thread(
        SentenceTransformer, settings.embed_model, trust_remote_code=True
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
    result = await asyncio.to_thread(_model.encode, text)
    return np.array(result, dtype=np.float32)


async def embed_batch(texts: list[str]) -> list[np.ndarray]:
    """Get embeddings for multiple texts in a single call."""
    if _model is None:
        raise RuntimeError("Embedding client not initialized")
    results = await asyncio.to_thread(_model.encode, texts)
    return [np.array(v, dtype=np.float32) for v in results]


async def check_health() -> bool:
    """Check if the embedding model is loaded and ready."""
    return _model is not None
