import os
import re
import socket

PROJECT_CFG_FILENAME = ".engram.cfg"
_VALID_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


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

    Boundary rule: when ``project_dir`` is under ``$HOME/projects/``, walk-up
    stops at ``$HOME/projects`` and does NOT cross into ``$HOME``. This prevents
    children without their own cfg from inheriting ``$HOME/.engram.cfg``
    (which declares the admin-session identity, not a default for projects).
    Paths outside ``$HOME/projects/`` (``$HOME`` itself, ``$HOME/Downloads``,
    server paths like ``/opt/srv/engram``) walk up normally.
    """
    if not project_dir or not os.path.isabs(project_dir):
        return None
    project_dir = os.path.abspath(project_dir)

    home = os.path.expanduser("~")
    projects_root = os.path.join(home, "projects")
    under_projects = project_dir == projects_root or project_dir.startswith(
        projects_root + os.sep
    )

    current = project_dir
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        candidate = os.path.join(current, PROJECT_CFG_FILENAME)
        if os.path.isfile(candidate):
            return _parse_engram_cfg(candidate)
        if under_projects and current == projects_root:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


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
        dirname = os.path.basename(project_dir) if project_dir else os.path.basename(os.getcwd())
        return ("project", dirname)
    else:
        return (scope, scope)
