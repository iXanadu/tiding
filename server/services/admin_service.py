import logging
from datetime import datetime, timedelta, timezone

from server.db import get_pool
from server.models import MemoryListItem, NamespaceStats

logger = logging.getLogger(__name__)


async def list_memories(
    namespaces: list[str] | None = None,
    scope: str | None = None,
    user_id: str | None = None,
    key_prefix: str | None = None,
    search: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    offset: int = 0,
    limit: int = 50,
    sort_by: str = "key",
    sort_order: str = "asc",
    include_value: bool = False,
    value_max_length: int = 200,
) -> tuple[int, list[MemoryListItem]]:
    pool = await get_pool()

    conditions: list[str] = []
    params: list = []
    idx = 1

    if namespaces:
        conditions.append(f"namespace = ANY(${idx}::text[])")
        params.append(namespaces)
        idx += 1
    if scope is not None:
        conditions.append(f"scope = ${idx}")
        params.append(scope)
        idx += 1
    if user_id is not None:
        conditions.append(f"user_id = ${idx}")
        params.append(user_id)
        idx += 1
    if key_prefix is not None:
        conditions.append(f"key LIKE ${idx}")
        params.append(key_prefix + "%")
        idx += 1
    if search is not None:
        conditions.append(f"search_text ILIKE ${idx}")
        params.append(f"%{search}%")
        idx += 1
    if created_after is not None:
        conditions.append(f"created_at >= ${idx}")
        params.append(created_after)
        idx += 1
    if created_before is not None:
        conditions.append(f"created_at < ${idx}")
        params.append(created_before)
        idx += 1

    where = " AND ".join(conditions) if conditions else "TRUE"

    allowed_sort = {"key": "key", "created_at": "created_at", "last_used_at": "last_used_at"}
    sort_col = allowed_sort.get(sort_by, "key")
    direction = "DESC" if sort_order.lower() == "desc" else "ASC"

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM memories WHERE {where}", *params
        )

        value_expr = f"LEFT(value, {int(value_max_length)})" if include_value else "NULL"
        rows = await conn.fetch(
            f"""
            SELECT namespace, key, {value_expr} AS value, scope, user_id, tags,
                   created_at, last_used_at, expires_at
            FROM memories
            WHERE {where}
            ORDER BY {sort_col} {direction}
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
            limit,
            offset,
        )

    items = [
        MemoryListItem(
            namespace=r["namespace"],
            key=r["key"],
            value=r["value"],
            scope=r["scope"],
            user_id=r["user_id"],
            tags=r["tags"],
            created_at=r["created_at"],
            last_used_at=r["last_used_at"],
            expires_at=r["expires_at"],
        )
        for r in rows
    ]
    return total, items


async def update_memory(
    namespace: str,
    key: str,
    scope: str,
    user_id: str,
    new_namespace: str | None = None,
    new_scope: str | None = None,
    new_user_id: str | None = None,
    new_key: str | None = None,
    new_tags: str | None = None,
) -> bool:
    pool = await get_pool()

    sets: list[str] = []
    params: list = []
    idx = 1

    if new_namespace is not None:
        sets.append(f"namespace = ${idx}")
        params.append(new_namespace)
        idx += 1
    if new_scope is not None:
        sets.append(f"scope = ${idx}")
        params.append(new_scope)
        idx += 1
    if new_user_id is not None:
        sets.append(f"user_id = ${idx}")
        params.append(new_user_id)
        idx += 1
    if new_key is not None:
        sets.append(f"key = ${idx}")
        params.append(new_key)
        idx += 1
    if new_tags is not None:
        sets.append(f"tags = ${idx}")
        params.append(new_tags)
        idx += 1

    if not sets:
        return False

    set_clause = ", ".join(sets)

    # WHERE clause uses the original identity
    where = f"namespace = ${idx} AND key = ${idx + 1} AND scope = ${idx + 2} AND user_id = ${idx + 3}"
    params.extend([namespace, key, scope, user_id])

    async with pool.acquire() as conn:
        result = await conn.execute(
            f"UPDATE memories SET {set_clause} WHERE {where}",
            *params,
        )

    return result != "UPDATE 0"


async def get_stats(
    namespace: str | None = None,
    by_scope: bool = False,
) -> list[NamespaceStats]:
    pool = await get_pool()

    group_cols = ["namespace"]
    if by_scope:
        group_cols.append("scope")

    group_clause = ", ".join(group_cols)

    conditions = []
    params: list = []
    if namespace is not None:
        conditions.append("namespace = $1")
        params.append(namespace)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {group_clause},
                   COUNT(*) AS count,
                   MIN(created_at) AS oldest,
                   MAX(created_at) AS newest,
                   COUNT(*) FILTER (
                       WHERE expires_at IS NOT NULL AND expires_at <= NOW()
                   ) AS expired_count
            FROM memories
            {where}
            GROUP BY {group_clause}
            ORDER BY {group_clause}
            """,
            *params,
        )

    return [
        NamespaceStats(
            namespace=r["namespace"],
            scope=r["scope"] if by_scope else None,
            count=r["count"],
            oldest=r["oldest"],
            newest=r["newest"],
            expired_count=r["expired_count"],
        )
        for r in rows
    ]


async def bulk_delete(
    namespace: str,
    key_prefix: str,
    scope: str | None = None,
    user_id: str | None = None,
    older_than_days: int | None = None,
) -> int:
    pool = await get_pool()

    conditions = ["namespace = $1", "key LIKE $2"]
    params: list = [namespace, key_prefix + "%"]
    idx = 3

    if scope is not None:
        conditions.append(f"scope = ${idx}")
        params.append(scope)
        idx += 1
    if user_id is not None:
        conditions.append(f"user_id = ${idx}")
        params.append(user_id)
        idx += 1
    if older_than_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        conditions.append(f"created_at < ${idx}")
        params.append(cutoff)
        idx += 1

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        result = await conn.execute(f"DELETE FROM memories WHERE {where}", *params)

    # result is like "DELETE 5"
    return int(result.split()[-1])


async def cleanup_expired(batch_size: int = 500) -> int:
    pool = await get_pool()
    total_deleted = 0

    async with pool.acquire() as conn:
        while True:
            result = await conn.execute(
                """
                DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories
                    WHERE expires_at IS NOT NULL AND expires_at <= NOW()
                    LIMIT $1
                )
                """,
                batch_size,
            )
            count = int(result.split()[-1])
            total_deleted += count
            if count < batch_size:
                break

    if total_deleted > 0:
        logger.info(f"Cleaned up {total_deleted} expired memories")
    return total_deleted
