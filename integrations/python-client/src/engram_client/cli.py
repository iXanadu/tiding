"""engram CLI — provisioning helpers for app integrations.

Currently provides ``engram principal create`` to mint a principal over the
admin API, append its token to ``~/.config/engram.keys``, and print the token
once.

Admin token resolution order: ``--admin-token`` > ``ENGRAM_ADMIN_TOKEN`` env >
the ``ENGRAM_ADMIN_KEY_LABEL`` entry (default ``admin``) in ``~/.config/engram.keys``.
Server URL: ``--url`` > ``ENGRAM_URL`` env > ``http://localhost:8920``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

KEYS_PATH = Path(os.path.expanduser("~/.config/engram.keys"))


def _admin_key_label() -> str:
    """Which label in the keys file holds the admin token (owner-configurable)."""
    return os.environ.get("ENGRAM_ADMIN_KEY_LABEL", "admin")


def _resolve_admin_token(arg_token: str | None) -> str | None:
    if arg_token:
        return arg_token
    env = os.environ.get("ENGRAM_ADMIN_TOKEN") or os.environ.get("ENGRAM_TOKEN")
    if env:
        return env
    if KEYS_PATH.exists():
        for line in KEYS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            label, _, tok = line.partition("=")
            if label.strip() == _admin_key_label():
                return tok.strip()
    return None


def _label_for(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def _append_key(label: str, token: str) -> bool:
    """Append ``label=token`` to the keys file. Returns False if the label
    already exists (does not overwrite)."""
    KEYS_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not KEYS_PATH.exists():
        KEYS_PATH.touch(mode=0o600)
    os.chmod(KEYS_PATH, 0o600)
    existing = KEYS_PATH.read_text()
    for ln in existing.splitlines():
        if ln.strip().startswith(f"{label}="):
            return False
    with KEYS_PATH.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{label}={token}\n")
    return True


def cmd_principal_create(args: argparse.Namespace) -> int:
    url = args.url.rstrip("/")
    admin = _resolve_admin_token(args.admin_token)
    if not admin:
        print(
            "error: no admin token. Use --admin-token, set ENGRAM_ADMIN_TOKEN, "
            "or keep an admin-labelled entry in ~/.config/engram.keys",
            file=sys.stderr,
        )
        return 2

    if args.admin:
        write_ns = args.write.split(",") if args.write else ["*"]
        read_ns = args.read.split(",") if args.read else ["*"]
    else:
        write_ns = args.write.split(",") if args.write else [args.name]
        read_ns = args.read.split(",") if args.read else list(write_ns)
        # primary write namespace is always readable by its owner
        for ns in write_ns:
            if ns not in read_ns:
                read_ns.append(ns)

    payload = {
        "name": args.name,
        "type": args.type,
        "is_admin": args.admin,
        "read_namespaces": read_ns,
        "write_namespaces": write_ns,
    }
    try:
        resp = httpx.post(
            f"{url}/admin/principals",
            json=payload,
            headers={"Authorization": f"Bearer {admin}"},
            timeout=30.0,
        )
    except Exception as e:  # noqa: BLE001 - surface any transport error to the user
        print(f"error: request to {url} failed: {e}", file=sys.stderr)
        return 1
    if resp.status_code >= 400:
        print(f"error: {resp.status_code} {resp.text}", file=sys.stderr)
        return 1

    data = resp.json()
    p = data.get("principal", {})
    token = data.get("raw_token")
    label = args.label or _label_for(args.name)

    print(
        f"created principal '{p.get('name')}' "
        f"(type={p.get('type')}, admin={p.get('is_admin')})"
    )
    print(f"  write: {p.get('write_namespaces')}")
    print(f"  read:  {p.get('read_namespaces')}")
    if token:
        saved = _append_key(label, token)
        print(f"\n  token (shown ONCE): {token}")
        if saved:
            print(f"  saved to {KEYS_PATH} as '{label}='")
        else:
            print(
                f"  NOT saved: label '{label}' already in {KEYS_PATH} "
                f"(use --label to choose another). Copy the token above now."
            )
    else:
        print("\n  (no token returned)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engram", description="engram provisioning CLI")
    groups = p.add_subparsers(dest="group", required=True)

    principal = groups.add_parser("principal", help="manage principals")
    actions = principal.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="create a principal and mint a token")
    create.add_argument("name", help="principal name (e.g. an app or person)")
    create.add_argument(
        "--write",
        help="comma-separated write namespaces (default: <name>)",
    )
    create.add_argument(
        "--read",
        help="comma-separated read namespaces (default: the write list)",
    )
    create.add_argument(
        "--type",
        default="agent",
        choices=["agent", "human"],
        help="principal type (default: agent; use 'human' for a real person)",
    )
    create.add_argument(
        "--admin",
        action="store_true",
        help="grant admin (*.* read/write). OFF by default — app principals are never admin.",
    )
    create.add_argument(
        "--label",
        help="label written to ~/.config/engram.keys (default: derived from name)",
    )
    create.add_argument(
        "--url",
        default=os.environ.get("ENGRAM_URL", "http://localhost:8920"),
        help="engram server URL (default: $ENGRAM_URL or http://localhost:8920)",
    )
    create.add_argument(
        "--admin-token",
        dest="admin_token",
        default=None,
        help="admin bearer token (default: $ENGRAM_ADMIN_TOKEN / $ENGRAM_TOKEN / keys file)",
    )
    create.set_defaults(func=cmd_principal_create)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
