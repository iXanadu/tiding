"""PROJECT-RENAME-1: a project rename is a relabel plus an alias, never a
fresh empty bucket.

Project memory is filed under the ``project`` column, which a client sets
from its folder's cfg (``project = <name>``). Before this module, editing that
value stranded the folder's whole history in the old bucket with no warning,
and mail to the old name stopped reaching the folder's sessions. Measured on
the first real case (2026-10-11): three successive names for one product,
36 rows, three competing ``startup/next`` handoffs, and a reply that "Woke 0
agents" because it went to the new name while the live session still listened
on the old one.

Three parts, all keyed off one table (``project_aliases``: old -> new):

1. ``relabel`` — move every scope=project row of N source names into the
   target in ONE transaction. Same-key collisions keep the target's own row
   (then the first source listed) and rename the rest to
   ``archive/<source>/<key>``; every moved row is stamped ``moved_from`` so
   ``reverse`` can undo it exactly. The sources become aliases of the target.

2. Canonicalization at the API boundary — a memory request naming an old
   project is served from the new bucket (``PROJECT-ALIAS-HIT`` logged, the
   NS-1 namespace-alias pattern), and a send to an old project address is
   delivered to the new one with a receipt line telling the sender.

3. Read-time listen expansion — a reader listening on any name in a family
   also sees mail addressed to the others. Server-side on purpose: a bridge
   change lands only at the next session start, and the session that needs
   this most is the one already running under the old name (the
   ``admin_listen_expansion`` lesson).

Seats (``proj-claude-2``) are never aliased: they are one session's address,
not the project's. Only the bare project name and its ``@host`` form are.
"""

from __future__ import annotations

import logging
import re
import time

from server.config import settings
from server.db import get_pool

logger = logging.getLogger(__name__)

PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ARCHIVE_PREFIX = "archive/"
CACHE_TTL_SECONDS = 30.0

# old_name -> new_name, as stored (one hop). Resolution follows chains.
_aliases: dict[str, str] = {}
_loaded_at: float = 0.0


def _norm(name: str | None) -> str:
    return (name or "").strip().lower()


def validate_project_name(name: str) -> str:
    n = _norm(name)
    if not PROJECT_NAME_RE.match(n):
        raise ValueError(
            f"'{name}' is not a valid project name (lowercase letters, "
            f"digits, '.', '_' or '-'; must start with a letter or digit)")
    return n


async def refresh(force: bool = False) -> None:
    """Reload the alias map when stale (or always, with ``force``)."""
    global _aliases, _loaded_at
    if not force and _loaded_at and time.monotonic() - _loaded_at < CACHE_TTL_SECONDS:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT old_name, new_name FROM project_aliases")
    _aliases = {r["old_name"]: r["new_name"] for r in rows}
    _loaded_at = time.monotonic()


def canonical(name: str | None) -> str | None:
    """The current name for ``name`` (itself when it is not an alias).

    Sync and cache-only, so request validators can call it. Callers on an
    async path should ``await refresh()`` first; a validator runs on the
    cache the refresh loop keeps warm.
    """
    if not name:
        return name
    n = _norm(name)
    seen = {n}
    while n in _aliases:
        n = _aliases[n]
        if n in seen:  # a cycle cannot be written by relabel; never spin
            logger.error("PROJECT-ALIAS cycle at %r", n)
            break
        seen.add(n)
    return n


def canonical_logged(name: str | None) -> str | None:
    """``canonical`` that logs a hit — the evidence for retiring an alias."""
    c = canonical(name)
    if name and c != _norm(name):
        logger.info("PROJECT-ALIAS-HIT: %r -> %r (a client still uses the "
                    "old project name)", name, c)
    return c


def family(name: str | None) -> list[str]:
    """Every name that resolves to the same project as ``name``, canonical
    first. A name with no aliases is a family of one."""
    if not name:
        return []
    c = canonical(name)
    members = [c] + sorted(o for o in _aliases if o != c and canonical(o) == c)
    return members


def old_names(name: str | None) -> list[str]:
    return [m for m in family(name) if m != canonical(name)]


def _split(addr: str) -> tuple[str, str]:
    base, sep, host = addr.partition("@")
    return base, (sep + host)


def canonical_address(addr: str | None) -> str | None:
    """Rewrite a bare project address (or ``project@host``) to its current
    name. Anything that is not an alias — seats, persons, groups — passes
    through untouched."""
    if not addr:
        return addr
    a = addr.strip().lower()
    base, qual = _split(a)
    if base in _aliases:
        return canonical(base) + qual
    return a


async def expand_listen_set(listen_set: list[str]) -> list[str]:
    """Extra addresses to ADD to a reader's listen set (never a
    replacement): every other name in the family of each listened project
    name, keeping any ``@host`` qualifier."""
    await refresh()
    if not _aliases:
        return []
    have = {a.lower() for a in listen_set}
    extra: list[str] = []
    for a in listen_set:
        base, qual = _split(a.lower())
        fam = family(base)
        if len(fam) < 2:
            continue
        for m in fam:
            cand = m + qual
            if cand not in have:
                have.add(cand)
                extra.append(cand)
    return extra


async def list_aliases() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT old_name, new_name, created_at, created_by "
            "FROM project_aliases ORDER BY new_name, old_name")
    return [dict(r) for r in rows]


def _plan(rows, sources: list[str], target: str) -> tuple[dict, list[dict]]:
    """Decide the final key of every row. Pure — the dry run and the real run
    share it, so what the dry run shows is what the real run does."""
    rank = {target: 0, **{s: i + 1 for i, s in enumerate(sources)}}
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["namespace"], r["user_id"], r["key"]), []).append(r)
    taken = {(r["namespace"], r["user_id"], r["key"]) for r in rows}
    renames: list[dict] = []
    new_key: dict = {}
    for (ns, uid, key), members in groups.items():
        members.sort(key=lambda r: rank[r["project"]])
        for loser in members[1:]:
            base = f"{ARCHIVE_PREFIX}{loser['project']}/{key}"
            cand, n = base, 2
            while (ns, uid, cand) in taken:
                cand, n = f"{base}-{n}", n + 1
            taken.add((ns, uid, cand))
            new_key[loser["id"]] = cand
            renames.append({"from_project": loser["project"], "key": key,
                            "new_key": cand, "user_id": uid})
    counts = {s: 0 for s in sources}
    for r in rows:
        if r["project"] in counts:
            counts[r["project"]] += 1
    return {"counts": counts, "new_key": new_key}, renames


async def relabel(sources: list[str], target: str, dry_run: bool = True,
                  actor: str | None = None) -> dict:
    """Merge every scope=project row of ``sources`` into ``target`` and make
    each source an alias of it. See the module docstring."""
    target = validate_project_name(target)
    srcs: list[str] = []
    for s in sources:
        s = validate_project_name(s)
        if s != target and s not in srcs:
            srcs.append(s)
    if not srcs:
        raise ValueError("no source project names (other than the target)")
    await refresh(force=True)
    if target in _aliases:
        raise ValueError(
            f"'{target}' is itself an alias of '{canonical(target)}' — "
            f"relabel into '{canonical(target)}' instead")
    pool = await get_pool()
    # A project name can also be a PERSON's or a login's address (the owner's
    # own name was once a project name). Aliasing it would reroute that
    # person's mail into the project and let the project's sessions read it.
    # Refused — the rows can still be merged by hand; the alias cannot be.
    async with pool.acquire() as conn:
        clash = await conn.fetch(
            """
            SELECT name FROM principals WHERE lower(name) = ANY($1::text[])
            UNION
            SELECT alias FROM principal_aliases WHERE lower(alias) = ANY($1::text[])
            """,
            srcs,
        )
    owner = _norm(settings.owner_principal_name)
    clashing = sorted({_norm(r[0]) for r in clash} | ({owner} & set(srcs)))
    if clashing:
        raise ValueError(
            f"refusing to alias {clashing}: each is also a person's or a "
            f"login's address, so an alias would reroute that mail into "
            f"'{target}'. Leave it out of sources.")
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, namespace, key, user_id, project
                FROM memories
                WHERE scope = 'project' AND project = ANY($1::text[])
                FOR UPDATE
                """,
                srcs + [target],
            )
            plan, renames = _plan(rows, srcs, target)
            result = {
                "dry_run": dry_run, "target": target,
                "moved": plan["counts"],
                "total_moved": sum(plan["counts"].values()),
                "renamed": renames,
                "aliases": [{"old_name": s, "new_name": target} for s in srcs],
            }
            if dry_run:
                return result
            for row_id, key in plan["new_key"].items():
                await conn.execute(
                    "UPDATE memories SET key = $2 WHERE id = $1", row_id, key)
            await conn.execute(
                """
                UPDATE memories
                SET project = $2,
                    metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object('project', $2::text,
                                              'moved_from', project,
                                              'moved_at', NOW()::text)
                WHERE scope = 'project' AND project = ANY($1::text[])
                """,
                srcs, target,
            )
            # Flatten: names that pointed at a source now point at the target.
            await conn.execute(
                "UPDATE project_aliases SET new_name = $2 "
                "WHERE new_name = ANY($1::text[])", srcs, target)
            for s in srcs:
                await conn.execute(
                    """
                    INSERT INTO project_aliases (old_name, new_name, created_by)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (old_name) DO UPDATE
                    SET new_name = EXCLUDED.new_name,
                        created_by = EXCLUDED.created_by,
                        created_at = NOW()
                    """,
                    s, target, actor,
                )
    await refresh(force=True)
    logger.warning("PROJECT RELABEL %s -> %s moved=%s renamed=%d",
                   srcs, target, plan["counts"], len(renames))
    return result


async def reverse(source: str, target: str, actor: str | None = None) -> dict:
    """Undo ``relabel`` for one source: its rows go back (keys un-archived),
    and its alias is dropped. Rows written into the target since stay."""
    source = validate_project_name(source)
    target = validate_project_name(target)
    prefix = f"{ARCHIVE_PREFIX}{source}/"
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, key FROM memories
                WHERE scope = 'project' AND project = $1
                  AND metadata->>'moved_from' = $2
                FOR UPDATE
                """,
                target, source,
            )
            for r in rows:
                key = r["key"]
                if key.startswith(prefix):
                    key = key[len(prefix):]
                await conn.execute(
                    """
                    UPDATE memories
                    SET project = $2, key = $3,
                        metadata = (COALESCE(metadata, '{}'::jsonb)
                                    - 'moved_from' - 'moved_at')
                                   || jsonb_build_object('project', $2::text)
                    WHERE id = $1
                    """,
                    r["id"], source, key,
                )
            dropped = await conn.execute(
                "DELETE FROM project_aliases WHERE old_name = $1", source)
    await refresh(force=True)
    logger.warning("PROJECT RELABEL REVERSED %s <- %s rows=%d by=%s",
                   source, target, len(rows), actor)
    return {"source": source, "target": target, "restored": len(rows),
            "alias_dropped": dropped.endswith(" 1")}


def describe(name: str | None) -> dict:
    """What a client should know about its declared project name."""
    n = _norm(name)
    c = canonical(n)
    return {"project": n, "canonical": c, "renamed": c != n,
            "old_names": old_names(n)}
