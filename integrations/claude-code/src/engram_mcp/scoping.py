import os
import re
import socket

PROJECT_CFG_FILENAME = ".engram.cfg"
_VALID_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# Values that are syntactically valid but are NOT a real project identity — a
# generic deploy label or an obvious placeholder. A .engram.cfg carrying one of
# these is treated as UNSET: we interrogate for the real name rather than
# silently adopting the default. (Decision 2026-07-18: "if .engram.cfg is there, and
# not whatever the default value is, we use it, otherwise we seek to set it.")
# NOTE: 'admin' is deliberately absent — it is a real, intentional identity for
# maintenance sessions, not a placeholder.
_SENTINEL_NAMES = frozenset(
    {
        # generic deploy labels (mirror the /startup Step 0b guard)
        "prod",
        "dev",
        "staging",
        "main",
        "trunk",
        "current",
        "release",
        "live",
        # obvious placeholders
        "default",
        "changeme",
        "placeholder",
        "unset",
        "todo",
        "example",
        "template",
    }
)


def is_real_project_name(name: str | None) -> bool:
    """True when ``name`` is a usable declared identity, not a placeholder.

    A name that is empty, malformed, or a known sentinel (deploy label /
    placeholder) is NOT real — the caller should interrogate for the real one.
    """
    return bool(name) and name.lower() not in _SENTINEL_NAMES


class AmbiguousIdentity(Exception):
    """Raised when scope=project resolution can't auto-determine a name.

    Carries the suggested basename so the MCP tool layer can prompt the user
    with a concrete option (e.g., declare project '<basename>' or treat as
    admin). Callers catch this and either ask the user, or call
    ``write_project_cfg`` to resolve it.
    """

    def __init__(self, project_dir: str, suggested: str):
        self.project_dir = project_dir
        self.suggested = suggested
        super().__init__(
            f"Ambiguous project identity at {project_dir} (suggested: {suggested})"
        )


def write_project_cfg(directory: str, name: str) -> str:
    """Write a .engram.cfg at ``directory`` declaring ``project = <name>``.

    Validates ``name`` against the same regex the parser uses. Returns the
    path written. Raises ValueError for invalid names.
    """
    if not _VALID_NAME.match(name):
        raise ValueError(f"invalid project name: {name!r}")
    path = os.path.join(directory, PROJECT_CFG_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Canonical project identifier — git-tracked so every clone agrees on the name.\n")
        f.write(f"project = {name}\n")
    return path


def _parse_engram_cfg(path: str, key: str = "project", raw: bool = False) -> str | None:
    """Read a .engram.cfg file and return the value of ``key``, or None.

    Format: INI-ish, lines ``<key> = <value>``. ``#`` comments and blank lines
    are ignored. Quotes around the value are stripped. Values are restricted to
    ``[A-Za-z0-9._-]`` to prevent path-separator or shell injection into
    downstream user_id / inbox address. The first matching line wins; a present
    but malformed value returns None (does not fall through to other lines).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, _, value = line.partition("=")
                if k.strip().lower() != key:
                    continue
                name = value.strip().strip('"').strip("'")
                if raw:
                    # Caller validates — used for LIST-valued keys (`groups`),
                    # whose entries are checked one by one against the same
                    # charset a single name must satisfy.
                    return name
                if _VALID_NAME.match(name):
                    return name
                return None
    except OSError:
        return None
    return None


def _find_cfg_path(project_dir: str | None) -> str | None:
    """Walk up from ``project_dir`` and return the path of the first
    ``.engram.cfg`` found, or None.

    Boundary rules (shared by every key read from .engram.cfg):
    - When ``project_dir`` is under ``$HOME/projects/``, walk-up stops at
      ``$HOME/projects`` (doesn't cross into ``$HOME``).
    - Walk-up NEVER crosses ``$HOME``. ``$HOME/.engram.cfg`` is only read when
      ``project_dir`` IS ``$HOME`` — not when walked up to.
    - Paths entirely outside ``$HOME`` (server paths like ``/opt/srv/engram``)
      walk up normally.
    """
    if not project_dir or not os.path.isabs(project_dir):
        return None
    project_dir = os.path.abspath(project_dir)

    home = os.path.expanduser("~")
    projects_root = os.path.join(home, "projects")
    under_projects = project_dir == projects_root or project_dir.startswith(
        projects_root + os.sep
    )
    started_at_home = project_dir == home

    current = project_dir
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        # Only read $HOME/.engram.cfg when $HOME was the original project_dir
        # (prevents children and home-adjacent dirs like ~/Downloads from
        # silently inheriting admin identity via walk-up).
        if current != home or started_at_home:
            candidate = os.path.join(current, PROJECT_CFG_FILENAME)
            if os.path.isfile(candidate):
                return candidate
        # Boundary stops
        if under_projects and current == projects_root:
            return None
        if current == home:
            return None  # never cross $HOME
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def resolve_project_name(project_dir: str | None) -> str | None:
    """Walk up from ``project_dir`` for ``.engram.cfg`` and return ``project``.

    Returns the declared name, or None if no file is found, the file is
    malformed, ``project_dir`` is not an absolute path we can walk, or the
    declared value is a sentinel (deploy label / placeholder — treated as
    unset so the caller interrogates for the real name).
    """
    path = _find_cfg_path(project_dir)
    if not path:
        return None
    name = _parse_engram_cfg(path, "project")
    return name if is_real_project_name(name) else None


def resolve_inbox_identity(project_dir: str | None) -> str | None:
    """Walk up from ``project_dir`` for ``.engram.cfg`` and return the optional
    ``inbox_identity``, or None.

    This is the per-repo, file-driven source for a session's inbox address —
    kept SEPARATE from ``project`` so two sessions sharing one project (one
    shared scope=project memory bucket) can declare distinct inbox identities
    (e.g. ``inbox_identity = beastchat-server`` vs ``beastchat-app``). It is the
    durable equivalent of the ``ENGRAM_INBOX_IDENTITY`` env var, which still
    wins as an override. See decision/three-axes-principal-project-address.
    """
    path = _find_cfg_path(project_dir)
    return _parse_engram_cfg(path, "inbox_identity") if path else None


def resolve_inbox_groups(project_dir: str | None) -> list[str]:
    """Extra loose TEAM addresses from ``groups =`` in ``.engram.cfg``.

    Comma-separated bare names (no ``#`` sigil — these are project-style
    group addresses, not cross-project channels). Every session resolving
    this folder listens on each of them IN ADDITION to its seat and its
    project group, whatever seat a launcher injected.

    Why this exists (GROUP-1, 2026-08-12): a sub-team's folder can share its
    parent project's memory brain (``project = agentbeast``) while needing
    its own convening address (``agentbeast-app``). ``inbox_identity`` was
    that address until the seat era — a launcher-injected per-session seat
    SHADOWS it, so with three app sessions running, a peer's natural send to
    ``agentbeast-app`` reached whichever single session happened to hold
    that exact seat string, or nobody. A group must not depend on which
    session won a name.

    File-declared deliberately, unlike channels (env-only): a team address
    is bound to the CODEBASE — every session working this folder belongs on
    it, whoever launched them and however many there are. That is exactly
    the property a folder-walked file has and an env var does not.
    """
    path = _find_cfg_path(project_dir)
    value = (_parse_engram_cfg(path, "groups", raw=True) if path else None) or ""
    out: list[str] = []
    for entry in value.split(","):
        entry = entry.strip().lower()
        if (entry and not entry.startswith("#")
                and _VALID_NAME.match(entry) and entry not in out):
            out.append(entry)
    return out


def ensure_project_identity(project_dir: str | None) -> str:
    """Resolve project identity, interrogating rather than defaulting silently.

    Rules (applied in order):
      1. ``project_dir == $HOME``: auto-write ``$HOME/.engram.cfg`` with
         ``project = admin`` if missing; return ``"admin"``. (Admin is a
         deterministic, intentional identity — not a project "default value" —
         so it is the one case we still settle without asking.)
      2. Existing ``.engram.cfg`` with a REAL declared name anywhere up the
         walk-up chain wins (honors a hand-written cfg). A cfg whose value is a
         sentinel (deploy label / placeholder) is treated as unset.
      3. Otherwise raise ``AmbiguousIdentity`` so the MCP tool layer interrogates
         the user and writes ``.engram.cfg`` on their answer.

    This deliberately does NOT auto-adopt a basename for clean
    ``~/projects/<x>/`` layouts (the old "Rule 2" silent auto-write). Per the operator
    (2026-07-18, option A): never silently adopt a default — always confirm,
    then set ``.engram.cfg``. The basename is offered as the prompt's
    suggestion, not stamped without consent.

    Raises:
      AmbiguousIdentity: whenever no real ``.engram.cfg`` is found. ``suggested``
        carries the basename to offer, or "" when the basename is itself a
        sentinel (so the prompt asks for a real name instead of proposing a
        deploy label).
    """
    if not project_dir or not os.path.isabs(project_dir):
        basename = os.path.basename(project_dir or "")
        raise AmbiguousIdentity(
            project_dir or "",
            suggested=basename if is_real_project_name(basename) else "",
        )
    project_dir = os.path.abspath(project_dir)

    home = os.path.expanduser("~")
    projects_root = os.path.join(home, "projects")

    # Rule 1: at $HOME → admin (auto-write; admin is intentional, not a default)
    if project_dir == home:
        cfg = os.path.join(home, PROJECT_CFG_FILENAME)
        if not os.path.isfile(cfg):
            write_project_cfg(home, "admin")
        return "admin"

    # Rule 2: an existing cfg with a REAL name up the walk-up chain wins.
    under_projects = project_dir.startswith(projects_root + os.sep)
    current = project_dir
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        # Existing cfg check (honoring $HOME boundary)
        if current != home or project_dir == home:
            candidate = os.path.join(current, PROJECT_CFG_FILENAME)
            if os.path.isfile(candidate):
                parsed = _parse_engram_cfg(candidate)
                if is_real_project_name(parsed):
                    return parsed
                # sentinel or malformed → treat as unset, keep walking
        # Boundary stops
        if under_projects and current == projects_root:
            break
        if current == home:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # Rule 3: nothing real found — interrogate. Offer the basename only if it is
    # itself a usable name (never propose a deploy label like 'prod'/'dev').
    basename = os.path.basename(project_dir)
    raise AmbiguousIdentity(
        project_dir,
        suggested=basename if is_real_project_name(basename) else "",
    )


def resolve_scope_and_user_id(
    scope: str | None = None,
    default_scope: str = "machine",
    project_dir: str | None = None,
) -> tuple[str, str]:
    """Resolve a scope name to an engram (scope, user_id) tuple.

    Args:
        scope: One of machine, shared, project, or a custom passthrough.
        default_scope: Fallback when scope is None.
        project_dir: The caller's working directory path or basename.
            Used when scope=project to derive user_id.  Falls back to
            os.getcwd() if not provided (unreliable in MCP subprocesses).

    Resolution for ``scope=project``:
        1. Walk up from project_dir looking for ``.engram.cfg`` — if found
           and it declares ``project = <name>``, use that name.
        2. Otherwise fall back to ``basename(project_dir)``.

    Returns:
        (scope, user_id) tuple:
            machine  -> ("machine", hostname)
            shared   -> ("shared", "global")
            project  -> ("project", declared-name or dirname)
            custom   -> (custom, custom)   passthrough

    Note:
        Library function kept for back-compat. New code (Phase 4) should
        use ``resolve_partition`` which returns the (scope, user_id,
        project) triple matching the server schema.
    """
    scope = scope or default_scope

    if scope == "machine":
        hostname = socket.gethostname().split(".")[0].lower()
        return ("machine", hostname)
    elif scope == "shared":
        return ("shared", "global")
    elif scope == "project":
        declared = resolve_project_name(project_dir)
        if declared:
            return ("project", declared)
        # Library callers get a silent basename fallback (backwards-compatible).
        # MCP tool callers should call ``ensure_project_identity`` first to
        # auto-write under Rules 1/2, or surface Rule 3 prompts to the user.
        dirname = os.path.basename(project_dir) if project_dir else os.path.basename(os.getcwd())
        return ("project", dirname)
    else:
        return (scope, scope)


def resolve_partition(
    scope: str | None = None,
    default_scope: str = "machine",
    project_dir: str | None = None,
    principal_name: str | None = None,
) -> tuple[str, str, str | None]:
    """Resolve scope to the engram (scope, user_id, project) triple.

    Phase 4 of the identity model splits the partition into three columns:
    ``user_id`` always identifies the person (or the machine for
    scope=machine), and ``project`` holds the project name as a separate
    column when scope=project.

    For ``scope=project``:
        - ``user_id`` = ``principal_name`` if provided, else ``"unknown"``
        - ``project`` = name declared in ``.engram.cfg`` (walk-up), or
          the basename of ``project_dir`` as a fallback.

    For other scopes, project is None:
        - ``machine``  → (``"machine"``, hostname, None)
        - ``shared``   → (``"shared"``, ``"global"``, None)
        - other        → (scope, scope, None) — passthrough
    """
    scope = scope or default_scope

    if scope == "machine":
        hostname = socket.gethostname().split(".")[0].lower()
        return ("machine", hostname, None)
    if scope == "shared":
        return ("shared", "global", None)
    if scope == "project":
        declared = resolve_project_name(project_dir)
        if not declared:
            declared = (
                os.path.basename(project_dir)
                if project_dir
                else os.path.basename(os.getcwd())
            )
        return ("project", principal_name or "unknown", declared)
    return (scope, scope, None)
