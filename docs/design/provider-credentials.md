# Provider credentials: one identity store, N agents

**Status:** ADOPTED 2026-07-20 · **Owner:** engram (bridge + docs)

## The problem

Every AI dev harness (Claude Code, Grok, Codex/GPT, anything MCP- or HTTP-capable)
needs three things to reach engram: a **server URL**, a **token**, and its
**identity**. Left to themselves, providers scatter these across their own config
files — and those files have provider-specific failure modes (Claude Code
rewrites `~/.claude.json` under you; every provider invents its own env-block
syntax). The result on a real multi-provider box:

- N tokens in N places, none of them audited together, rotation = a scavenger hunt.
- Hand-copied values drift (a stale namespace or read-list in one provider's
  config silently diverges from the server's truth).
- Secrets live inside files that other tooling rewrites, syncs, or backs up.

Two rules fix the whole class.

## Rule 1 — providers never choose the namespace

The namespace an agent writes to is decided by **its token** (the server
canonicalizes and enforces); it is not client configuration. No provider config
should ever set `memory_namespace` or `memory_read_namespaces`. Leave them
unset: writes land in the bridge default, reads resolve server-side from the
principal's permissions. A config that pins these can only ever be redundant or
wrong.

## Rule 2 — secrets live in the identity store, configs carry a pointer

```
~/.config/engram/                     (0700)
  identities/
    claude          # memory_api_token=engram_xxx        (0600 each)
    grok            # memory_api_token=engram_yyy
    gpt             # memory_api_token=engram_zzz
  identity          # legacy single-identity file — now a fallback/symlink
```

- Each file is `.env`-style `key=value` — deliberately trivial so **raw-HTTP
  harnesses with no MCP** can read it too (`memory_api_token=` line, done).
- A file holds that identity's **token**, and optionally `memory_api_url` and
  `memory_default_scope` if that identity wants non-defaults. Nothing else.
- The provider's own config carries exactly **one non-secret line**:

  | Provider | Where | Line |
  |---|---|---|
  | Grok | `~/.grok/config.toml` engram env block | `ENGRAM_IDENTITY = "grok"` |
  | Codex | `~/.codex/config.toml` `[mcp_servers.engram.env]` | `ENGRAM_IDENTITY = "codex"` |
  | Claude Code | *(nothing needed)* | legacy-file fallback covers it |

  Claude Code deliberately carries **zero** engram lines in `~/.claude.json`:
  that file is rewritten by its harness, so we depend on it for nothing. The
  bridge's fallback (below) resolves Claude to the legacy identity file, which
  is (or symlinks to) `identities/claude`.

- **Identity names are identities, not providers.** `claude`, `grok`, `codex`
  today; a second same-provider agent on one box (e.g. an app-specific
  session) is just another file + selector.
- Pair the selector with `ENGRAM_PROVIDER` in the same env block. The provider
  is a separate axis (it is what the roster uses to tell agents apart); a
  selector without it leaves the session reporting the back-compat default
  `claude`, which is the decorative-`providers_seen` defect over again.

## Resolution order (bridge `config.py`, also inherited by `engram-inbox-wait`)

1. **Process env vars** — explicit override, always wins (escape hatch, CI).
2. **`ENGRAM_IDENTITY=<name>`** → load `~/.config/engram/identities/<name>`.
   A selector pointing at a missing file is a **loud startup error**, not a
   silent fallback — misconfigured identity must never impersonate another.
3. **Legacy `~/.config/engram/identity`** — back-compat fallback when no
   selector is set.
4. Field defaults (`localhost:8920`, namespace `fleet`, scope `machine`).

## Truth-in-display (companion fix)

The bridge previously echoed its *configured* namespace in `memory_store`
confirmations and `memory_whoami` — even when the server canonicalized a legacy
alias to something else, producing contradictory output ("writes to X" / "can
WRITE Y only"). Fixed alongside this standard:

- `/memory/set` now returns the **canonical namespace** it actually wrote to;
  the bridge displays that (falls back to config if talking to an older server).
- `memory_whoami` reports the **config source** it resolved (env / which
  identity file) and flags a configured namespace that isn't in the token's
  write set as a probable legacy alias, with the fix.
- Tool descriptions state plainly: *you don't pick namespaces — your token does.*

## Operational notes

- **Rotation/audit:** one directory to sweep; one per-identity file to replace.
  Include `~/.config/engram/` in the box's secret-custody/backup mechanism.
- **Minting:** one principal per identity (`POST /admin/principals`), token
  shown once → straight into `identities/<name>`, `chmod 600`.
- **The watcher** (`engram-inbox-wait`) resolves identically; a non-default
  identity's watcher just sets `ENGRAM_IDENTITY=<name>` in its launch env.
- **Never in a repo, never in provider config:** tokens. The only engram line a
  provider config may carry is the selector (plus, transitionally, a URL).

## Migration (per box)

1. Deploy the bridge with selector support (git pull the serving checkout; new
   sessions pick it up — no server restart required for the bridge part).
2. For each provider with inline creds: create `identities/<name>` (token +
   any per-identity prefs), then strip the provider's env block down to the
   selector. **Remove any `memory_namespace` / `memory_read_namespaces` lines
   everywhere.**
3. Convert the legacy `identity` file into `identities/claude` + fallback
   (keep the legacy path working — existing sessions and watchers read it).
4. Verify per provider: `memory_whoami` shows the right principal, the right
   config source, and no alias warning; a store round-trips with the canonical
   namespace.
