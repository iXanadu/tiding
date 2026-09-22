#!/usr/bin/env python3
"""Messaging overhead for a time window, read from the store — no agent turns.

Owner, 2026-09-22: "measure improvements, and document message overhead,
without introducing message overhead." Nothing here sends mail or wakes
anyone; it reads rows engram already keeps. Pair with the launcher's own
turn/token logs for cost (the launcher's own wake-baseline script).

    scripts/messaging-cost-report.py 2026-09-22T04:04Z 2026-09-22T11:30Z [prefix ...]

prefixes (optional) limit senders to project names, e.g. `retc meidura`.

Reports, for DIRECT mail (not room-threaded):
  messages, waking share, deliveries that woke an agent (a lane address
  counts every seat that read it), lane fan-out, and follow-ups a sender
  sent before the recipient answered. Room traffic lives in the hub's
  transcript, not here.
"""

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict

import re
from datetime import datetime

import asyncpg


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _dsn() -> str:
    return os.environ.get("ENGRAM_DATABASE_URL") or "postgresql:///engram"


async def main(start: str, end: str, prefixes: list[str]) -> None:
    conn = await asyncpg.connect(_dsn())
    rows = await conn.fetch(
        """
        SELECT key, created_at, user_id, metadata
        FROM memories
        WHERE scope = 'inbox' AND key LIKE 'inbox/%'
          AND created_at >= $1::timestamptz AND created_at < $2::timestamptz
          AND coalesce(metadata->>'thread_id', '') NOT LIKE 'huddle/%'
        ORDER BY created_at
        """,
        _ts(start), _ts(end),
    )
    await conn.close()
    msgs = []
    for r in rows:
        md = r["metadata"] or {}
        if isinstance(md, str):
            md = json.loads(md)
        frm = (md.get("from") or "").lower().split("@", 1)[0]
        if prefixes and not any(frm.startswith(p) for p in prefixes):
            continue
        msgs.append({
            "at": r["created_at"], "from": frm,
            "to": (r["user_id"] or "").lower().split("@", 1)[0],
            "intent": (md.get("intent") or "").lower(),
            "readers": len(md.get("read_by") or []),
            "reply": bool(md.get("in_reply_to")),
            "key": r["key"], "parent": md.get("in_reply_to"),
        })
    waking = [m for m in msgs if m["intent"] != "fyi"]
    seats = {m["from"] for m in msgs}
    # A lane is `<project>-<provider>`: a shared address whose seats are
    # `<lane>-<n>`. Every seat on it reads (and is woken by) the message.
    lane = [m for m in waking
            if not re.search(r"-\d+$", m["to"])
            and any(re.fullmatch(re.escape(m["to"]) + r"-\d+", s) for s in seats)]
    deliveries = sum(max(1, m["readers"]) if m in lane else 1 for m in waking)
    # follow-ups: consecutive messages from A to B with no B→A in between.
    # A reply routed to a LANE (REPLY-TARGET-1, pre-fix) is an answer to the
    # parent's sender, so resolve it to that seat or it reads as no answer.
    sender_of = {m["key"]: m["from"] for m in msgs}
    pair = defaultdict(list)
    for m in msgs:
        to = m["to"]
        if m in lane and m["parent"] in sender_of:
            to = sender_of[m["parent"]]
        pair[(m["from"], to)].append(("out", m["at"]))
        pair[(to, m["from"])].append(("in", m["at"]))
    followups = 0
    for seq in pair.values():
        run = 0
        for kind, _ in sorted(seq, key=lambda x: x[1]):
            if kind == "out":
                run += 1
            else:
                followups += max(0, run - 1)
                run = 0
        followups += max(0, run - 1)
    print(f"window            {start} → {end}")
    print(f"direct messages   {len(msgs)}")
    print(f"  waking          {len(waking)}  ({len(msgs) - len(waking)} fyi)")
    print(f"  replies         {sum(m['reply'] for m in msgs)}")
    print(f"wake deliveries   {deliveries}  (lane messages counted per reader)")
    print(f"  lane fan-out    {len(lane)} messages → "
          f"{sum(max(1, m['readers']) for m in lane)} deliveries")
    print(f"follow-ups sent before an answer  {followups}")
    print("top senders       " + ", ".join(
        f"{k} {v}" for k, v in Counter(m["from"] for m in msgs).most_common(6)))


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2], [p.lower() for p in sys.argv[3:]]))
