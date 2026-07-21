"""Inbox usage guidance delivered to clients in tool responses.

This module is the canonical source of "how to use the inbox" text shown to
Claude sessions. Keeping it here — instead of in the MCP wrapper's docstrings —
means we can iterate on wording, addressing rules, and workflow hints without
forcing a Claude restart. The MCP bridge appends this text to each tool's
return value, so guidance updates land on the next call after a server restart.
"""

from __future__ import annotations


def derive_listen_set(reader_identity: str | None) -> list[str]:
    """Reconstruct a listen_set from a reader_identity.

    Mirrors the MCP bridge's compute_identity():
      - 'project@host'  → [project, machine:host, project@host]
      - 'machine:host'  → [machine:host]
      - None / unknown  → []
    """
    if not reader_identity:
        return []
    if reader_identity.startswith("machine:"):
        return [reader_identity]
    if "@" in reader_identity:
        project, host = reader_identity.split("@", 1)
        return [project, f"machine:{host}", reader_identity]
    return [reader_identity]


def send_guidance(to: str, reader_identity: str | None) -> str:
    listen_set = derive_listen_set(reader_identity)
    ri_display = reader_identity or "(unknown)"
    return (
        "How inbox addressing works:\n"
        f"  • You just sent to '{to}'. Recipients see this when they call memory_inbox\n"
        "    or memory_search (the 📬 banner nudges them).\n"
        "  • Addresses are flat strings, not discoverable via a directory:\n"
        "      - a project folder basename (e.g. 'engram', 'HomeBuyersCourse')\n"
        "      - 'machine:<hostname>' (e.g. 'machine:macmini') for admin sessions\n"
        "  • To learn a recipient's address: ask the user, or use the sender\n"
        "    field of a message you already received.\n"
        f"  • Your own listen_set this call: {listen_set} (as '{ri_display}').\n"
        "  • Replies should use memory_reply, which handles addressing + thread\n"
        "    linking automatically. Do NOT reply by calling memory_send manually\n"
        "    with the sender's reader_identity — that won't land."
    )


def _digest_line(counts: dict | None) -> str:
    """One-line status digest: what's open vs. drained (reassurance that
    resolved/superseded mail is handled, not lost)."""
    if not counts:
        return ""
    parts = [f"{counts.get('open', 0)} open"]
    if counts.get("stale"):
        parts.append(f"{counts['stale']} stale")
    hidden = (counts.get("resolved", 0) or 0) + (counts.get("superseded", 0) or 0)
    if hidden:
        parts.append(f"{hidden} resolved/superseded hidden")
    return "📬 " + " · ".join(parts) + ".\n"


def inbox_list_guidance(
    reader_identity: str,
    listen_set: list[str],
    msg_count: int,
    stale_count: int = 0,
    counts: dict | None = None,
) -> str:
    digest = _digest_line(counts)
    if msg_count == 0:
        drained = counts and ((counts.get("resolved", 0) or 0) + (counts.get("superseded", 0) or 0))
        tail = (
            "  • Nothing open. Resolved/superseded mail is hidden by default;\n"
            "    pass include_resolved=true to see the full history.\n"
            if drained else ""
        )
        return (
            digest
            + "No open messages right now. Polling cadence:\n"
            "  • memory_search automatically shows a 📬 INBOX banner when there is\n"
            "    unread mail — you do not need to poll memory_inbox on a timer.\n"
            "  • Call memory_inbox when the banner appears, at session startup,\n"
            "    or when the user asks you to check messages.\n"
            + tail
            + f"  • You are listening as '{reader_identity}' on: {listen_set}"
        )
    stale_note = (
        f"  • ⚠️  {stale_count} message(s) are STALE (older than 72h) — coordination\n"
        "    goes out of date; VERIFY against current state before acting on them.\n"
        if stale_count else ""
    )
    return (
        digest
        + "Handling messages:\n"
        "  • Reply:    memory_reply(message_id, body)  — sends reply AND acks the\n"
        "              parent. Replies go to the sender's project name (not their\n"
        "              fully-qualified 'project@host'), and thread-link automatically.\n"
        "              GROUP CHAT: a reply to '#channel' mail goes to the CHANNEL\n"
        "              (all subscribers see it) and defaults intent=fyi so it\n"
        "              doesn't wake every peer — pass intent='action' only when\n"
        "              your reply needs the others awake.\n"
        "  • Resolve:  memory_resolve(message_id)      — close the thread so it drains\n"
        "              from the default view (kept, retrievable via include_resolved).\n"
        "              Either party may resolve once the loop is closed.\n"
        "  • Ack:      memory_ack(message_id)          — mark as read without replying.\n"
        "  • Archive:  memory_inbox_archive(message_id) — global hard-hide; prefer\n"
        "              resolve for finished threads (archive is for noise/mistakes).\n"
        + stale_note
        + f"  • You are listening as '{reader_identity}' on: {listen_set}.\n"
        "  • Default view shows only OPEN mail. Resolved/superseded has drained.\n"
        "  • Another session listening on the same address can still see unacked\n"
        "    messages — acks are per-reader, not global."
    )


def ack_guidance() -> str:
    return (
        "Acked. Acks are per-reader — other sessions listening on the same address\n"
        "still see this message until they ack it themselves. Ack only stops it\n"
        "from re-waking YOU; it does not close the thread. To drain a finished\n"
        "thread for everyone, use memory_resolve (recorded, reversible) — or\n"
        "memory_inbox_archive only for noise/mistakes."
    )


def resolve_guidance() -> str:
    return (
        "Resolved. This thread has drained from the default inbox view for every\n"
        "reader — it no longer wakes or trips fresh sessions. It is NOT deleted:\n"
        "pass include_resolved=true to memory_inbox to retrieve it. A sender\n"
        "revising guidance should instead send the replacement with supersedes=<id>\n"
        "so the stale message is marked superseded automatically."
    )


def archive_guidance() -> str:
    return (
        "Archived. This message is now hidden from every reader's inbox queries\n"
        "(unread_only=False included). Archive is global, unlike ack which is\n"
        "per-reader."
    )
