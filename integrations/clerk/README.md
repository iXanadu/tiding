# engram clerk — always-on API-billed mail handler (reference sidecar)

A ~350-line sidecar that gives the fleet a 24x7 knowledge processor without
making the store an actor. It long-polls `/memory/inbox/wait` (the same wake
mechanism session watchers use — no N-second polling), hands each message to
an LLM **as data**, receives a **decision object**, and executes only what
its script-side gates allow.

## Why it's deliberately thin

The rich AI-services layers (provider routing, streaming, conversation
stores) serve interactive products. The clerk keeps exactly one seam from
them: an **OpenAI-compatible chat call**, so provider swap = `base_url` +
`model` + key (xAI, OpenAI, and Anthropic's compatibility endpoint all
qualify). Plus a usage ledger (JSONL per call) and hard caps. If it ever
needs real multi-model routing, that's the day to import a shared layer.

## Security model

- Message bodies are prompt-injection surface. The model proposes; the
  script disposes. No free tool-calling.
- Shared-memory writes gate on the envelope's server-stamped
  `from_principal` (unforgeable) against a trusted-sender allow-list, and on
  a key-prefix allow-list (`reference/`, `lesson/`, `alert/`). A successful
  injection can at worst propose a row — attributed, supersedable, refused
  unless the sender is trusted.
- Tool cap: reply, store (gated), escalate-to-owner, ack. Nothing else.
- MEM-8 applies server-side regardless: the clerk cannot destroy rows it
  does not own.
- **Dry-run is the default** (`ENGRAM_CLERK_DRY_RUN=1`): decisions are
  logged, nothing is executed, nothing is acked.
- Auth failure (401/403) is fail-loud-and-exit — the BRIDGE-2 lesson; a dead
  credential must never become a silent retry hammer.

## Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `ENGRAM_CLERK_TOKEN` | — (required) | engram principal token for the clerk |
| `ENGRAM_CLERK_API_URL` | `http://localhost:8920` | engram server |
| `ENGRAM_CLERK_ADDRESS` | `clerk` | inbox address it serves |
| `ENGRAM_CLERK_OWNER_ADDRESS` | `ixanadu` | escalation target |
| `ENGRAM_CLERK_NAMESPACE` | `fleet` | namespace for shared stores |
| `ENGRAM_CLERK_LLM_BASE_URL` | — (required) | OpenAI-compatible base, e.g. `https://api.x.ai/v1` |
| `ENGRAM_CLERK_LLM_KEY` | — (required) | provider API key |
| `ENGRAM_CLERK_LLM_MODEL` | — (required) | e.g. `grok-4-6` |
| `ENGRAM_CLERK_TRUSTED_STORE_SENDERS` | `ixanadu` | csv of principals whose mail may trigger a store |
| `ENGRAM_CLERK_MAX_COMPLETION_TOKENS` | `1024` | per-call cap |
| `ENGRAM_CLERK_DAILY_CALL_CAP` | `200` | LLM calls/day; excess mail stays queued |
| `ENGRAM_CLERK_DRY_RUN` | `1` | set `0` to act |
| `ENGRAM_CLERK_STATE_DIR` | `~/.local/state/engram-clerk` | ledger, processed ids, daily counters |

## Deployment checklist (owner decisions, in order)

1. Mint a `clerk` principal — suggested grants: read on the namespaces it
   must search; write on the shared namespace ONLY if it is the
   knowledge-committer. Never admin.
2. Run in **dry-run** against real mail for a day; read
   `state/ledger.jsonl` and judge its decisions.
3. Flip `ENGRAM_CLERK_DRY_RUN=0`.
4. Install the service unit (`com.engram.clerk.plist` / `engram-clerk.service`
   templates in this directory).
5. Rotate its two credentials on the same schedule as other service tokens.

## Run

```bash
ENGRAM_CLERK_TOKEN=... ENGRAM_CLERK_LLM_BASE_URL=https://api.x.ai/v1 \
ENGRAM_CLERK_LLM_KEY=... ENGRAM_CLERK_LLM_MODEL=grok-4-6 \
python3 integrations/clerk/clerk.py
```

Tests: `pytest integrations/clerk/tests/ -v` (pure-function tests; no network).
