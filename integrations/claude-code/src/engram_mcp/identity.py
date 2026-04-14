"""Compute the local session's inbox identity and listen_set.

An inbox "address" is a flat string. A Claude session listens on a **set** of
addresses — typically its project and its machine — and is addressed by the
same tuple when ack'ing read receipts.

The rule:
    CWD has a ``/projects/<name>/`` segment → project session, name = <name>
    anything else (``~``, ``/opt/srv``, ``/tmp``, bare ``~/projects``) → admin

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

import socket

ADMIN_NAME = "admin"


def hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


def derive_project_name(project_dir: str | None) -> str:
    """Return the project name for this session.

    Rule: if any path segment is ``projects`` and a non-empty segment follows
    it, that next segment is the project name. Otherwise the session is
    ``admin`` (machine-level work, scratch dirs, system paths, home).
    """
    if not project_dir:
        return ADMIN_NAME
    parts = [p for p in project_dir.strip().split("/") if p]
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts) and parts[idx + 1]:
            return parts[idx + 1]
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
    """
    host = hostname()
    name = derive_project_name(project_dir)
    reader = f"{name}@{host}"
    return (reader, [name, f"machine:{host}", reader])


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
