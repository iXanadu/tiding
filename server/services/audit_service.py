"""AUDIT-1: the append-only write trail.

The ``audit_log`` table shipped with the principals work and sat at ZERO rows
— so the store could not answer "who wrote what, when." Proven costly
2026-07-24: "did a shut-down agent store its findings?" took three
inferential DB queries by hand instead of one lookup, and still could not
rule out an overwrite; the same week a /memory/forget could be proven but
not dated (OBS-1). This module is the missing writer.

WHAT GETS RECORDED (decided 2026-07-27): mutations of memory rows —
``memory.set`` (with created/replaced), ``memory.forget`` (with outcome),
``admin.bulk_delete`` (executed deletes only, with the predicate and count),
and ``system.expiration_sweep`` summaries. Reads are deliberately absent:
``last_used_at`` already bumps on reads, volume would swamp the trail, and
the questions this exists to answer are all about writes. Inbox lifecycle
(ack/resolve) is metadata churn on rows the trail already covers at
creation; recording it can be added if a forensic need ever shows.

RETENTION (decided the same day): unbounded for now. Rows are ~200 bytes and
write volume is tens per hour; an append-only trail that silently expires is
worth less than the disk it saves. Revisit when size actually matters.

BEST-EFFORT BY CONSTRUCTION: an audit insert must never fail the operation
it describes — a store that refuses writes because its bookkeeping is down
has inverted its priorities. Failures are logged (with OBS-1's timestamps,
they are now datable) and dropped.

The principal NAME rides inside ``detail`` even though ``principal_id`` is a
column: the FK is ``ON DELETE SET NULL``, so a trail row must survive its
principal's deletion without losing attribution.
"""

import json
import logging

from server.db import get_pool

logger = logging.getLogger(__name__)


async def audit(
    action: str,
    principal: dict | None,
    detail: dict,
    target_principal_id: str | None = None,
) -> None:
    """Append one row to the write trail. Never raises."""
    try:
        record = dict(detail)
        if principal and principal.get("name"):
            record["by"] = principal["name"]
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (principal_id, action,
                                       target_principal_id, detail)
                VALUES ($1, $2, $3, $4)
                """,
                principal.get("id") if principal else None,
                action,
                target_principal_id,
                json.dumps(record, separators=(",", ":"), default=str),
            )
    except Exception:
        logger.warning("audit write failed for action=%s", action, exc_info=True)
