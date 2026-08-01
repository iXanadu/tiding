from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "ENGRAM_", "env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # PostgreSQL
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "engram"
    db_user: str = "engram"
    db_password: str = "engram"

    # Embeddings
    embed_model: str = "nomic-ai/nomic-embed-text-v1.5"
    # Pinned HF revision (2026-07-21 audit): without it, an online (re)fetch
    # trusts whatever the hub serves for 'main' — with trust_remote_code that
    # means arbitrary code. Empty string = unpinned (deliberate upgrades only).
    embed_model_revision: str = "e9b6763023c676ca8431644204f50c2b100d9aab"

    # Server
    # SEC-1 secure-by-default: bind loopback unless the operator deliberately
    # opens up. Non-loopback WITHOUT auth refuses to boot unless
    # allow_insecure_bind=true (trusted-network opt-out, e.g. Tailscale).
    host: str = "127.0.0.1"
    allow_insecure_bind: bool = False
    # Host header allowlist (anti-DNS-rebinding). Comma-separated. Requests
    # whose Host isn't listed get 400 before auth/routing — a hostile web page
    # that rebinds its DNS to 127.0.0.1:8920 makes same-origin requests that
    # CORS can't stop, but a Host it can't forge to a trusted value is blocked.
    # Add your own hostname / Tailscale MagicDNS name when binding non-loopback.
    trusted_hosts: str = "localhost,127.0.0.1,[::1],::1"

    def trusted_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()]

    # Browser origins permitted to READ engram's responses cross-origin.
    # DEFAULT EMPTY — engram's clients are servers and MCP bridges, which are
    # not subject to CORS at all, so nothing legitimate needs this by default.
    #
    # It previously hardcoded `https://claude.ai` for a claude.ai skill planned
    # 2026-04-01 that was never completed (its principal wrote 0 rows in four
    # months). The grant outlived the plan, which is the general hazard: a named
    # external origin is easy to add for a specific integration and nothing ever
    # revisits it when that integration dies. Operator config with an empty
    # default means the grant has to be re-stated deliberately, by someone who
    # currently wants it.
    cors_origins: str = ""

    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # NS-1 provider-agnostic namespaces. `primary_namespace` is where inbox +
    # presence rows live. `namespace_aliases` maps legacy names to canonical
    # ones at the API boundary ("old=new,old2=new2") so clients sending an old
    # name keep working through a rename — a relabel, never a repartition.
    # Reversible: flip the alias direction and reverse the data UPDATE.
    # Default empty (NS-2, 2026-07-21): the claude-code=fleet transition alias
    # was retired after fleet verification (0 DB rows, 0 grants, all live
    # client configs canonical). Set this only while a rename is in flight.
    primary_namespace: str = "fleet"
    namespace_aliases: str = ""

    def canonical_namespace(self, ns: str | None) -> str | None:
        if not ns:
            return ns
        for pair in self.namespace_aliases.split(","):
            if "=" in pair:
                old, new = pair.split("=", 1)
                if ns.strip().lower() == old.strip().lower():
                    # Observability is the retirement gate (MEM-403 lesson:
                    # the silent rewrite hid a hardcoding client, and retiring
                    # the alias 403'd it). Grep logs for NAMESPACE-ALIAS-HIT;
                    # retire an alias only after a quiet grace window.
                    import logging
                    logging.getLogger(__name__).info(
                        "NAMESPACE-ALIAS-HIT: %r -> %r (a client still sends "
                        "the legacy name)", ns, new.strip().lower(),
                    )
                    return new.strip().lower()
        return ns

    port: int = 8920
    log_level: str = "info"

    # Optional API token — if set, all requests must include Authorization: Bearer <token>
    api_token: str = ""

    # Require principal-based auth (Phase 2 enforcement; reserved in Phase 1)
    require_auth: bool = False

    # Log warnings for unauthenticated requests (dry-run before flipping require_auth)
    warn_unauthed: bool = False

    # Expiration cleanup
    cleanup_enabled: bool = True
    cleanup_interval_hours: int = 6
    cleanup_batch_size: int = 500

    # Inbox stale auto-resolve — drains read-but-unresolved mail so the open
    # pile doesn't grow without bound (resolve is manual/optional; dormant
    # recipients never return to resolve their own mail). Only touches ALREADY
    # read + stale mail; reversible (resolve, not delete).
    inbox_autoresolve_enabled: bool = True
    inbox_autoresolve_interval_hours: int = 6
    inbox_autoresolve_after_hours: int = 72

    # Search tuning
    vector_threshold: float = 0.35
    trigram_weight: float = 0.15
    trigram_threshold: float = 0.1

    @property
    def dsn(self) -> str:
        if self.db_password:
            return f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
        return f"postgresql://{self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}"


settings = Settings()


def canonical_namespace(ns: str | None) -> str | None:
    """Module-level NS-1 canonicalizer bound to the real settings instance —
    import this by name where `settings` may be test-patched."""
    return settings.canonical_namespace(ns)
