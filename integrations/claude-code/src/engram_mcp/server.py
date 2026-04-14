"""MCP server providing persistent semantic memory for Claude Code."""

import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from engram_mcp.client import MemoryClient
from engram_mcp.config import settings
from engram_mcp.identity import compute_identity, reader_to_address
from engram_mcp.scoping import resolve_scope_and_user_id


def _get_version() -> str:
    """Return version string with git short hash, e.g. '0.2.0 (abc1234)'.

    Falls back to just the version number if git is unavailable.
    """
    version = "0.2.0"
    try:
        repo_root = Path(__file__).resolve().parents[4]  # up to engram root
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        suffix = "-dirty" if dirty else ""
        return f"{version} ({commit}{suffix})"
    except Exception:
        return version


VERSION = _get_version()

mcp = FastMCP(
    "claude-memory",
    instructions="Persistent semantic memory for Claude Code sessions",
)

_client = MemoryClient(settings.memory_api_url, settings.memory_api_token)


@mcp.tool()
async def memory_store(
    key: str,
    value: str,
    tags: str = "",
    scope: str = "",
    project_dir: str = "",
) -> str:
    """Store a memory. Use for session progress, lessons learned, and important context.

    Args:
        key: Descriptive identifier (e.g. "session-2026-02-07-auth-refactor")
        value: The content to remember
        tags: Comma-separated tags for categorization (e.g. "session,progress")
        scope: machine (default), shared (all machines), or project (current project)
        project_dir: Required when scope=project. Pass your working directory path so project memories are scoped correctly.
    """
    resolved_scope, user_id = resolve_scope_and_user_id(
        scope or None, settings.memory_default_scope, project_dir or None
    )
    result = await _client.store(
        key=key,
        value=value,
        namespace=settings.memory_namespace,
        scope=resolved_scope,
        user_id=user_id,
        tags=tags,
    )
    return f"Stored memory '{result['key']}' (namespace: {settings.memory_namespace}, scope: {resolved_scope}, user_id: {user_id})"


def _render_inbox_banner(banner: dict | None) -> str:
    """Format an inbox banner dict as a short human-readable block."""
    if not banner:
        return ""
    count = banner.get("unread_count", 0)
    if not count:
        return ""
    lines = [f"📬 INBOX: {count} unread message(s) — call memory_inbox() to read"]
    for p in banner.get("preview", []):
        lines.append(f"  • {p}")
    return "\n".join(lines) + "\n\n---\n\n"


@mcp.tool()
async def memory_search(
    query: str,
    limit: int = 5,
    scope: str = "",
    project_dir: str = "",
) -> str:
    """Search memories semantically. Returns the most relevant matches.

    Args:
        query: Natural language search query
        limit: Max results to return (default 5)
        scope: machine (default), shared (all machines), or project (current project)
        project_dir: Required when scope=project. Pass your working directory path so project memories are scoped correctly.
    """
    if not query or not query.strip():
        return "No memories found."

    resolved_scope, user_id = resolve_scope_and_user_id(
        scope or None, settings.memory_default_scope, project_dir or None
    )
    reader_identity, listen_set = compute_identity(project_dir or None)
    read_ns = [ns.strip() for ns in settings.memory_read_namespaces.split(",") if ns.strip()]
    result = await _client.search(
        query=query,
        namespaces=read_ns,
        scope=resolved_scope,
        user_id=user_id,
        limit=limit,
        listen_set=listen_set,
        reader_identity=reader_identity,
    )

    banner_text = _render_inbox_banner(result.get("inbox_banner"))

    if result.get("status") != "ok" or not result.get("results"):
        if banner_text:
            return banner_text + "No memories found."
        return "No memories found."

    lines = []
    for mem in result["results"]:
        score = f" (score: {mem['score']:.3f})" if mem.get("score") else ""
        tags = f" [{mem['tags']}]" if mem.get("tags") else ""
        lines.append(f"**{mem['key']}**{tags}{score}\n{mem['value']}")
    return banner_text + "\n\n---\n\n".join(lines)


@mcp.tool()
async def memory_get(
    key: str,
    scope: str = "",
    project_dir: str = "",
) -> str:
    """Retrieve a specific memory by its exact key.

    Args:
        key: The exact key of the memory to retrieve
        scope: machine (default), shared (all machines), or project (current project)
        project_dir: Required when scope=project. Pass your working directory path so project memories are scoped correctly.
    """
    resolved_scope, user_id = resolve_scope_and_user_id(
        scope or None, settings.memory_default_scope, project_dir or None
    )
    result = await _client.get(
        key=key,
        namespace=settings.memory_namespace,
        scope=resolved_scope,
        user_id=user_id,
    )
    if result["status"] == "not_found":
        return f"No memory found with key '{key}'"
    mem = result["memory"]
    tags = f"\nTags: {mem['tags']}" if mem.get("tags") else ""
    return f"**{mem['key']}**{tags}\n{mem['value']}"


@mcp.tool()
async def memory_forget(
    key: str,
    scope: str = "",
    project_dir: str = "",
) -> str:
    """Delete a specific memory by its exact key.

    Args:
        key: The exact key of the memory to delete
        scope: machine (default), shared (all machines), or project (current project)
        project_dir: Required when scope=project. Pass your working directory path so project memories are scoped correctly.
    """
    resolved_scope, user_id = resolve_scope_and_user_id(
        scope or None, settings.memory_default_scope, project_dir or None
    )
    result = await _client.forget(
        key=key,
        namespace=settings.memory_namespace,
        scope=resolved_scope,
        user_id=user_id,
    )
    if result["status"] == "not_found":
        return f"No memory found with key '{key}'"
    return f"Deleted memory '{key}'"


@mcp.tool()
async def memory_status() -> str:
    """Check the health of the memory service and show connection status."""
    try:
        result = await _client.health()
        checks = result.get("checks", {})
        status = result.get("status", "unknown")
        lines = [f"Memory service: {status}", f"Server version: {VERSION}"]
        for name, ok in checks.items():
            lines.append(f"  {name}: {'ok' if ok else 'FAILED'}")
        return "\n".join(lines)
    except Exception as e:
        return f"Memory service unreachable: {e}\nServer version: {VERSION}"


def _format_inbox_message(m: dict) -> str:
    sender = m.get("from_") or "unknown"
    subject = m.get("subject") or "(no subject)"
    thread = f" [thread: {m['thread_id']}]" if m.get("thread_id") else ""
    header = f"**{m['id']}**{thread}\nFrom: {sender}  →  {m['to']}\nSubject: {subject}"
    body = m.get("body", "")
    return f"{header}\n\n{body}"


@mcp.tool()
async def memory_send(
    to: str,
    body: str,
    subject: str = "",
    thread_id: str = "",
    project_dir: str = "",
) -> str:
    """Send an inbox message to another Claude session.

    Addresses are flat strings:
      - A project name (e.g. "engram") — any Claude working in that project
      - "machine:<hostname>" — any Claude on that machine (usually admin sessions)

    Args:
        to: Recipient address (project name or "machine:hostname")
        body: Message body
        subject: Short subject line
        thread_id: Optional thread id to group a back-and-forth
        project_dir: Your working directory path (used to stamp "from")
    """
    if not to or not to.strip():
        return "Error: 'to' is required."
    if not body or not body.strip():
        return "Error: 'body' is required."
    reader_identity, _ = compute_identity(project_dir or None)
    result = await _client.inbox_send(
        to=to.strip(),
        body=body,
        subject=subject,
        from_=reader_identity,
        thread_id=thread_id or None,
    )
    return f"Sent inbox message {result['id']} → {to} (from {reader_identity})"


@mcp.tool()
async def memory_inbox(
    unread_only: bool = True,
    limit: int = 20,
    project_dir: str = "",
) -> str:
    """Read the inbox for this Claude session.

    Listens on the current project AND the current machine. Pass
    unread_only=False to see previously-acknowledged messages too.

    Args:
        unread_only: When True (default), only show messages this session hasn't acked
        limit: Max messages to return
        project_dir: Your working directory path (used to compute listen_set)
    """
    reader_identity, listen_set = compute_identity(project_dir or None)
    result = await _client.inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=unread_only,
        limit=limit,
    )
    if result.get("status") != "ok":
        return f"Inbox error: {result}"
    msgs = result.get("messages", [])
    if not msgs:
        return f"Inbox empty for {reader_identity} (listen_set={listen_set})."
    header = f"Inbox for {reader_identity} (listen_set={listen_set}) — {len(msgs)} message(s):\n"
    return header + "\n\n---\n\n".join(_format_inbox_message(m) for m in msgs)


@mcp.tool()
async def memory_ack(
    message_id: str,
    project_dir: str = "",
) -> str:
    """Mark an inbox message as read by this session.

    Acks are per-reader — other sessions listening on the same address can
    still see the message until they ack it themselves.

    Args:
        message_id: The inbox message id (e.g. "inbox/abc-123")
        project_dir: Your working directory path (used to compute reader identity)
    """
    reader_identity, _ = compute_identity(project_dir or None)
    try:
        result = await _client.inbox_ack(message_id=message_id, reader_identity=reader_identity)
    except Exception as e:
        return f"Ack failed: {e}"
    return f"Acked {result['id']} as {reader_identity}"


@mcp.tool()
async def memory_reply(
    message_id: str,
    body: str,
    subject: str = "",
    project_dir: str = "",
) -> str:
    """Reply to an inbox message and ack it in one call.

    Looks up the parent to determine the reply address and thread_id, then
    sends a new inbox message AND marks the parent as read.

    Args:
        message_id: The id of the message being replied to
        body: The reply body
        subject: Optional subject for the reply
        project_dir: Your working directory path
    """
    reader_identity, listen_set = compute_identity(project_dir or None)
    # Fetch the parent to resolve its sender + thread_id
    parent_list = await _client.inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=False,
        limit=200,
    )
    parent = None
    for m in parent_list.get("messages", []):
        if m["id"] == message_id:
            parent = m
            break
    if parent is None:
        return f"Cannot reply: parent message {message_id} not found in this session's listen_set."

    raw_from = parent.get("from_")
    if not raw_from:
        return f"Cannot reply: parent message {message_id} has no 'from' address."
    # Replies go to the sender's loose-broadcast address (project name or
    # machine:host), NOT their fully-qualified reader_identity. A specific
    # session is the reader, but the addressable role is the project.
    reply_to = reader_to_address(raw_from)
    thread_id = parent.get("thread_id") or parent["id"]

    send_result = await _client.inbox_send(
        to=reply_to,
        body=body,
        subject=subject or f"re: {parent.get('subject', '')}",
        from_=reader_identity,
        thread_id=thread_id,
    )
    await _client.inbox_ack(message_id=message_id, reader_identity=reader_identity)
    return f"Replied to {message_id} → {reply_to} (thread {thread_id}); sent {send_result['id']}"


@mcp.tool()
async def memory_inbox_archive(
    message_id: str,
    project_dir: str = "",
) -> str:
    """Archive an inbox message so it disappears from all future inbox views."""
    reader_identity, _ = compute_identity(project_dir or None)
    try:
        result = await _client.inbox_archive(
            message_id=message_id,
            reader_identity=reader_identity,
        )
    except Exception as e:
        return f"Archive failed: {e}"
    return f"Archived {result['id']}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
