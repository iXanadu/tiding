"""Compute the local session's inbox identity and listen_set.

An inbox "address" is a flat string. A Claude session listens on a **set** of
addresses — typically its project and its machine — and is addressed by the
same tuple when ack'ing read receipts.

The rule:
    CWD == $HOME         → admin session, listen_set = [machine:{host}]
    CWD is a project dir → project session, listen_set = [{project}, machine:{host}]
    no CWD               → fall back to [machine:{host}]

``project_dir`` comes from the tool call (passed by Claude Code via the
``project_dir`` parameter on engram_mcp tools) — NOT from ``os.getcwd()``
inside this subprocess, which is unreliable (see commit 223b17b).
"""

import os
import socket


def hostname() -> str:
    return socket.gethostname().split(".")[0].lower()


def _home_basename() -> str:
    home = os.environ.get("HOME") or ""
    return os.path.basename(home.rstrip("/")) if home else ""


def is_admin_context(project_dir: str | None) -> bool:
    """True when we're running from the user's home dir (no project)."""
    if not project_dir:
        return True
    pd = project_dir.rstrip("/")
    home = (os.environ.get("HOME") or "").rstrip("/")
    if home and pd == home:
        return True
    base = os.path.basename(pd)
    return bool(base) and base == _home_basename()


def compute_identity(project_dir: str | None) -> tuple[str, list[str]]:
    """Return ``(reader_identity, listen_set)`` for the current call.

    - reader_identity uniquely names this session: ``{project-or-machine}@{host}``
    - listen_set contains every address this session receives mail on:
        - the project name (loose broadcast: "any Claude in that project")
        - ``machine:{host}`` (loose broadcast: "any Claude on that machine")
        - the fully-qualified reader_identity itself (precise targeting:
          "the specific project@host session")
    """
    host = hostname()
    if is_admin_context(project_dir):
        addr = f"machine:{host}"
        return (addr, [addr])

    project = os.path.basename(project_dir.rstrip("/"))  # type: ignore[union-attr]
    if not project:
        addr = f"machine:{host}"
        return (addr, [addr])

    reader = f"{project}@{host}"
    return (reader, [project, f"machine:{host}", reader])


def reader_to_address(reader_identity: str) -> str:
    """Convert a reader_identity back to its loose-broadcast address.

    Reader identities have two shapes:
        ``{project}@{host}`` → loose address is ``{project}``
        ``machine:{host}``   → already a loose address, use as-is

    This is how ``memory_reply`` recovers the right ``to:`` for a reply —
    replies go to the sender's *role*, not their specific session.
    """
    if not reader_identity:
        return reader_identity
    if "@" in reader_identity and not reader_identity.startswith("machine:"):
        return reader_identity.split("@", 1)[0]
    return reader_identity
