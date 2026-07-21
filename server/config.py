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

    # NS-1 provider-agnostic namespaces. `primary_namespace` is where inbox +
    # presence rows live. `namespace_aliases` maps legacy names to canonical
    # ones at the API boundary ("old=new,old2=new2") so clients sending an old
    # name keep working through a rename — a relabel, never a repartition.
    # Reversible: flip the alias direction and reverse the data UPDATE.
    primary_namespace: str = "fleet"
    namespace_aliases: str = "claude-code=fleet"

    def canonical_namespace(self, ns: str | None) -> str | None:
        if not ns:
            return ns
        for pair in self.namespace_aliases.split(","):
            if "=" in pair:
                old, new = pair.split("=", 1)
                if ns.strip().lower() == old.strip().lower():
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
