"""ACCEPT-1 drivers: provider-shaped sessions and their watchers.

A "session" here is the REAL bridge (engram_mcp) running in a subprocess with
a controlled environment — real HTTP to a real server, real seat files, real
identity resolution. The only thing simulated is the MCP framing, which is
FastMCP's code, not ours: the subprocess awaits the same tool coroutines a
real session's tool calls reach.

Isolation: each session gets its own HOME (so ~/.config/engram and
~/.local/state/engram resolve inside the sandbox) and speaks only to the
harness's scratch server. Nothing here can touch prod state.

Per the ratified list (backlog/ACCEPT-1): every assertion in the tests
measures a WORLD outcome through these drivers — never the allocator's
mechanism.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# One shared token for the whole scratch stack — closer to a real deployment
# than anonymous mode (an empty Bearer is rejected, correctly), and it keeps
# the harness from ever speaking to a server that isn't its own.
ACCEPT_TOKEN = "accept-harness-scratch-token"

# The bridge lives in the cc-memory venv; the server in engram-3.12. Resolve
# the bridge python the same way the fleet does, with the stable fallbacks.
_CC_PY_CANDIDATES = [
    os.path.expanduser("~/.pyenv/versions/cc-memory-3.12/bin/python"),
    "/usr/local/pyenv/versions/cc-memory-3.12/bin/python",
]


def bridge_python() -> str:
    for p in _CC_PY_CANDIDATES:
        if os.path.exists(p):
            return p
    resolver = REPO_ROOT / "scripts" / "resolve-venv-python.sh"
    if resolver.exists():
        out = subprocess.run(
            [str(resolver), "cc-memory-3.12", "python"],
            capture_output=True, text=True, timeout=30,
        )
        cand = out.stdout.strip()
        if cand and os.path.exists(cand):
            return cand
    raise RuntimeError("no cc-memory-3.12 python found for the bridge")


def watcher_binary(home: str) -> str:
    """The real watcher, from the same venv as the bridge."""
    p = pathlib.Path(bridge_python()).parent / "engram-inbox-wait"
    if not p.exists():
        raise RuntimeError(f"engram-inbox-wait not found at {p}")
    return str(p)


# The in-subprocess runner. Reads one JSON command per line on stdin:
#   {"tool": "memory_search", "kwargs": {...}}
# and prints one framed JSON result per line:
#   RESULT {"ok": true, "value": "..."}
# The frame prefix keeps tool-call results distinguishable from any logging
# the bridge emits on stdout.
_RUNNER = r"""
import asyncio, json, sys
import engram_mcp.server as srv

async def main():
    print("READY", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            if cmd["tool"] == "__eval":
                # Harness-debug escape hatch: evaluate an expression in the
                # bridge process (sync). Never used by assertions — world
                # outcomes only — but indispensable when a run needs its
                # identity state inspected from outside.
                out = {"ok": True, "value": repr(eval(cmd["kwargs"]["expr"]))}
                print("RESULT " + json.dumps(out), flush=True)
                continue
            fn = getattr(srv, cmd["tool"])
            value = await fn(**cmd.get("kwargs", {}))
            out = {"ok": True, "value": value}
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print("RESULT " + json.dumps(out), flush=True)

asyncio.run(main())
"""


class SessionSim:
    """One provider-shaped session: the real bridge under a controlled env."""

    def __init__(
        self,
        *,
        server_url: str,
        project_dir: str,
        provider: str,
        session_key: str,
        inbox_identity: str | None = None,
        home: str | None = None,
    ):
        self.project_dir = project_dir
        self.provider = provider
        self.session_key = session_key
        self.home = home or str(
            pathlib.Path(project_dir).parent / f"home-{session_key}"
        )
        os.makedirs(os.path.join(self.home, ".config", "engram"), exist_ok=True)
        # Identity file: the watcher reads its auth here (a bare process does
        # not inherit the bridge env). Scratch server runs open, so the file
        # carries only the URL.
        with open(
            os.path.join(self.home, ".config", "engram", "identity"), "w"
        ) as f:
            f.write(f"memory_api_url={server_url}\n")
            f.write(f"memory_api_token={ACCEPT_TOKEN}\n")

        env = {
            # Minimal, deliberate environment — nothing inherited that could
            # leak prod endpoints or a real seat.
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "MEMORY_API_URL": server_url,
            "MEMORY_API_TOKEN": ACCEPT_TOKEN,
            "ENGRAM_PROVIDER": provider,
            "ENGRAM_SESSION_KEY": session_key,
        }
        if inbox_identity:
            env["ENGRAM_INBOX_IDENTITY"] = inbox_identity
        self.env = env
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 30.0) -> "SessionSim":
        self.proc = subprocess.Popen(
            [bridge_python(), "-u", "-c", _RUNNER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=self.env,
            # LAUNCH SHAPE, not convenience: identity anchors on the process's
            # startup cwd BY DESIGN (identity.py — a cross-project call must
            # not move a session's addresses out from under its watcher), and
            # every real launcher starts the session IN the project folder.
            # Spawning from the pytest cwd made the sim listen on THIS repo's
            # group and miss its own — a harness artifact that read exactly
            # like a product bug (first run of this suite, 2026-08-13).
            cwd=self.project_dir,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if line.strip() == "READY":
                return self
            if self.proc.poll() is not None:
                break
        raise RuntimeError(
            f"session {self.session_key} failed to boot: "
            f"{self.proc.stderr.read() if self.proc.stderr else ''}"
        )

    def call(self, tool: str, timeout: float = 60.0, **kwargs) -> str:
        """Drive one tool call; returns the tool's text result or raises."""
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"tool": tool, "kwargs": kwargs}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if line.startswith("RESULT "):
                out = json.loads(line[len("RESULT "):])
                if not out["ok"]:
                    raise RuntimeError(out["error"])
                return out["value"]
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"session {self.session_key} died mid-call: "
                    f"{self.proc.stderr.read() if self.proc.stderr else ''}"
                )
        raise TimeoutError(f"{tool} did not answer within {timeout}s")

    def kill(self):
        """A crash: no release, no goodbye. (A9-crash's world setup.)"""
        if self.proc:
            self.proc.kill()
            self.proc.wait(timeout=10)
            self.proc = None

    def stop(self):
        if self.proc:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


class WatcherSim:
    """The real engram-inbox-wait, armed the way a session would arm it."""

    def __init__(self, session: SessionSim):
        self.session = session
        self.proc: subprocess.Popen | None = None
        self._events: list[str] = []

    def start(self) -> "WatcherSim":
        self.proc = subprocess.Popen(
            [watcher_binary(self.session.home), "--follow",
             # The default poll interval is 45s — tuned for a session's
             # lifetime, not a test's. The A4 deadline (R-b) must exceed at
             # least one full poll, so tighten the poll rather than slacken
             # the deadline.
             "--poll-interval", "2",
             "--project-dir", self.session.project_dir],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.session.env,
            cwd=self.session.project_dir,  # same launch shape as the session
        )
        return self

    def wait_for_event(self, deadline_s: float = 20.0) -> dict | None:
        """One JSON event line, or None on deadline. R-b: the caller treats
        None as FAIL, never as skip."""
        assert self.proc and self.proc.stdout
        import select
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            r, _, _ = select.select([self.proc.stdout], [], [], 0.5)
            if not r:
                continue
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            self._events.append(line)
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait(timeout=10)
            self.proc = None


def make_project_dir(base: pathlib.Path, project: str, groups: str | None = None) -> str:
    d = base / project
    d.mkdir(parents=True, exist_ok=True)
    cfg = f"project = {project}\n"
    if groups:
        cfg += f"groups = {groups}\n"
    (d / ".engram.cfg").write_text(cfg)
    return str(d)


def cleanup_home(sim: SessionSim):
    shutil.rmtree(sim.home, ignore_errors=True)
