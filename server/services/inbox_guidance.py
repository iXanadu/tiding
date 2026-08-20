"""Inbox usage guidance delivered to clients in tool responses.

This module is the canonical source of "how to use the inbox" text shown to
Claude sessions. Keeping it here — instead of in the MCP wrapper's docstrings —
means we can iterate on wording, addressing rules, and workflow hints without
forcing a Claude restart. The MCP bridge appends this text to each tool's
return value, so guidance updates land on the next call after a server restart.
"""

from __future__ import annotations


def derive_listen_set(reader_identity: str | None) -> list[str]:
    """APPROXIMATE a listen_set from a reader_identity. Fallback only.

    This is a LOWER BOUND, not the truth, and callers must say so (ADDR-1). It
    once mirrored the bridge's compute_identity(), back when a session's
    identity WAS its project name. Since seats shipped it cannot: from
    'engram-claude@macmini' there is no way to recover either the project group
    address ('engram') or channel subscriptions ('#devagents') — both of which
    the session really does receive mail on.

    Under-reporting here is not cosmetic: this text reaches an agent at the
    moment it is deciding how to address peers, and a session told it is not in
    a channel may report that to its owner as fact. Prefer the real listen_set
    from the client whenever it is supplied.

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


def send_guidance(
    to: str,
    reader_identity: str | None,
    listen_set: list[str] | None = None,
) -> str:
    """Addressing guidance echoed back to a sender.

    ``listen_set`` is the sender's REAL set as computed by its own bridge. When
    omitted (older bridge) we fall back to the approximation and label it, so a
    reader is never handed a partial list presented as complete.
    """
    approximate = not listen_set
    listen_set = listen_set or derive_listen_set(reader_identity)
    ri_display = reader_identity or "(unknown)"
    qualifier = (
        " (approximate — your bridge did not report it; a seat's project group "
        "and channels cannot be recovered from the identity string)"
        if approximate else ""
    )
    return (
        "How inbox addressing works:\n"
        f"  • You just sent to '{to}'. Recipients see this when they call memory_inbox\n"
        "    or memory_search (the 📬 banner nudges them).\n"
        "  • Addresses are flat strings, not discoverable via a directory:\n"
        "      - a project folder basename (e.g. 'engram', 'HomeBuyersCourse')\n"
        "      - 'machine:<hostname>' (e.g. 'machine:macmini') for admin sessions\n"
        "  • To learn a recipient's address: ask the user, or use the sender\n"
        "    field of a message you already received.\n"
        f"  • Your own listen_set this call: {listen_set} (as '{ri_display}')"
        f"{qualifier}.\n"
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
        open_n = (counts or {}).get("open", 0) or 0
        drained = counts and ((counts.get("resolved", 0) or 0) + (counts.get("superseded", 0) or 0))
        # "Nothing open" is only true when nothing is open. It used to print
        # whenever ANY mail had drained, so a reader with 12 open messages was
        # told "12 open" in the digest and "Nothing open" four lines later, in
        # one response. Contradicting yourself is worse than staying silent.
        tail = (
            "  • Nothing open. Resolved/superseded mail is hidden by default;\n"
            "    pass include_resolved=true to see the full history.\n"
            if drained and not open_n else ""
        )
        # An INHERITED ESTATE. Acks are per-reader and never transfer, so mail a
        # predecessor read-but-never-resolved stays open, is unacked by nobody,
        # and is invisible in this view. A successor reads "no unread" and
        # concludes nothing is waiting while real asks sit on its own addresses.
        # Measured 2026-08-20: a 27h owner question and a 44h "urgent" peer ask
        # were both sitting exactly here, at addresses whose sessions restarted.
        estate = (
            f"  • \u26a0\ufe0f  {open_n} message(s) are OPEN on your addresses and NOT\n"
            "    shown here — already acked, but by a PREDECESSOR at one of\n"
            "    these addresses, not by you. Acks are per-reader and never\n"
            "    transfer. Zero unread is NOT an empty estate: call\n"
            "    memory_inbox with unread_only=false and read them before\n"
            "    concluding nothing is waiting on you.\n"
            if open_n else ""
        )
        return (
            digest
            + "No open messages right now. Polling cadence:\n"
            "  • memory_search automatically shows a 📬 INBOX banner when there is\n"
            "    unread mail — you do not need to poll memory_inbox on a timer.\n"
            "  • Call memory_inbox when the banner appears, at session startup,\n"
            "    or when the user asks you to check messages.\n"
            "  • An empty inbox is NOT an empty room. Huddles run letters-off:\n"
            "    a room records every utterance in its TRANSCRIPT and announces\n"
            "    them by WAKES, writing no inbox rows at all. If something woke\n"
            "    you, fetch the transcript at the room id your wake note carries.\n"
            "    Concluding \"nothing arrived\" from THIS view is the documented\n"
            "    failure mode, and it has been made independently more than once.\n"
            + estate
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
