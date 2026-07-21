# Deployment: where engram lives and how to run it as a service

## Docker (the one-command path)

`docker compose up -d` at the repo root runs the whole stack — server +
PostgreSQL/pgvector, ports bound to `127.0.0.1` only, data and the embedding
model cached in named volumes (`pgdata`, `hf-cache`). Upgrades:
`git pull && docker compose up -d --build`. To run only the database (native
server for dev / Apple-Silicon GPU embeddings): `docker compose up -d postgres`.

The rest of this page covers the **native** install, where the service runs
directly on the box.

## Where to install (short answer: anywhere)

Engram has **no required install location**. `scripts/install.sh` derives
every path from wherever the repo actually sits — it writes the service
definition (launchd plist on macOS, systemd unit on Linux) with *your* clone's
absolute paths. Clone to `~/code/engram`, `/srv/engram`, wherever.

You'll see `/opt/srv/engram` in examples and in the static templates
(`launchd/com.engram.plist`, `systemd/engram.service`). That is a
**convention, not a requirement**: a stable, user-neutral path for
"services that run on this box," kept separate from anyone's home directory
so the daemon doesn't depend on a particular user's login environment. Adopt
it if you like the tidiness; ignore it freely. The static templates exist
only as reference — `install.sh` generates its own with correct paths.

**Changing location later:** move/re-clone the repo, run
`./scripts/install.sh` again from the new location (it rewrites the service
definition), and restart. Nothing else references the old path.

## The service lifecycle

```bash
cd <your-clone>
./scripts/bootstrap-db.sh  # first time only: PostgreSQL 17 + pgvector + createdb
./scripts/install.sh    # pyenv env, deps, .env from example, service definition
./scripts/start.sh      # sudo under the hood (LaunchDaemon / systemd)
./scripts/stop.sh
./scripts/restart.sh    # after every git pull that touches server/ code
./scripts/uninstall.sh  # removes the service definition, leaves your data
```

macOS gets a **LaunchDaemon** (starts at boot, no login needed — correct for
a headless box; this is why it needs `sudo`, unlike a LaunchAgent). Linux
gets a **systemd unit** ordered after PostgreSQL. Logs land in
`<clone>/logs/`.

## The one file you configure: `.env`

`install.sh` seeds `.env` from `.env.example`. The three decisions that
matter on day one:

| Decision | Setting | Guidance |
|---|---|---|
| Database auth | `ENGRAM_DB_USER` / `ENGRAM_DB_PASSWORD` | Local peer auth: your username + empty password |
| Reachability | `ENGRAM_HOST` | Stays `127.0.0.1` until you've read the [security posture](getting-started.md#️-security-posture--read-this-before-exposing-anything) — a non-loopback bind without auth **refuses to start**, on purpose |
| Auth | `ENGRAM_REQUIRE_AUTH` + principals | Flip on before anything non-loopback; see the posture table |

## Dev alongside prod (two checkouts, one box)

A common working setup: a dev clone (e.g. `~/projects/engram`) where you
edit, and a serving clone (e.g. `/opt/srv/engram`) the service runs from.
Both can be `pip install -e` into the same virtualenvs — **the last install
wins** for import resolution, so pick one as the serving install and deploy
by `git pull` in the serving clone (+ `restart.sh` when `server/` changed;
bridge-only changes just need new sessions). Single-checkout setups skip all
of this.

## Multi-machine

One engram serves a whole fleet: run the server on one box, point every
other machine's clients at it (`memory_api_url` in each identity file).
Reachability across machines means a non-loopback bind — do it with real
auth (`ENGRAM_REQUIRE_AUTH=true` + per-provider principal tokens), or on a
genuinely private overlay network (Tailscale/WireGuard) with the explicit
`ENGRAM_ALLOW_INSECURE_BIND=true` opt-out. The [security
posture](getting-started.md#️-security-posture--read-this-before-exposing-anything)
table is the law here.
