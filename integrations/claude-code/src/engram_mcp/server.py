"""MCP server providing persistent semantic memory for Claude Code."""

import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from engram_mcp.client import MemoryClient
from engram_mcp.config import settings
from engram_mcp.identity import compute_identity, reader_to_address
from engram_mcp.scoping import (
    AmbiguousIdentity,
    ensure_project_identity,
    resolve_partition,
    write_project_cfg,
)


_PRINCIPAL_CACHE: dict | None = None
_PRINCIPAL_FETCHED = False


async def _get_principal_name() -> str | None:
    """Fetch the authenticated principal's name via /whoami, cached.

    Returns the principal name on success, or None when the bridge is
    running anonymously / token invalid. Logs nothing — we don't want to
    fail memory ops just because /whoami isn't reachable; downstream
    falls back to ``user_id='unknown'`` in that case.
    """
    global _PRINCIPAL_CACHE, _PRINCIPAL_FETCHED
    if _PRINCIPAL_FETCHED:
        return _PRINCIPAL_CACHE.get("name") if _PRINCIPAL_CACHE else None
    _PRINCIPAL_FETCHED = True
    if not settings.memory_api_token:
        return None
    try:
        _PRINCIPAL_CACHE = await _client.whoami()
    except Exception:
        _PRINCIPAL_CACHE = None
    return _PRINCIPAL_CACHE.get("name") if _PRINCIPAL_CACHE else None


async def _resolve_partition_with_identity(scope: str | None, project_dir: str | None):
    """Wrap partition resolution with identity auto-write / ambiguous raise.

    For scope=project, invoke ensure_project_identity first — it auto-writes
    .engram.cfg under Rule 1 ($HOME) or Rule 2 (~/projects/<x>/.claude/),
    or raises AmbiguousIdentity for Rule 3 (ambiguous dir). After that,
    resolve_partition returns the (scope, user_id, project) triple, with
    user_id resolved to the authenticated principal name (Phase 3).
    """
    effective_scope = scope or settings.memory_default_scope
    if effective_scope == "project":
        effective = project_dir if project_dir and Path(project_dir).is_absolute() else None
        # Skip ensure when project_dir isn't a usable absolute path — the
        # tool's project_dir docstring already directs callers to pass one.
        if effective:
            ensure_project_identity(effective)  # side effect: writes cfg or raises
    principal_name = await _get_principal_name() if effective_scope == "project" else None
    return resolve_partition(
        scope or None,
        settings.memory_default_scope,
        project_dir or None,
        principal_name=principal_name,
    )


def _identity_error_message(e: AmbiguousIdentity) -> str:
    """Format a structured prompt for Claude when scope=project is ambiguous.

    Wording is deliberately imperative: past sessions have silently
    written to file-based ``.claude/projects/.../MEMORY.md`` when this
    error fires, hiding the prompt from the user. DO NOT do that.
    """
    return (
        f"STOP. Project identity is required for scope=project at "
        f"'{e.project_dir}'.\n\n"
        f"DO NOT fall back to file-based memory "
        f"(`.claude/projects/*/MEMORY.md` or `MEMORY.md` anywhere).\n"
        f"DO NOT silently store the memory somewhere else.\n"
        f"DO NOT bury this prompt in a footnote while doing other work.\n"
        f"DO NOT skip the memory call and continue as if nothing happened.\n\n"
        f"Ask the user this exact question and WAIT for their answer:\n\n"
        f'  "I\'m in {e.project_dir}. This isn\'t a known project. For\n'
        f"   memory storage, do you want to:\n"
        f"     (1) Declare this folder as project '{e.suggested}' "
        f"(basename suggestion)?\n"
        f"     (2) Treat it as admin territory (project = admin)?\n"
        f'     (3) Use a custom project name?"\n\n'
        f"After the user picks, call:\n"
        f"  memory_declare_identity(project_dir='{e.project_dir}', name=<chosen>)\n"
        f"where <chosen> is the user's project name or 'admin'. Then retry\n"
        f"the original memory call.\n\n"
        f"This writes .engram.cfg at {e.project_dir} so identity persists — "
        f"you won't be asked again at this path."
    )


def _append_guidance(body: str, result: dict) -> str:
    """Append server-provided usage guidance to a tool result string.

    The engram server returns a 'guidance' field on inbox responses — usage
    hints, addressing rules, polling cadence — so we can iterate on wording
    server-side without forcing an MCP/Claude restart.
    """
    guidance = result.get("guidance") if isinstance(result, dict) else None
    if not guidance:
        return body
    return f"{body}\n\n---\n{guidance}"


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
    try:
        resolved_scope, user_id, project = await _resolve_partition_with_identity(
            scope or None, project_dir or None
        )
    except AmbiguousIdentity as e:
        return _identity_error_message(e)
    reader_identity, listen_set = compute_identity(project_dir or None)
    result = await _client.store(
        key=key,
        value=value,
        namespace=settings.memory_namespace,
        scope=resolved_scope,
        user_id=user_id,
        project=project,
        tags=tags,
        project_dir=project_dir or None,
        listen_set=listen_set,
        reader_identity=reader_identity,
    )
    banner_text = _render_inbox_banner(result.get("inbox_banner"))
    proj_suffix = f", project: {project}" if project else ""
    head = f"Stored memory '{result['key']}' (namespace: {settings.memory_namespace}, scope: {resolved_scope}, user_id: {user_id}{proj_suffix})"
    return banner_text + head if banner_text else head


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

    try:
        resolved_scope, user_id, project = await _resolve_partition_with_identity(
            scope or None, project_dir or None
        )
    except AmbiguousIdentity as e:
        return _identity_error_message(e)
    reader_identity, listen_set = compute_identity(project_dir or None)
    read_ns = [ns.strip() for ns in settings.memory_read_namespaces.split(",") if ns.strip()]
    result = await _client.search(
        query=query,
        namespaces=read_ns,
        scope=resolved_scope,
        user_id=user_id,
        project=project,
        limit=limit,
        listen_set=listen_set,
        reader_identity=reader_identity,
        project_dir=project_dir or None,
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
    try:
        resolved_scope, user_id, project = await _resolve_partition_with_identity(
            scope or None, project_dir or None
        )
    except AmbiguousIdentity as e:
        return _identity_error_message(e)
    result = await _client.get(
        key=key,
        namespace=settings.memory_namespace,
        scope=resolved_scope,
        user_id=user_id,
        project=project,
        project_dir=project_dir or None,
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
    try:
        resolved_scope, user_id, project = await _resolve_partition_with_identity(
            scope or None, project_dir or None
        )
    except AmbiguousIdentity as e:
        return _identity_error_message(e)
    result = await _client.forget(
        key=key,
        namespace=settings.memory_namespace,
        scope=resolved_scope,
        user_id=user_id,
        project=project,
        project_dir=project_dir or None,
    )
    if result["status"] == "not_found":
        return f"No memory found with key '{key}'"
    return f"Deleted memory '{key}'"


@mcp.tool()
async def memory_declare_identity(
    project_dir: str,
    name: str,
) -> str:
    """Declare the canonical project identity for a directory by writing
    .engram.cfg there.

    Call this to resolve an "IDENTITY NEEDED" prompt from another memory
    tool. After it succeeds, retry the original call — scope=project will
    now resolve deterministically.

    Args:
        project_dir: Absolute path where to write .engram.cfg (the ambiguous
            directory from the prompt, e.g. "/tmp/scratch" or
            "~/Documents/HomeMaintenance").
        name: The project name to declare. Use the user-suggested basename,
            a custom name, or "admin" if the user chose admin territory.
            Must match [A-Za-z0-9._-]+.
    """
    if not project_dir or not project_dir.strip():
        return "Error: project_dir is required."
    if not name or not name.strip():
        return "Error: name is required."
    try:
        path = write_project_cfg(project_dir.strip(), name.strip())
    except ValueError as e:
        return f"Error: {e}"
    except OSError as e:
        return f"Error writing .engram.cfg: {e}"
    return (
        f"Declared project identity: {name} at {project_dir}\n"
        f"Wrote {path}. Retry the memory call that triggered this."
    )


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
    """Send an inbox message to another Claude session. Response includes
    current addressing guidance — read it.

    Args:
        to: Recipient address
        body: Message body
        subject: Short subject line
        thread_id: Optional thread id to group a back-and-forth
        project_dir: Your working directory path (required for identity)
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
        project_dir=project_dir or None,
    )
    corrected_from = result.get("corrected_from")
    if corrected_from:
        head = f"Sent inbox message {result['id']} → (from {reader_identity})\n⚠️  Address auto-corrected: '{corrected_from}' was rewritten. See guidance below."
    else:
        head = f"Sent inbox message {result['id']} → {to} (from {reader_identity})"
    return _append_guidance(head, result)


@mcp.tool()
async def memory_inbox(
    unread_only: bool = True,
    limit: int = 20,
    project_dir: str = "",
) -> str:
    """Read this session's inbox. Response includes current usage guidance
    for reply/ack/archive — read it.

    Args:
        unread_only: When True (default), only show messages this session hasn't acked
        limit: Max messages to return
        project_dir: Your working directory path (required for identity)
    """
    reader_identity, listen_set = compute_identity(project_dir or None)
    result = await _client.inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=unread_only,
        limit=limit,
        project_dir=project_dir or None,
    )
    if result.get("status") != "ok":
        return f"Inbox error: {result}"
    msgs = result.get("messages", [])
    if not msgs:
        head = f"Inbox empty for {reader_identity} (listen_set={listen_set})."
    else:
        header = f"Inbox for {reader_identity} (listen_set={listen_set}) — {len(msgs)} message(s):\n"
        head = header + "\n\n---\n\n".join(_format_inbox_message(m) for m in msgs)
    return _append_guidance(head, result)


@mcp.tool()
async def memory_ack(
    message_id: str,
    project_dir: str = "",
) -> str:
    """Mark an inbox message as read by this session. Response includes
    per-reader vs archive semantics — read it.

    Args:
        message_id: The inbox message id (e.g. "inbox/abc-123")
        project_dir: Your working directory path (required for identity)
    """
    reader_identity, _ = compute_identity(project_dir or None)
    try:
        result = await _client.inbox_ack(
            message_id=message_id,
            reader_identity=reader_identity,
            project_dir=project_dir or None,
        )
    except Exception as e:
        return f"Ack failed: {e}"
    head = f"Acked {result['id']} as {reader_identity}"
    return _append_guidance(head, result)


@mcp.tool()
async def memory_reply(
    message_id: str,
    body: str,
    subject: str = "",
    project_dir: str = "",
) -> str:
    """Reply to an inbox message and ack it in one call. Addressing and
    thread-linking are automatic. Response includes current guidance.

    Args:
        message_id: The id of the message being replied to
        body: The reply body
        subject: Optional subject for the reply
        project_dir: Your working directory path (required for identity)
    """
    reader_identity, listen_set = compute_identity(project_dir or None)
    parent_list = await _client.inbox_list(
        listen_set=listen_set,
        reader_identity=reader_identity,
        unread_only=False,
        limit=200,
        project_dir=project_dir or None,
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
    reply_to = reader_to_address(raw_from)
    thread_id = parent.get("thread_id") or parent["id"]

    send_result = await _client.inbox_send(
        to=reply_to,
        body=body,
        subject=subject or f"re: {parent.get('subject', '')}",
        from_=reader_identity,
        thread_id=thread_id,
        project_dir=project_dir or None,
    )
    await _client.inbox_ack(
        message_id=message_id,
        reader_identity=reader_identity,
        project_dir=project_dir or None,
    )
    head = f"Replied to {message_id} → {reply_to} (thread {thread_id}); sent {send_result['id']}"
    return _append_guidance(head, send_result)


@mcp.tool()
async def memory_inbox_archive(
    message_id: str,
    project_dir: str = "",
) -> str:
    """Archive an inbox message globally (hidden from all readers). Response
    includes archive-vs-ack semantics.
    """
    reader_identity, _ = compute_identity(project_dir or None)
    try:
        result = await _client.inbox_archive(
            message_id=message_id,
            reader_identity=reader_identity,
            project_dir=project_dir or None,
        )
    except Exception as e:
        return f"Archive failed: {e}"
    head = f"Archived {result['id']}"
    return _append_guidance(head, result)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
