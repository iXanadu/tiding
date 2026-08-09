"""Which MODEL is doing the work — read from the harness, never asked of the agent.

`provider` says which HARNESS is driving (see ``identity.resolve_provider``).
It used to imply the model too, because every provider ran one: claude→Anthropic,
grok→xAI, codex→OpenAI. That coincidence is gone. Measured 2026-08-09 on one box:
Claude Code transcripts carry four distinct models and 45 of 237 sessions changed
model mid-session; codex shows five; a 1-to-many harness (Cursor) can switch model
inside a single conversation. So provider no longer answers "what was thinking",
and nothing else recorded it either — for ANY provider.

TWO RULES SHAPE THIS MODULE.

1. **Read the harness, don't ask the agent.** Every supported harness that knows
   its model writes it down. The bridge reads that file. This costs no tokens, is
   not the agent's word about itself, and stays correct when the model changes
   mid-session — which the same measurement showed happening in 45 sessions.

2. **A blank must be legible as a blank.** The failure this module exists to avoid
   is a field that is right sometimes and empty otherwise with nothing saying
   which — a reader then cannot tell "ran an unknown model" from "we didn't look".
   That exact half-fed field was found on a peer's wire the same night. So every
   answer carries a SOURCE, and an unknown model is reported as unknown rather
   than defaulted. ``resolve_provider`` may fall back to a historical default
   because a wrong provider is merely imprecise; a wrong MODEL would be a false
   provenance claim, so there is no default here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Source values, ordered by how much a reader should trust them.
SOURCE_TRANSCRIPT = "transcript"  # read from the harness's own per-turn record
SOURCE_DECLARED = "declared"      # ENGRAM_MODEL, stated by whatever launched us
SOURCE_UNKNOWN = "unknown"        # nothing to read; say so rather than guess

# Only the tail matters: the CURRENT model is the last one stamped. Reading a
# fixed window keeps the cost flat as a transcript grows into the tens of MB —
# measured at 0.6 ms against a 1.2 MB file.
_TAIL_BYTES = 256 * 1024

_MODEL_RE = re.compile(r'"model"\s*:\s*"([^"]+)"')

# Claude Code writes this for turns with no model behind them (hook output,
# local synthesis). It is not a model and must never be reported as one.
_NOT_A_MODEL = {"<synthetic>", ""}

# (path, mtime, size) -> model. A transcript is append-only, so mtime+size is a
# sound cache key: any new turn changes both. Keeps repeated writes in one turn
# from re-reading the same tail.
_CACHE: dict[tuple[str, float, int], str | None] = {}


def _tail_model(path: Path) -> str | None:
    """Last real model stamped in a JSONL transcript, or None."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    try:
        with path.open("rb") as fh:
            if st.st_size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, os.SEEK_END)
            blob = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None
    found = None
    for m in _MODEL_RE.findall(blob):
        if m not in _NOT_A_MODEL:
            found = m  # keep the LAST — that is the current model
    if len(_CACHE) > 64:  # a session touches one or two transcripts, not many
        _CACHE.clear()
    _CACHE[key] = found
    return found


def _claude_transcript() -> Path | None:
    """This session's Claude Code transcript, from the env it hands the bridge.

    Claude Code spawns the bridge with ``CLAUDE_CODE_SESSION_ID`` and
    ``CLAUDE_PROJECT_DIR``, so the path is derivable exactly — no scanning a
    directory and guessing which session is ours by mtime, which would pick the
    wrong one whenever two sessions share a project folder (the co-working case
    this fleet runs constantly).
    """
    sid = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    pdir = (os.environ.get("CLAUDE_PROJECT_DIR") or "").strip()
    if not sid or not pdir:
        return None
    # Claude Code slugifies the project path by replacing separators with '-'.
    slug = pdir.replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"


def resolve_model(provider: str | None = None) -> tuple[str | None, str]:
    """Return ``(model, source)`` for the session driving this bridge.

    ``model`` is None exactly when nothing authoritative could be read; callers
    must record the source rather than substituting a default, so that "unknown"
    stays distinguishable from a real value downstream.
    """
    # An explicit declaration wins for every provider. It is the only channel
    # available to a harness that records nothing on disk — notably Cursor,
    # whose session files carry no model at all, and which passes an MCP server
    # ONLY what its config block lists (verified 2026-08-09), so a launcher
    # cannot reach the bridge through the parent environment.
    declared = (os.environ.get("ENGRAM_MODEL") or "").strip()
    if declared:
        return declared, SOURCE_DECLARED

    if provider is None:
        from .identity import resolve_provider

        provider = resolve_provider()

    if provider == "claude":
        path = _claude_transcript()
        if path is not None:
            found = _tail_model(path)
            if found:
                return found, SOURCE_TRANSCRIPT

    # Deliberately no fallback. Other harnesses either record nothing the bridge
    # can locate (Cursor) or record it too sparsely to trust as "current" (grok
    # stamps `_meta.modelId` on a handful of update records, and its actual
    # message log carries none). Those want ENGRAM_MODEL, or a reader that is
    # honest about not knowing.
    return None, SOURCE_UNKNOWN
