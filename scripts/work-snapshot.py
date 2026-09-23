#!/usr/bin/env python3
"""Work done per 30-minute slot, per team and provider — the WORK side of
"how much work does a subscription buy". Pair it with the launcher's usage
meter snapshots (same slot boundaries, UTC) to get work per 1% of meter.

Owner, 2026-09-23: "capture a snapshot of all provider usage stats ... every
30 min and store. We will use this to answer what no provider will answer."

Read-only: engram's inbox rows plus `git log` on each team repo. Sends no
mail and wakes nobody. Appends one JSON line per (slot, team, provider) to
--out and skips slots already written, so it is safe to run on a timer and
to backfill:

    scripts/work-snapshot.py --repo alpha=/path/to/alpha --repo beta=/path/to/beta \
        [--since 2026-09-22T00:00Z] [--out ~/.local/state/engram/work-snapshots.jsonl]

Per row: sends (a group send counts once), copies delivered, waking copies,
and non-merge commits with lines added/removed. Commit provider comes from
the Co-Authored-By trailer; commits without one are provider "unknown".
Only the last COMPLETE slot is ever written — a slot is final once written.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import asyncpg

SLOT = timedelta(minutes=30)
TRAILER_PROVIDER = [("anthropic", "claude"), ("openai", "codex"), ("x.ai", "grok"),
                    ("cursor", "cursor")]


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _floor(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 30, second=0, microsecond=0)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mail(rows, teams):
    """(slot, team, provider) -> counters, from inbox rows."""
    out = defaultdict(lambda: {"sends": 0, "copies": 0, "waking": 0})
    last: dict[tuple, datetime] = {}
    for r in rows:
        md = r["metadata"] or {}
        if isinstance(md, str):
            md = json.loads(md)
        frm = (md.get("from") or "").lower().split("@", 1)[0]
        team = next((t for t in teams if frm.startswith(t + "-")), None)
        if not team:
            continue
        m = re.match(re.escape(team) + r"-([a-z]+)", frm)
        k = (_floor(r["created_at"]), team, m.group(1) if m else "unknown")
        c = out[k]
        c["copies"] += 1
        if (md.get("intent") or "").lower() != "fyi":
            c["waking"] += 1
        dup = (frm, (r["value"] or "")[:400])
        if md.get("participants") and dup in last \
                and (r["created_at"] - last[dup]).total_seconds() < 5:
            continue
        last[dup] = r["created_at"]
        c["sends"] += 1
    return out


def _commits(team, repo, since, until):
    """(slot, team, provider) -> commits/added/removed, non-merge, all refs."""
    out = defaultdict(lambda: {"commits": 0, "added": 0, "removed": 0})
    log = subprocess.run(
        ["git", "-C", repo, "log", "--all", "--no-merges", "--numstat",
         f"--since={_iso(since)}", f"--until={_iso(until)}",
         "--format=@@%H|%ct|%(trailers:key=Co-Authored-By,valueonly,separator=;)"],
        capture_output=True, text=True, check=True).stdout
    seen = set()
    cur = None
    for line in log.splitlines():
        if line.startswith("@@"):
            sha, ct, trailer = line[2:].split("|", 2)
            if sha in seen:
                cur = None
                continue
            seen.add(sha)
            t = datetime.fromtimestamp(int(ct), timezone.utc)
            if not (since <= t < until):
                cur = None
                continue
            low = trailer.lower()
            prov = next((p for needle, p in TRAILER_PROVIDER if needle in low), "unknown")
            cur = out[(_floor(t), team, prov)]
            cur["commits"] += 1
        elif cur is not None and line.strip():
            a, d, _ = (line.split("\t", 2) + ["", ""])[:3]
            cur["added"] += int(a) if a.isdigit() else 0
            cur["removed"] += int(d) if d.isdigit() else 0
    return out


async def main(args) -> None:
    repos = dict(r.split("=", 1) for r in args.repo)
    teams = sorted(repos, key=len, reverse=True)
    out_path = os.path.expanduser(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    done = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            done = {json.loads(line)["slot"] for line in f if line.strip()}
    until = _floor(datetime.now(timezone.utc))  # last complete slot ends here
    since = _floor(_ts(args.since)) if args.since else until - SLOT
    slots = []
    t = since
    while t < until:
        if _iso(t) not in done:
            slots.append(t)
        t += SLOT
    if not slots:
        return
    lo, hi = slots[0], slots[-1] + SLOT

    conn = await asyncpg.connect(os.environ.get("ENGRAM_DATABASE_URL") or "postgresql:///engram")
    rows = await conn.fetch(
        """
        SELECT created_at, metadata, value FROM memories
        WHERE scope = 'inbox' AND key LIKE 'inbox/%'
          AND created_at >= $1 AND created_at < $2
          AND coalesce(metadata->>'thread_id', '') NOT LIKE 'huddle/%'
        ORDER BY created_at
        """, lo, hi)
    await conn.close()
    mail = _mail(rows, teams)
    work = {}
    for team, repo in repos.items():
        work.update(_commits(team, os.path.expanduser(repo), lo, hi))

    wanted = set(slots)
    keys = sorted({k for k in list(mail) + list(work) if k[0] in wanted})
    written = {k[0] for k in keys}
    with open(out_path, "a") as f:
        for k in keys:
            row = {"slot": _iso(k[0]), "slot_minutes": 30, "team": k[1], "provider": k[2]}
            row.update(mail.get(k, {"sends": 0, "copies": 0, "waking": 0}))
            row.update(work.get(k, {"commits": 0, "added": 0, "removed": 0}))
            f.write(json.dumps(row) + "\n")
        # An idle slot still gets a row, so "nothing happened" is recorded
        # rather than indistinguishable from "the collector did not run".
        for s in sorted(wanted - written):
            f.write(json.dumps({"slot": _iso(s), "slot_minutes": 30, "team": None,
                                "provider": None, "idle": True}) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", required=True, metavar="TEAM=PATH")
    ap.add_argument("--since", help="backfill from this UTC time (default: last slot)")
    ap.add_argument("--out", default="~/.local/state/engram/work-snapshots.jsonl")
    asyncio.run(main(ap.parse_args()))
