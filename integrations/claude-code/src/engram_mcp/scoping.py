import os
import re
import socket

PROJECT_CFG_FILENAME = ".engram.cfg"
_VALID_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


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


def _parse_engram_cfg(path: str) -> str | None:
    """Read a .engram.cfg file and return the declared project name, or None.

    Format: INI-ish, one line ``project = <name>``. ``#`` comments and blank
    lines are ignored. Quotes around the value are stripped. Names are
    restricted to ``[A-Za-z0-9._-]`` to prevent path-separator or shell
    injection into downstream user_id.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip().lower() != "project":
                    continue
                name = value.strip().strip('"').strip("'")
                if _VALID_NAME.match(name):
                    return name
                return None
    except OSError:
        return None
    return None


def resolve_project_name(project_dir: str | None) -> str | None:
    """Walk up from ``project_dir`` looking for ``.engram.cfg``.

    Returns the declared name, or None if no file is found, the file is
    malformed, or ``project_dir`` is not an absolute path we can walk.

    Boundary rules:
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
                return _parse_engram_cfg(candidate)
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


def _has_claude_dir(path: str) -> bool:
    """True if ``path/.claude/`` exists as a directory (project-root signal)."""
    return os.path.isdir(os.path.join(path, ".claude"))


def ensure_project_identity(project_dir: str | None) -> str:
    """Resolve project identity with auto-write for deterministic cases.

    Rules (applied in order):
      1. ``project_dir == $HOME``: auto-write ``$HOME/.engram.cfg`` with
         ``project = admin`` if missing; return ``"admin"``.
      2. ``project_dir`` is (or has a parent) under ``$HOME/projects/`` that
         contains a ``.claude/`` directory: auto-write ``.engram.cfg`` there
         with ``project = <basename>``; return that name.
      3. Existing ``.engram.cfg`` anywhere up the walk-up chain wins.
      4. No rule applies: raise ``AmbiguousIdentity``.

    Raises:
      AmbiguousIdentity: when no rule determines a name (e.g. ``/tmp/foo/``,
        ``~/Documents/HomeMaintenance/``, or ``~/projects/<x>/`` without a
        ``.claude/`` marker). Caller (MCP tool layer) should ask the user.
    """
    if not project_dir or not os.path.isabs(project_dir):
        raise AmbiguousIdentity(project_dir or "", suggested=os.path.basename(project_dir or ""))
    project_dir = os.path.abspath(project_dir)

    home = os.path.expanduser("~")
    projects_root = os.path.join(home, "projects")

    # Rule 1: at $HOME → admin (auto-write)
    if project_dir == home:
        cfg = os.path.join(home, PROJECT_CFG_FILENAME)
        if not os.path.isfile(cfg):
            write_project_cfg(home, "admin")
        return "admin"

    # Rule 3 precondition: existing cfg via walk-up wins over Rule 2 auto-write.
    # Walk up looking for either (a) existing cfg — return its name, OR
    # (b) a .claude/ directory under ~/projects/ — auto-write cfg there.
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
                if parsed:
                    return parsed
                # fall through to keep walking if malformed
        # Rule 2: .claude/ under ~/projects/ → auto-write cfg here
        if under_projects and current != projects_root and current.startswith(projects_root + os.sep):
            if _has_claude_dir(current):
                name = os.path.basename(current)
                if _VALID_NAME.match(name):
                    write_project_cfg(current, name)
                    return name
        # Boundary stops
        if under_projects and current == projects_root:
            break
        if current == home:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # Nothing resolved — Rule 3 territory
    raise AmbiguousIdentity(project_dir, suggested=os.path.basename(project_dir))


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
