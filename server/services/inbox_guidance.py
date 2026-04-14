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


def inbox_list_guidance(reader_identity: str, listen_set: list[str], msg_count: int) -> str:
    if msg_count == 0:
        return (
            "No messages right now. Polling cadence:\n"
            "  • memory_search automatically shows a 📬 INBOX banner when there is\n"
            "    unread mail — you do not need to poll memory_inbox on a timer.\n"
            "  • Call memory_inbox when the banner appears, at session startup,\n"
            "    or when the user asks you to check messages.\n"
            f"  • You are listening as '{reader_identity}' on: {listen_set}"
        )
    return (
        "Handling messages:\n"
        "  • Reply:   memory_reply(message_id, body)  — sends reply AND acks the\n"
        "             parent. Replies go to the sender's project name (not their\n"
        "             fully-qualified 'project@host'), and thread-link automatically.\n"
        "  • Ack:     memory_ack(message_id)          — mark as read without replying.\n"
        "  • Archive: memory_inbox_archive(message_id) — hide from all future views\n"
        "             (use after the thread is resolved, not just to silence it).\n"
        f"  • You are listening as '{reader_identity}' on: {listen_set}.\n"
        "  • Another session listening on the same address can still see unacked\n"
        "    messages — acks are per-reader, not global."
    )


def ack_guidance() -> str:
    return (
        "Acked. Acks are per-reader — other sessions listening on the same address\n"
        "still see this message until they ack it themselves. Use memory_inbox_archive\n"
        "to hide a message from ALL future views once the thread is fully resolved."
    )


def archive_guidance() -> str:
    return (
        "Archived. This message is now hidden from every reader's inbox queries\n"
        "(unread_only=False included). Archive is global, unlike ack which is\n"
        "per-reader."
    )
