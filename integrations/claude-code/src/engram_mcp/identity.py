"""Compute the local session's inbox identity and listen_set.

An inbox "address" is a flat string. A Claude session listens on a **set** of
addresses — typically its project and its machine — and is addressed by the
same tuple when ack'ing read receipts.

The rule:
    1. Walk up from CWD looking for ``.engram.cfg`` — if found, use declared name
    2. Else CWD has a ``/projects/<name>/`` segment → project session, name = <name>
    3. Else (``~``, ``/opt/srv``, ``/tmp``, bare ``~/projects``) → admin

Project session:
    reader_identity = "<name>@<host>"
    listen_set      = ["<name>", "machine:<host>", "<name>@<host>"]

Admin session:
    reader_identity = "admin@<host>"
    listen_set      = ["admin", "machine:<host>", "admin@<host>"]

``project_dir`` comes from the tool call (passed by Claude Code via the
``project_dir`` parameter on engram_mcp tools) — NOT from ``os.getcwd()``
inside this subprocess, which is unreliable (see commit 223b17b).
"""

import os
import socket

from engram_mcp.scoping import resolve_project_name

ADMIN_NAME = "admin"

# Opt-in per-session inbox identity. When set, this session is ADDRESSED as
# ``<value>@<host>`` and sends FROM that identity, while still joining its
# project's group address for broadcasts. MEMORY scoping is unaffected — it
# derives from .engram.cfg, not this. This is how two sessions that share one
# project (and thus shared scope=project memory) get DISTINCT inbox identities
# so they can DM each other and so the watcher's self-echo filter stays precise.
# See decision/three-axes-principal-project-address.
INBOX_IDENTITY_ENV = "ENGRAM_INBOX_IDENTITY"


def hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


def derive_project_name(project_dir: str | None) -> str:
    """Return the project name for this session.

    Resolution order:
        1. ``.engram.cfg`` walk-up (authoritative — survives prod/dev layouts)
        2. ``/projects/<name>/`` path segment (legacy convention)
        3. ``admin`` (machine-level work, scratch dirs, system paths, home)
    """
    declared = resolve_project_name(project_dir)
    if declared:
        return declared.lower()
    if not project_dir:
        return ADMIN_NAME
    parts = [p for p in project_dir.strip().split("/") if p]
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts) and parts[idx + 1]:
            return parts[idx + 1].lower()
    return ADMIN_NAME


def is_admin_context(project_dir: str | None) -> bool:
    """True when the session should be treated as an admin session."""
    return derive_project_name(project_dir) == ADMIN_NAME


def compute_identity(project_dir: str | None) -> tuple[str, list[str]]:
    """Return ``(reader_identity, listen_set)`` for the current call.

    - reader_identity uniquely names this session: ``<name>@<host>``
    - listen_set contains every address this session receives mail on:
        - the role/project name (loose broadcast: "any Claude with that role")
        - ``machine:<host>`` (loose broadcast: "any Claude on that machine")
        - the fully-qualified reader_identity itself (precise targeting)

    Admin and project sessions are symmetric — the only difference is that
    admin's loose-broadcast name is the literal string ``admin``.

    When ``ENGRAM_INBOX_IDENTITY`` is set, the session keeps its project's group
    address but is precisely addressed (and sends) as ``<override>@<host>`` — so
    sibling sessions sharing one project get distinct inbox identities without
    splitting their shared memory.
    """
    host = hostname()
    project = derive_project_name(project_dir)
    override = (os.environ.get(INBOX_IDENTITY_ENV) or "").strip().lower()
    if override and override != project:
        reader = f"{override}@{host}"
        # precise identity first, then the project GROUP address (broadcasts to
        # all sessions on the project still land), then machine, then self.
        return (reader, [override, project, f"machine:{host}", reader])
    reader = f"{project}@{host}"
    return (reader, [project, f"machine:{host}", reader])


def reader_to_address(reader_identity: str) -> str:
    """Convert a reader_identity back to its loose-broadcast address.

    Reader identities have shape ``<name>@<host>``; the loose address is the
    ``<name>`` part. Legacy ``machine:<host>`` identities (pre-admin rollout)
    pass through unchanged for backward compatibility with already-sent mail.

    This is how ``memory_reply`` recovers the right ``to:`` for a reply —
    replies go to the sender's *role*, not their specific session.
    """
    if not reader_identity:
        return reader_identity
    if reader_identity.startswith("machine:"):
        return reader_identity
    if "@" in reader_identity:
        return reader_identity.split("@", 1)[0]
    return reader_identity
