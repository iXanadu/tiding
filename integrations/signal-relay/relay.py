#!/usr/bin/env python3
"""Tiding signal relay — wakes an assistant that can be CALLED but cannot poll.

Some hosted assistants can run their own cheap poller between conversations;
they poll a mail signal URL themselves and need nothing from this file. Others
have no such thing: their machine sleeps when no run is active, and the only
way in is a trigger URL (a webhook) that something else has to call. This
sidecar is that something else. It runs on a machine that stays awake, polls
each assistant's mail signal, and calls the assistant's webhook when waking
mail arrives.

It is a sidecar on purpose. The memory server never makes an outbound call and
never holds a vendor credential; this process does both, and can be stopped
without touching the server.

What it sends: a POST with a small JSON marker. A mail signal carries no
subject, body or sender, so the relay could not leak mail content if it tried.

Standard library only — it must run on any box with python3.

SECURITY MODEL (read before editing):
- The signal URL and the webhook URL/bearer are secrets. They are read from
  the config file or from files it names, all of which must be private to the
  owner (no group/other permission bits). The relay refuses to start otherwise.
- Nothing secret is ever logged: log lines carry the target NAME and a host,
  never a URL or a header.
- Redirects are refused. A webhook that answers 3xx would otherwise receive
  the bearer at an address the operator never configured.
- Plain http is refused except to loopback.
"""

from __future__ import annotations

import argparse
import json
import os
import signal as _signal
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1"
USER_AGENT = f"tiding-signal-relay/{VERSION}"

DEFAULT_CONFIG = "~/.config/tiding/signal-relay.json"
DEFAULT_STATE_DIR = "~/.local/state/tiding-signal-relay"

# The server rate-limits a key to one poll per 2 s and asks hooks for 10-15 s.
MIN_POLL_SECONDS = 5.0
DEFAULT_POLL_SECONDS = 12.0
# One call per burst of mail: a second message inside this window rides on the
# call already made (the run it started reads the whole inbox).
DEFAULT_MIN_INTERVAL = 60.0
HTTP_TIMEOUT = 15.0
BACKOFF_START = 5.0
BACKOFF_CAP = 600.0
# A signal that answers 404 is revoked or mistyped; hammering it helps nobody.
DEAD_KEY_RETRY = 300.0
# A re-call for mail that is still unread is a paid run; never allow a tight one.
MIN_REFIRE_SECONDS = 300.0
# A signal answer is under 200 bytes; anything larger is not a signal.
MAX_BODY_BYTES = 65536

EXIT_BAD_CONFIG = 2


class ConfigError(Exception):
    pass


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


# --- config ----------------------------------------------------------------

@dataclass
class Target:
    name: str
    signal_url: str
    webhook_url: str
    bearer: str = ""
    headers: dict = field(default_factory=dict)
    payload: dict | None = None
    min_interval: float = DEFAULT_MIN_INTERVAL
    refire_after: float = 0.0


@dataclass
class Config:
    poll_seconds: float
    state_dir: Path
    targets: list[Target]


def _is_private(path: Path) -> bool:
    """True when the file is ours and only its owner can read it.

    Windows has no such permission bits; there the check is skipped and the
    operator is responsible for the file's ACL.
    """
    if os.name == "nt":
        return True
    st = path.stat()
    if st.st_uid != os.geteuid():
        return False
    return not (st.st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def _clean(value: str, what: str) -> str:
    """Refuse a secret with whitespace or control characters inside it.

    A key file with a comment line, or a URL with a stray space, would
    otherwise fail later inside the HTTP library — whose error text quotes
    the value.
    """
    value = value.strip()
    if not value:
        raise ConfigError(f"{what} is empty")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ConfigError(
            f"{what} contains whitespace or a control character — it must be "
            f"one value on one line")
    return value


def _read_secret_file(raw: str, what: str) -> str:
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ConfigError(f"{what}: file not found: {path}")
    if not _is_private(path):
        raise ConfigError(
            f"{what}: {path} must be owned by you and private — chmod 600 it")
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"{what}: cannot read {path}") from None
    return _clean(text, f"{what} ({path})")


def _secret(entry: dict, key: str, target: str, required: bool = True) -> str:
    """A secret given inline as ``key`` or by path as ``key_file``."""
    inline, by_file = entry.get(key), entry.get(f"{key}_file")
    if inline and by_file:
        raise ConfigError(f"target '{target}': give {key} or {key}_file, not both")
    if by_file:
        return _read_secret_file(str(by_file), f"target '{target}' {key}_file")
    if inline:
        return _clean(str(inline), f"target '{target}' {key}")
    if required:
        raise ConfigError(f"target '{target}': {key} (or {key}_file) is required")
    return ""


def check_url(url: str, what: str) -> None:
    """https only, except http to loopback (a relay beside its own server)."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise ConfigError(f"{what} is not a valid URL") from None
    if "@" in parts.netloc or "\\" in parts.netloc:
        raise ConfigError(f"{what} must not carry a user name or password")
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"):
        return
    raise ConfigError(f"{what} must be https (http is allowed only to loopback)")


def _number(entry: dict, key: str, default: float, minimum: float, where: str) -> float:
    raw = entry.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"{where}: {key} must be a number")
    if raw < minimum:
        raise ConfigError(f"{where}: {key} must be at least {minimum:g}")
    return float(raw)


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config not found: {path}")
    try:
        if not _is_private(path):
            raise ConfigError(
                f"{path} must be owned by you and private — chmod 600 it")
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{path} is not valid JSON (line {e.lineno}, column {e.colno})") from None
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"cannot read {path}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")

    poll = _number(data, "poll_seconds", DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS, "config")
    state_dir = Path(str(data.get("state_dir") or DEFAULT_STATE_DIR)).expanduser()

    entries = data.get("targets")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("config: 'targets' must be a non-empty list")

    targets: list[Target] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("config: each target must be an object")
        name = str(entry.get("name") or "").strip()
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            raise ConfigError(
                "config: each target needs a 'name' of letters, digits, '-' or '_'")
        if name in seen:
            raise ConfigError(f"config: duplicate target name '{name}'")
        seen.add(name)

        signal_url = _secret(entry, "signal_url", name)
        webhook_url = _secret(entry, "webhook_url", name)
        check_url(signal_url, f"target '{name}' signal_url")
        check_url(webhook_url, f"target '{name}' webhook_url")

        headers = entry.get("headers") or {}
        if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ConfigError(f"target '{name}': headers must map strings to strings")
        for hk, hv in headers.items():
            if (not hk or not all(c.isalnum() or c in "-_" for c in hk)
                    or any(ord(c) < 0x20 or ord(c) == 0x7F for c in hv)):
                raise ConfigError(
                    f"target '{name}': header '{hk[:40]}' has a bad name or a "
                    f"control character in its value")
        payload = entry.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise ConfigError(f"target '{name}': payload must be an object")

        where = f"target '{name}'"
        min_interval = _number(entry, "min_interval_seconds",
                               DEFAULT_MIN_INTERVAL, 0, where)
        refire_after = _number(entry, "refire_after_seconds", 0, 0, where)
        if refire_after and refire_after < max(MIN_REFIRE_SECONDS, min_interval):
            raise ConfigError(
                f"{where}: refire_after_seconds must be 0 (off) or at least "
                f"{max(MIN_REFIRE_SECONDS, min_interval):g} — every re-call is "
                f"a run someone pays for")
        targets.append(Target(
            name=name,
            signal_url=signal_url,
            webhook_url=webhook_url,
            bearer=_secret(entry, "webhook_bearer", name, required=False),
            headers=dict(headers),
            payload=payload,
            min_interval=min_interval,
            refire_after=refire_after,
        ))
    return Config(poll_seconds=poll, state_dir=state_dir, targets=targets)


# --- state -----------------------------------------------------------------

def load_state(state_dir: Path, name: str) -> dict:
    try:
        data = json.loads((state_dir / f"{name}.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_dir: Path, name: str, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{name}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    os.replace(tmp, path)


# --- the decision (pure) ---------------------------------------------------

def decide(state: dict, cursor: str, pending: int, now: float,
           min_interval: float, refire_after: float) -> tuple[str, str]:
    """What to do with one signal reading. Returns (action, reason).

    Actions: ``fire`` (call the webhook), ``record`` (remember this cursor,
    call nothing), ``defer`` (a call is owed, but not yet), ``none``.

    The rules, in order:
    - Nothing waiting → never call. A changed cursor with nothing pending
      means the mail was already read; remember it so it cannot fire later.
    - A cursor already called for is not called for again, unless the mail is
      still unread after ``refire_after`` seconds (off when 0): that is a wake
      that started nothing.
    - Calls are spaced ``min_interval`` apart, so a burst of mail is one call.
    """
    fired_cursor = state.get("fired_cursor")
    last_fire = _num(state.get("last_fire_at"))
    since_fire = now - last_fire if last_fire else None
    if since_fire is not None and since_fire < 0:
        since_fire = None  # the clock went backwards; do not wait for it

    if pending <= 0:
        if cursor != fired_cursor:
            return "record", "cursor moved, nothing pending"
        return "none", "nothing pending"

    if cursor == fired_cursor:
        if refire_after > 0 and since_fire is not None and since_fire >= refire_after:
            return "fire", "still unread after the re-call window"
        return "none", "already called for this cursor"

    if since_fire is not None and since_fire < min_interval:
        return "defer", "inside the minimum interval"
    return "fire", "new waking mail"


def _num(value) -> float:
    """A state value as a float; anything unusable reads as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value == value and abs(value) != float("inf") else 0.0


def backoff(failures: int) -> float:
    """Seconds to wait after the Nth consecutive failure (1-based)."""
    return min(BACKOFF_CAP, BACKOFF_START * (2 ** max(0, failures - 1)))


# --- http ------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surfaces as an HTTPError with the 3xx code


_opener = urllib.request.build_opener(_NoRedirect)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or "?"


def poll_signal(url: str) -> tuple[int, dict | None, float | None]:
    """GET the signal. Returns (status, body, retry_after); status 0 = no answer."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read(MAX_BODY_BYTES).decode("utf-8", "replace"))
            return resp.status, body if isinstance(body, dict) else None, None
    except urllib.error.HTTPError as e:
        retry = None
        try:
            retry = float(e.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            pass
        return e.code, None, retry
    except Exception:
        # Deliberately broad and silent: http.client's own errors are not
        # OSErrors, and several of them quote the URL — which holds the key.
        return 0, None, None


def call_webhook(target: Target, pending: int, latest_at: str | None) -> int:
    """POST the wake. Returns the HTTP status; 0 = no answer."""
    payload = target.payload if target.payload is not None else {
        "source": "tiding-signal-relay",
        "event": "mail",
        "target": target.name,
        "pending": pending,
        "latest_at": latest_at,
    }
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    headers.update(target.headers)
    if target.bearer:
        headers["Authorization"] = f"Bearer {target.bearer}"
    req = urllib.request.Request(
        target.webhook_url, data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        # Broad and silent for the same reason as poll_signal: the error text
        # can quote the webhook URL or the Authorization header.
        return 0


# --- one step for one target -----------------------------------------------

def step(target: Target, state: dict, now: float,
         poll=poll_signal, call=call_webhook) -> tuple[dict, float, str | None]:
    """Poll once and act. Returns (new_state, seconds_until_next, log_line).

    ``seconds_until_next`` of 0 means "the normal poll interval".
    """
    state = dict(state)
    status, body, retry_after = poll(target.signal_url)

    if status == 429:
        return state, max(retry_after or 0, MIN_POLL_SECONDS), None
    if status == 404:
        was_dead = state.get("signal_dead")
        state["signal_dead"] = True
        line = None if was_dead else (
            f"{target.name}: signal answers 404 — the key is revoked or "
            f"mistyped. Ask the operator for a new signal URL. Retrying every "
            f"{int(DEAD_KEY_RETRY)} s.")
        return state, DEAD_KEY_RETRY, line
    pending = body.get("pending") if body else None
    if (status != 200 or body is None
            or not isinstance(body.get("cursor"), str)
            or isinstance(pending, bool) or not isinstance(pending, int)):
        # A 200 without a usable cursor AND pending is not a signal reading.
        # Guessing pending=0 here would record the cursor as handled and
        # swallow the wake.
        fails = int(_num(state.get("poll_failures"))) + 1
        state["poll_failures"] = fails
        line = (f"{target.name}: signal poll failed (status {status}, "
                f"host {_host(target.signal_url)}), attempt {fails}"
                if fails in (1, 5) or fails % 50 == 0 else None)
        return state, backoff(fails), line

    recovered = state.pop("poll_failures", 0) or state.pop("signal_dead", False)
    state.pop("signal_dead", None)
    cursor = body["cursor"]
    line = f"{target.name}: signal reachable again" if recovered else None

    action, reason = decide(state, cursor, pending, now,
                            target.min_interval, target.refire_after)
    if action == "defer":
        return state, 0, line
    if action != "fire":
        # Nothing is owed any more (read, or already called for): a failure
        # count left over from earlier mail must not slow the next wake.
        state.pop("hook_failures", None)
        if action == "record":
            state["fired_cursor"] = cursor
        return state, 0, line

    hook_status = call(target, pending, body.get("latest_at"))
    if 200 <= hook_status < 300:
        state["fired_cursor"] = cursor
        state["last_fire_at"] = now
        state["fires"] = int(_num(state.get("fires"))) + 1
        state.pop("hook_failures", None)
        return state, 0, (f"{target.name}: called the webhook ({reason}; "
                          f"pending {pending}; status {hook_status})")

    # The cursor is NOT recorded: the wake is still owed and is retried.
    fails = int(_num(state.get("hook_failures"))) + 1
    state["hook_failures"] = fails
    wait = backoff(fails)
    if hook_status == 0:
        # No answer is not proof nothing happened: a webhook that starts the
        # run and answers late looks exactly like this. Keep the spacing.
        wait = max(wait, target.min_interval)
    return state, wait, (
        f"{target.name}: webhook call FAILED (status {hook_status}, host "
        f"{_host(target.webhook_url)}), attempt {fails}; retrying in "
        f"{int(wait)} s")


# --- modes -----------------------------------------------------------------

def run(config: Config) -> int:
    stop = {"now": False}

    def _stop(*_):
        stop["now"] = True

    _signal.signal(_signal.SIGTERM, _stop)
    _signal.signal(_signal.SIGINT, _stop)

    states = {t.name: load_state(config.state_dir, t.name) for t in config.targets}
    due = {t.name: 0.0 for t in config.targets}
    write_failed: set[str] = set()
    log(f"signal relay started: {len(config.targets)} target(s) "
        f"[{', '.join(t.name for t in config.targets)}], poll {config.poll_seconds:g} s")

    while not stop["now"]:
        for target in config.targets:
            if stop["now"]:
                break
            if time.monotonic() < due[target.name]:
                continue
            before = states[target.name]
            try:
                after, wait, line = step(target, before, time.time())
            except Exception as e:
                # One target's surprise must not stop the others. Only the
                # exception's TYPE is logged: its text may quote a secret.
                after, wait = before, 60.0
                line = f"{target.name}: unexpected {type(e).__name__}; retrying in 60 s"
            if line:
                log(line)
            states[target.name] = after
            if after != before:
                try:
                    save_state(config.state_dir, target.name, after)
                    write_failed.discard(target.name)
                except OSError as e:
                    if target.name not in write_failed:
                        write_failed.add(target.name)
                        log(f"{target.name}: could not write state "
                            f"({e.strerror}); a restart may repeat one call")
            due[target.name] = time.monotonic() + (wait or config.poll_seconds)
        time.sleep(1.0)

    log("signal relay stopped")
    return 0


def check(config: Config) -> int:
    """Poll every signal once and report. Calls no webhook."""
    bad = 0
    for i, target in enumerate(config.targets):
        if i:
            time.sleep(0.1)
        status, body, _ = poll_signal(target.signal_url)
        if (status == 200 and body and isinstance(body.get("cursor"), str)
                and isinstance(body.get("pending"), int)):
            print(f"ok    {target.name}: signal answers; pending "
                  f"{body.get('pending')}; latest {body.get('latest_at')}; "
                  f"webhook host {_host(target.webhook_url)}")
        else:
            bad += 1
            hint = {404: "key revoked or mistyped", 429: "polled too fast, try again",
                    0: "no answer from the server"}.get(status, "unexpected answer")
            print(f"FAIL  {target.name}: signal status {status} ({hint})")
    return 1 if bad else 0


def fire_test(config: Config, name: str) -> int:
    """Call one target's webhook once, whatever the signal says."""
    for target in config.targets:
        if target.name == name:
            status = call_webhook(target, 0, None)
            ok = 200 <= status < 300
            print(f"{'ok  ' if ok else 'FAIL'}  {name}: webhook status {status}")
            return 0 if ok else 1
    print(f"no target named '{name}'")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Call an assistant's webhook when its Tiding mail signal moves.")
    parser.add_argument(
        "--config",
        default=os.environ.get("TIDING_RELAY_CONFIG", DEFAULT_CONFIG),
        help=f"config file (default {DEFAULT_CONFIG})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="poll each signal once and report; calls no webhook")
    mode.add_argument("--fire-test", metavar="NAME",
                      help="call one target's webhook once, as a test")
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config).expanduser())
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    if args.check:
        return check(config)
    if args.fire_test:
        return fire_test(config, args.fire_test)
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
