"""Inbox addressing and identity helpers.

An inbox "address" is a flat string that identifies a logical recipient:

- ``engram``            — any Claude working in the engram project
- ``machine:macmini``   — any Claude on macmini (typically admin sessions)
- ``topic:refactor-x``  — anyone subscribed to that topic (future)

A running Claude session listens on a **set** of addresses (its ``listen_set``)
computed by the MCP bridge at startup from ``$CWD``, ``$HOME``, and
``hostname``. The server trusts whatever the client sends — the MCP bridge
has perfect information about its own environment, and this is internal
single-principal infrastructure.
"""

import re

RESERVED_PREFIXES = ("machine:", "topic:")
# Leading '#' marks a cross-project coalition CHANNEL (e.g. '#courseware'):
# a named address distinct from any project, that agents from different
# projects subscribe to at launch. The sigil keeps channels from colliding
# with project names in the flat address space (mirrors the reserved
# 'machine:' prefix). See docs/design/messaging-architecture.md §3.3.
ADDRESS_RE = re.compile(r"^#?[a-zA-Z0-9][a-zA-Z0-9_.\-:@]{0,127}$")


def validate_address(address: str) -> str:
    """Return the address if valid, else raise ValueError.

    Addresses must be non-empty, <=128 chars, and match ``ADDRESS_RE``.
    Reserved prefixes (``machine:``, ``topic:``) are allowed — senders may
    target them intentionally. A leading ``#`` marks a coalition channel.
    """
    if not isinstance(address, str) or not address.strip():
        raise ValueError("address must be a non-empty string")
    address = address.strip().lower()
    if not ADDRESS_RE.match(address):
        raise ValueError(f"invalid address: {address!r}")
    return address


def autocorrect_address(address: str) -> tuple[str, str | None]:
    """Validate and auto-correct common addressing mistakes.

    Returns ``(corrected_address, original_or_none)``.  When no correction
    is needed, ``original_or_none`` is ``None``.

    Corrections:
    - ``admin:host`` → ``machine:host`` (admin targeting a host)
    - ``host:project`` → ``project`` (strip host qualifier, broadcast)
    """
    clean = validate_address(address)
    if ":" not in clean:
        return clean, None
    if any(clean.startswith(p) for p in RESERVED_PREFIXES):
        return clean, None
    # Non-reserved colon — likely a mis-formatted address
    original = clean
    left, right = clean.split(":", 1)
    if left == "admin":
        return f"machine:{right}", original
    if right == "admin":
        return f"machine:{left}", original
    # Assume left is a host qualifier, right is the project name
    return right, original


def validate_listen_set(addresses: list[str]) -> list[str]:
    """Validate and normalize a list of addresses. Empty list is allowed."""
    if addresses is None:
        return []
    if not isinstance(addresses, list):
        raise ValueError("listen_set must be a list of strings")
    return [validate_address(a) for a in addresses]
