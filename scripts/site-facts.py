#!/usr/bin/env python3
"""Write docs/site-facts.json: the facts the product site prints.

The site keeps each of these as marked text and rewrites it from this file at
publish time, so a number on the site cannot drift from the code without a
visible diff. Three kinds of fact live here:

* DERIVED — read out of the code or repo files below. Nothing to maintain:
  change the code, re-run this script, commit the result.
* MEASURED — a number someone measured on a date. It carries `measured_at`
  and is only as fresh as that date. Re-measure, edit MEASURED, re-run.
* DECLARED — wording this repo owns that no file states in one place.

`tests/test_site_facts.py` fails when the committed file disagrees with what
this script produces, so the file cannot quietly rot.

Usage:
    python3 scripts/site-facts.py            # rewrite docs/site-facts.json
    python3 scripts/site-facts.py --check    # exit 1 if it is out of date

Standard library only, and it imports nothing from the server: the values are
parsed from source, so it runs without the project's dependencies installed.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "site-facts.json"
SCHEMA = 1

# Intents the site lists as the ones an agent chooses between. The code also
# accepts the owner-only broadcast intent, which the site describes separately.
PUBLIC_INTENTS = ["fyi", "action", "proceed", "escalate"]
OWNER_ONLY_INTENTS = {"authority-directive"}

# The scopes a caller picks from. The server does not enumerate these (scope
# is a free string with a few reserved values), so this list is declared here;
# the reserved mail scope is appended from the code.
USER_SCOPES = ["shared", "project", "machine", "user"]

# The model snapshot the size below was measured on. If the pinned revision in
# server/config.py moves, the size is stale and build() refuses to run until
# someone re-measures and updates both lines.
EMBED_SIZE_MEASURED_ON_REVISION = "e9b6763023c676ca8431644204f50c2b100d9aab"

# id -> (display, measured_at). Sources and method are in the comments, since
# the file itself carries only what the site prints.
MEASURED = {
    # 522 MB on disk for the pinned snapshot (model.safetensors 521 MB).
    "embed_model_size": ("~520 MB", "2026-11-16"),
    # An idle worker at a task boundary: independent sender's message to the
    # worker acting on it. One live test.
    "wake_proceed_to_act": ("~26s", "2026-07-19"),
    # 24,469 inbox messages in the 35 days from the first one (2026-07-23).
    # Counted from rows still in the store, so a lower bound.
    "msgs_first_five_weeks": ("24,000+", "2026-11-16"),
    # 106,894 inbox messages in the store. Lower bound, same reason.
    "msgs_to_date": ("100,000+", "2026-11-16"),
    # A hosted assistant whose platform polls the mail signal: mail sent to
    # its hook seeing the change. One test; the assistant's own report.
    "poller_wake": ("13 seconds", "2026-11-15"),
    # A sleeping assistant that can only be called: mail sent to the relay's
    # webhook call (relay log), then to its run starting (its own report).
    "relay_call": ("4 seconds", "2026-11-15"),
    "relay_run_start": ("13 seconds", "2026-11-15"),
}

DECLARED = {
    # The date phrase that goes with msgs_to_date. Edit both together.
    "msgs_to_date_asof": "mid-November 2026",
}


class FactError(Exception):
    pass


def _read(rel: str) -> str:
    return (REPO / rel).read_text()


def _settings_default(name: str):
    """The literal default of one field of class Settings in server/config.py."""
    tree = ast.parse(_read("server/config.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if (isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                        and stmt.target.id == name and stmt.value is not None):
                    return ast.literal_eval(stmt.value)
    raise FactError(f"server/config.py: Settings.{name} has no literal default")


def _module_constant(rel: str, name: str):
    """A module-level literal assignment, by name."""
    for node in ast.parse(_read(rel)).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise FactError(f"{rel}: no literal constant named {name}")


def _one(pattern: str, rel: str, what: str) -> str:
    found = set(re.findall(pattern, _read(rel)))
    if len(found) != 1:
        raise FactError(
            f"{rel}: expected exactly one {what}, found {sorted(found) or 'none'}")
    return found.pop()


def _device_display(device: str) -> str:
    # The site's sentence is "embeddings run in-process on the ___ by default".
    return {"cpu": "CPU", "mps": "Apple GPU", "cuda": "GPU",
            "": "device PyTorch selects"}.get(device.lower(), device)


def _intents_display() -> str:
    in_code = set(_module_constant("server/models.py", "INBOX_INTENTS"))
    expected = set(PUBLIC_INTENTS) | OWNER_ONLY_INTENTS
    if in_code != expected:
        raise FactError(
            "server/models.py INBOX_INTENTS changed: "
            f"added {sorted(in_code - expected)}, removed {sorted(expected - in_code)}. "
            "Decide whether the site should list it, then update PUBLIC_INTENTS "
            "or OWNER_ONLY_INTENTS in scripts/site-facts.py.")
    return ", ".join(PUBLIC_INTENTS[:-1]) + " or " + PUBLIC_INTENTS[-1]


def _license_display() -> str:
    head = _read("LICENSE")[:400]
    if "Apache License" in head and "Version 2.0" in head:
        return "Apache-2.0"
    raise FactError("LICENSE is no longer Apache-2.0; update _license_display()")


def _install_commands() -> str:
    """The Quick Start block: the first bash block under its 'Fastest' heading."""
    text = _read("README.md")
    m = re.search(r"^### Fastest[^\n]*\n+```bash\n(.*?)\n```", text, re.S | re.M)
    if not m:
        raise FactError("README.md: Quick Start 'Fastest' bash block not found")
    return m.group(1)


def build() -> dict:
    facts: dict[str, dict] = {}

    def derived(fact_id: str, display, source: str) -> None:
        facts[fact_id] = {"display": str(display), "source": source}

    derived("port", _settings_default("port"), "server/config.py")
    derived("pg_version",
            _one(r"postgresql@(\d+)", "scripts/bootstrap-db.sh", "PostgreSQL version"),
            "scripts/bootstrap-db.sh")
    derived("embed_dims",
            _one(r"\bvector\((\d+)\)", "server/db.py", "vector dimension"),
            "server/db.py")
    derived("embed_device", _device_display(_settings_default("embed_device")),
            "server/config.py")
    derived("intents", _intents_display(), "server/models.py")
    derived("scopes",
            ", ".join(USER_SCOPES + [
                _module_constant("server/services/memory_service.py", "INBOX_SCOPE")]),
            "scripts/site-facts.py, server/services/memory_service.py")
    derived("license", _license_display(), "LICENSE")
    derived("install_commands", _install_commands(), "README.md")

    pinned = _settings_default("embed_model_revision")
    if pinned != EMBED_SIZE_MEASURED_ON_REVISION:
        raise FactError(
            "the pinned embedding model revision changed, so embed_model_size is "
            "stale. Measure the new snapshot on disk, then update MEASURED and "
            "EMBED_SIZE_MEASURED_ON_REVISION in scripts/site-facts.py.")

    for fact_id, (display, measured_at) in MEASURED.items():
        facts[fact_id] = {"display": display, "source": "measured",
                          "measured_at": measured_at}
    for fact_id, display in DECLARED.items():
        facts[fact_id] = {"display": display, "source": "scripts/site-facts.py"}

    return {"schema": SCHEMA, "facts": dict(sorted(facts.items()))}


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    try:
        text = render()
    except FactError as e:
        print(f"site-facts: {e}", file=sys.stderr)
        return 2
    if "--check" in argv:
        if not OUT.is_file() or OUT.read_text() != text:
            print("docs/site-facts.json is out of date: run "
                  "python3 scripts/site-facts.py and commit the result",
                  file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
