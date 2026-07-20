import os

from pydantic_settings import BaseSettings, SettingsConfigDict

ENGRAM_CONFIG_DIR = os.path.expanduser("~/.config/engram")
IDENTITY_FILE = os.path.join(ENGRAM_CONFIG_DIR, "identity")  # legacy single-identity fallback
IDENTITIES_DIR = os.path.join(ENGRAM_CONFIG_DIR, "identities")


class IdentitySelectorError(RuntimeError):
    """ENGRAM_IDENTITY named an identity file that doesn't exist.

    Deliberately loud: a misconfigured selector must never fall back to a
    different identity file and silently impersonate another principal.
    """


def _resolve_identity_file() -> tuple[str | None, str]:
    """Pick the .env-style credentials file for this process.

    Resolution (see docs/design/provider-credentials.md):
      1. ENGRAM_IDENTITY=<name>  ->  ~/.config/engram/identities/<name>
         (missing file = hard error, never a silent fallback)
      2. legacy ~/.config/engram/identity, if present
      3. none (field defaults / process env only)

    Returns (path-or-None, human-readable source label). Process env vars
    always override file values regardless of which file is chosen.
    """
    selector = os.environ.get("ENGRAM_IDENTITY", "").strip()
    if selector:
        path = os.path.join(IDENTITIES_DIR, selector)
        if not os.path.isfile(path):
            raise IdentitySelectorError(
                f"ENGRAM_IDENTITY={selector!r} but {path} does not exist. "
                f"Create it (memory_api_token=... , chmod 600) or fix the selector."
            )
        return path, f"identity '{selector}' ({path})"
    if os.path.isfile(IDENTITY_FILE):
        return IDENTITY_FILE, f"legacy identity file ({IDENTITY_FILE})"
    return None, "process env / defaults (no identity file)"


_identity_file, CONFIG_SOURCE = _resolve_identity_file()


class Settings(BaseSettings):
    """MCP bridge config.

    Resolution order (later loses; pydantic: init > env vars > env_file):
        1. Process env vars (explicit override / escape hatch)
        2. The resolved identity file — ENGRAM_IDENTITY selects
           ~/.config/engram/identities/<name>; otherwise the legacy
           ~/.config/engram/identity (key=value, .env-style)
        3. Field defaults below

    Tokens never belong in provider config files (Claude Code rewrites
    ~/.claude.json; every provider's config has its own failure mode).
    Provider configs carry at most the non-secret ENGRAM_IDENTITY selector.

    Namespace is NOT a client decision: writes are attributed by the token
    and the server canonicalizes legacy aliases. memory_namespace stays on
    its default unless you know exactly why you're overriding it.
    """

    memory_api_url: str = "http://localhost:8920"
    memory_api_token: str = ""
    memory_namespace: str = "fleet"
    memory_read_namespaces: str = ""  # empty = search every namespace the token can read (server resolves from the principal's perms); set a CSV only to narrow
    memory_default_scope: str = "machine"

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=_identity_file,
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
