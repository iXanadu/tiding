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
ADDRESS_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-:@]{0,127}$")


def validate_address(address: str) -> str:
    """Return the address if valid, else raise ValueError.

    Addresses must be non-empty, <=128 chars, and match ``ADDRESS_RE``.
    Reserved prefixes (``machine:``, ``topic:``) are allowed — senders may
    target them intentionally.
    """
    if not isinstance(address, str) or not address.strip():
        raise ValueError("address must be a non-empty string")
    address = address.strip()
    if not ADDRESS_RE.match(address):
        raise ValueError(f"invalid address: {address!r}")
    return address


def validate_listen_set(addresses: list[str]) -> list[str]:
    """Validate and normalize a list of addresses. Empty list is allowed."""
    if addresses is None:
        return []
    if not isinstance(addresses, list):
        raise ValueError("listen_set must be a list of strings")
    return [validate_address(a) for a in addresses]
