import os

from pydantic_settings import BaseSettings, SettingsConfigDict

IDENTITY_FILE = os.path.expanduser("~/.config/engram/identity")


class Settings(BaseSettings):
    """MCP bridge config.

    Resolution order (later wins for pydantic-settings init args, but
    env vars > .env file by default):
        1. Process env vars (e.g. ``memory_api_token`` in ~/.claude.json env)
        2. ~/.config/engram/identity  (key=value, .env-style)
        3. Field defaults below

    The identity file lets the token live outside ~/.claude.json — that
    file is prone to corruption by the CC harness, and the env block has
    surprised users by being silently rewritten. Identity file is the
    durable, user-owned location.
    """

    memory_api_url: str = "http://localhost:8920"
    memory_api_token: str = ""
    memory_namespace: str = "claude-code"
    memory_read_namespaces: str = ""  # empty = search every namespace the token can read (server resolves from the principal's perms); set a CSV only to narrow
    memory_default_scope: str = "machine"

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=IDENTITY_FILE if os.path.isfile(IDENTITY_FILE) else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
