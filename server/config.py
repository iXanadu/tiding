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

    # Server
    # SEC-1 secure-by-default: bind loopback unless the operator deliberately
    # opens up. Non-loopback WITHOUT auth refuses to boot unless
    # allow_insecure_bind=true (trusted-network opt-out, e.g. Tailscale).
    host: str = "127.0.0.1"
    allow_insecure_bind: bool = False
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
