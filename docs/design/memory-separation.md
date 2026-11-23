# Separating private memory from shared knowledge

**Status: documented, not implemented.** The operator's direction
(2026-11-23) is to write this down and build nothing yet. This note records
the problem, the options, and what each would need, so the work can start
from here when it is asked for. Ledger item: `STANDBY-1` in
[BACKLOG.md](../../BACKLOG.md).

## The problem

One store holds two very different kinds of memory:

- **Shared knowledge**: lessons, fixes, project and machine state written by
  agents for other agents. It is the large majority of what the store is
  read for, and it is what an operator needs most during an outage.
- **Private memory**: a person's own notes and a household assistant's
  memories. Small, and the reason an operator keeps the store on hardware
  they own.

A third kind sits in the same table and is neither: **operational rows**.
Agent mail, wake records and the seat register are most of the rows and
most of the bytes, change constantly, and are of no use to a reader that is
not the live primary.

Two wishes pull against each other. An operator wants a second copy of the
store somewhere else, so agents can still read fleet knowledge when the
primary or its network is down. The same operator does not want private
memory, mail or credentials on a machine they do not own.

## What already separates them

Nothing new is needed to tell the kinds apart. Every row carries a
**namespace** and a **scope**, and a token is granted namespaces, not rows:

| Kind | Where it lives today |
|------|----------------------|
| Shared knowledge | the fleet's primary namespace, scopes `shared`, `machine`, `project` |
| Private memory | its own namespaces (a personal namespace, an assistant's namespace) |
| Operational rows | the primary namespace, scopes for mail, wakes and the register |
| Credentials | the principals table (token hashes), not a memory row at all |

So separation is a question of *which rows go where*, decided by namespace
and scope. It never requires reading a memory to classify it.

## Options

### A. One store, and a filtered read-only copy

Keep a single primary. Publish an **allow-list** of rows (one namespace,
the three knowledge scopes) to a second, read-only instance elsewhere.
Private namespaces, mail, register rows, the request log and the principals
table are never sent. A namespace added later is not copied until someone
adds it to the list.

- Clients do not change. Private memory never leaves the primary.
- The copy is small: knowledge is a few percent of the table.
- The copy can never be promoted to a primary. It is missing rows by
  design. A promotable standby is a different thing: a full copy, with
  everything that implies.
- Needs: a server mode that performs no writes at all (reads set a
  last-used timestamp today), a stable refusal for every write and mail
  endpoint, its own principals and tokens, a replication recipe whose row
  filter does not require logging whole rows on every update, an ordering
  rule for schema changes (the copy first), and an optional second address
  in the client bridge for reads only.

### B. Two stores, split by namespace

Run a second small instance that holds only the private namespaces. The
clients that write private memory point at it; everything else points at
the main store, which can then be copied whole.

- No filtered replication, and the main store can have a promotable
  standby.
- Two services to run, back up and upgrade. Private rows must be moved
  once. A client that writes both kinds needs both addresses.
- The whole-store copy still carries mail and credentials off the
  operator's hardware, so it only answers the privacy of *private memory*,
  not the exposure of everything else.

### C. Routing in the client

One client decides per write which store to use. This is option B with the
decision moved into every client. It is only needed for a client that
writes both kinds, and it should route by namespace, never by guessing from
content.

## What a second copy exposes

"Not personal" is not the same as "harmless". Shared knowledge describes
the fleet: machines, paths, project state. Operational rows carry every
message agents and the operator have sent each other. The principals table
carries token hashes. Before any copy leaves the operator's own hardware:

- decide which of those the copy may hold (option A's allow-list makes this
  an explicit list rather than a default);
- give the copy its own tokens, so a token taken from it does not open the
  primary;
- make sure the access rules on the mail path are enforced by the server
  and not by what a caller says about itself, since a second reachable
  instance widens who can call.

## Recommendation, when this is taken up

Option A for an outage copy: it meets the privacy wish without changing any
client, and its cost is a server read-only mode and a tested replication
recipe. Option B only if a promotable standby of the main store is also
wanted. Option C only for a client that truly writes both kinds.
