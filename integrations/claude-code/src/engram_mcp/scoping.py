import os
import socket


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

    Returns:
        (scope, user_id) tuple:
            machine  -> ("machine", hostname)
            shared   -> ("shared", "global")
            project  -> ("project", dirname)
            custom   -> (custom, custom)   passthrough
    """
    scope = scope or default_scope

    if scope == "machine":
        hostname = socket.gethostname().split(".")[0].lower()
        return ("machine", hostname)
    elif scope == "shared":
        return ("shared", "global")
    elif scope == "project":
        dirname = os.path.basename(project_dir) if project_dir else os.path.basename(os.getcwd())
        return ("project", dirname)
    else:
        return (scope, scope)
