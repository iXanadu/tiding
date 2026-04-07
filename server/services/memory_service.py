import json
import re
from datetime import datetime, timedelta, timezone

import asyncpg
import numpy as np

from server.config import settings
from server.db import get_pool
from server.embeddings import embed
from server.models import MemoryItem


def _expand_key(key: str) -> str:
    """Expand snake_case/camelCase key into natural words.

    'my_location' → 'my location'
    'wifeName' → 'wife Name'
    """
    expanded = key.replace("_", " ").replace("-", " ")
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", expanded)
    return expanded.lower().strip()


def _build_search_text(key: str, value: str, tags: str) -> str:
    """Build the combined text that gets embedded and trigram-indexed."""
    key_expanded = _expand_key(key)
    parts = [key_expanded, key, value]
    if tags:
        parts.append(tags)
    return " ".join(parts)


async def memory_set(
    namespace: str,
    key: str,
    value: str,
    scope: str = "user",
    user_id: str = "default",
    tags: str = "",
    tags_search: str = "",
    expiration_days: int = 180,
    metadata: dict | None = None,
) -> str:
    """Store or update a memory with its embedding."""
    pool = await get_pool()
    search_text = _build_search_text(key, value, tags)
    embedding = await embed(search_text)

    # 0 = never expires
    expires_at = None
    if expiration_days and expiration_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expiration_days)

    metadata_json = json.dumps(metadata) if metadata else None

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO memories (namespace, key, value, scope, user_id, tags, tags_search, embedding, search_text, expires_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            ON CONFLICT (namespace, key, scope, user_id) DO UPDATE SET
                value = EXCLUDED.value,
                tags = EXCLUDED.tags,
                tags_search = EXCLUDED.tags_search,
                embedding = EXCLUDED.embedding,
                search_text = EXCLUDED.search_text,
                expires_at = EXCLUDED.expires_at,
                metadata = EXCLUDED.metadata,
                last_used_at = NOW()
            """,
            namespace,
            key,
            value,
            scope,
            user_id,
            tags,
            tags_search,
            embedding,
            search_text,
            expires_at,
            metadata_json,
        )
    return key


async def memory_get(
    namespace: str,
    key: str,
    scope: str = "user",
    user_id: str = "default",
) -> MemoryItem | None:
    """Retrieve a memory by exact key within a namespace."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT namespace, key, value, scope, user_id, tags, tags_search
            FROM memories
            WHERE namespace = $1 AND key = $2 AND scope = $3 AND user_id = $4
              AND (expires_at IS NULL OR expires_at > NOW())
            """,
            namespace,
            key,
            scope,
            user_id,
        )
        if row:
            await conn.execute(
                """UPDATE memories SET last_used_at = NOW()
                   WHERE namespace = $1 AND key = $2 AND scope = $3 AND user_id = $4""",
                namespace,
                key,
                scope,
                user_id,
            )
            return MemoryItem(**dict(row))
    return None


async def memory_search(
    namespaces: list[str],
    query: str,
    scope: str = "user",
    user_id: str = "default",
    limit: int = 5,
) -> list[MemoryItem]:
    """Hybrid vector + trigram search, scoped to namespace(s) and user."""
    pool = await get_pool()
    query_embedding = await embed(query)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH vector_results AS (
                SELECT
                    namespace, key, value, scope, user_id, tags, tags_search,
                    1 - (embedding <=> $1) AS vec_score,
                    similarity(search_text, $2) AS trgm_score
                FROM memories
                WHERE (expires_at IS NULL OR expires_at > NOW())
                  AND namespace = ANY($3::text[])
                  AND scope = $4
                  AND user_id = $5
                ORDER BY embedding <=> $1
                LIMIT $6 * 3
            )
            SELECT *,
                   vec_score + ($7 * trgm_score) AS combined_score
            FROM vector_results
            WHERE vec_score >= $8 OR trgm_score >= $9
            ORDER BY combined_score DESC
            LIMIT $6
            """,
            query_embedding,
            query,
            namespaces,
            scope,
            user_id,
            limit,
            settings.trigram_weight,
            settings.vector_threshold,
            settings.trigram_threshold,
        )

        results = []
        keys_to_update: dict[str, list[str]] = {}
        for row in rows:
            results.append(
                MemoryItem(
                    namespace=row["namespace"],
                    key=row["key"],
                    value=row["value"],
                    scope=row["scope"],
                    user_id=row["user_id"],
                    tags=row["tags"],
                    tags_search=row["tags_search"],
                    score=round(float(row["combined_score"]), 4),
                )
            )
            keys_to_update.setdefault(row["namespace"], []).append(row["key"])

        for ns, keys in keys_to_update.items():
            await conn.execute(
                """UPDATE memories SET last_used_at = NOW()
                   WHERE namespace = $1 AND key = ANY($2) AND scope = $3 AND user_id = $4""",
                ns,
                keys,
                scope,
                user_id,
            )

    return results


async def memory_forget(
    namespace: str,
    key: str,
    scope: str = "user",
    user_id: str = "default",
) -> bool:
    """Delete a memory by key within a namespace. Returns True if found and deleted."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM memories WHERE namespace = $1 AND key = $2 AND scope = $3 AND user_id = $4",
            namespace,
            key,
            scope,
            user_id,
        )
    return result == "DELETE 1"
