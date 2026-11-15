"""Signal relay tests — loopback only, no outside network.

The relay's job is small and the cost of getting it wrong is concrete: a wake
that never fires leaves mail unread, and a wake that fires too often is a paid
run each time. Every test here is a claim about one of those two.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "signal_relay", Path(__file__).parent.parent / "relay.py"
)
relay = importlib.util.module_from_spec(spec)
sys.modules["signal_relay"] = relay
spec.loader.exec_module(relay)


SIG = "https://memory.example.com/memory/signal/sig_abc"
HOOK = "https://hooks.example.com/run/123"


def target(**kw):
    base = dict(name="hand", signal_url=SIG, webhook_url=HOOK, bearer="k",
                min_interval=60.0, refire_after=0.0)
    base.update(kw)
    return relay.Target(**base)


def polls(*answers):
    """A poll stub that returns the given (status, body, retry_after) in turn."""
    queue = list(answers)

    def _poll(url):
        return queue.pop(0)
    return _poll


def reading(cursor, pending):
    return (200, {"v": 1, "cursor": cursor, "pending": pending,
                  "latest_at": "2026-01-01T00:00:00+00:00"}, None)


class Hook:
    def __init__(self, *statuses):
        self.statuses = list(statuses) or [200]
        self.calls = 0

    def __call__(self, target, pending, latest_at):
        self.calls += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


# --- decide ----------------------------------------------------------------

def test_first_reading_with_mail_waiting_fires():
    assert relay.decide({}, "c1", 2, 1000, 60, 0)[0] == "fire"


def test_first_reading_with_nothing_waiting_only_records():
    assert relay.decide({}, "c1", 0, 1000, 60, 0)[0] == "record"


def test_same_cursor_never_fires_twice():
    state = {"fired_cursor": "c1", "last_fire_at": 1000}
    assert relay.decide(state, "c1", 3, 5000, 60, 0)[0] == "none"


def test_pending_alone_never_fires():
    """The loop the docs warn about: waking on pending instead of the cursor."""
    state = {"fired_cursor": "c1", "last_fire_at": 1000}
    for now in (1010, 2000, 90000):
        assert relay.decide(state, "c1", 9, now, 60, 0)[0] == "none"


def test_cursor_moved_but_already_read_records_without_firing():
    state = {"fired_cursor": "c1", "last_fire_at": 1000}
    assert relay.decide(state, "c2", 0, 5000, 60, 0)[0] == "record"


def test_burst_inside_min_interval_is_deferred_then_fires():
    state = {"fired_cursor": "c1", "last_fire_at": 1000}
    assert relay.decide(state, "c2", 1, 1030, 60, 0)[0] == "defer"
    assert relay.decide(state, "c2", 1, 1060, 60, 0)[0] == "fire"


def test_refire_is_off_by_default_and_fires_when_enabled():
    state = {"fired_cursor": "c1", "last_fire_at": 1000}
    assert relay.decide(state, "c1", 1, 1000 + 901, 60, 0)[0] == "none"
    assert relay.decide(state, "c1", 1, 1000 + 899, 60, 900)[0] == "none"
    assert relay.decide(state, "c1", 1, 1000 + 900, 60, 900)[0] == "fire"


def test_refire_never_fires_once_mail_is_read():
    state = {"fired_cursor": "c1", "last_fire_at": 1000}
    assert relay.decide(state, "c1", 0, 99999, 60, 900)[0] == "none"


def test_backoff_doubles_and_caps():
    assert relay.backoff(1) == relay.BACKOFF_START
    assert relay.backoff(2) == relay.BACKOFF_START * 2
    assert relay.backoff(50) == relay.BACKOFF_CAP


# --- step ------------------------------------------------------------------

def test_step_fires_and_records_on_success():
    hook = Hook(200)
    state, wait, line = relay.step(target(), {}, 1000, polls(reading("c1", 1)), hook)
    assert hook.calls == 1
    assert state["fired_cursor"] == "c1" and state["last_fire_at"] == 1000
    assert wait == 0 and "called the webhook" in line


def test_failed_webhook_leaves_the_wake_owed():
    """A failed call must not be recorded as delivered, or the mail is lost."""
    hook = Hook(500)
    state, wait, line = relay.step(target(), {}, 1000, polls(reading("c1", 1)), hook)
    assert "fired_cursor" not in state
    assert state["hook_failures"] == 1 and wait == relay.backoff(1)
    assert "FAILED" in line

    hook2 = Hook(200)
    state, wait, _ = relay.step(target(), state, 1010, polls(reading("c1", 1)), hook2)
    assert hook2.calls == 1 and state["fired_cursor"] == "c1"
    assert "hook_failures" not in state


def test_redirect_and_no_answer_count_as_failure():
    for status in (0, 302, 401):
        state, _, line = relay.step(
            target(), {}, 1000, polls(reading("c1", 1)), Hook(status))
        assert "fired_cursor" not in state and "FAILED" in line


def test_second_message_in_a_burst_makes_one_call():
    hook = Hook(200)
    t = target()
    state, _, _ = relay.step(t, {}, 1000, polls(reading("c1", 1)), hook)
    state, _, _ = relay.step(t, state, 1012, polls(reading("c2", 2)), hook)
    assert hook.calls == 1
    state, _, _ = relay.step(t, state, 1061, polls(reading("c2", 2)), hook)
    assert hook.calls == 2 and state["fired_cursor"] == "c2"


def test_burst_read_before_the_interval_ends_makes_no_second_call():
    hook = Hook(200)
    t = target()
    state, _, _ = relay.step(t, {}, 1000, polls(reading("c1", 1)), hook)
    state, _, _ = relay.step(t, state, 1012, polls(reading("c2", 2)), hook)
    state, _, _ = relay.step(t, state, 1061, polls(reading("c2", 0)), hook)
    assert hook.calls == 1 and state["fired_cursor"] == "c2"


def test_rate_limited_poll_waits_and_changes_nothing():
    hook = Hook(200)
    state, wait, line = relay.step(
        target(), {"fired_cursor": "c1"}, 1000, polls((429, None, 2.0)), hook)
    assert hook.calls == 0 and line is None
    assert wait >= relay.MIN_POLL_SECONDS and state == {"fired_cursor": "c1"}


def test_dead_key_is_reported_once_and_polled_slowly():
    t = target()
    state, wait, line = relay.step(t, {}, 1000, polls((404, None, None)), Hook())
    assert wait == relay.DEAD_KEY_RETRY and "404" in line
    state, wait, line = relay.step(t, state, 1300, polls((404, None, None)), Hook())
    assert wait == relay.DEAD_KEY_RETRY and line is None


def test_poll_failure_backs_off_and_recovers():
    t = target()
    hook = Hook(200)
    state, wait, line = relay.step(t, {}, 1000, polls((0, None, None)), hook)
    assert state["poll_failures"] == 1 and wait == relay.backoff(1) and line
    state, wait, line = relay.step(t, state, 1005, polls((503, None, None)), hook)
    assert state["poll_failures"] == 2 and wait == relay.backoff(2)
    state, wait, line = relay.step(t, state, 1015, polls(reading("c1", 0)), hook)
    assert "poll_failures" not in state and "reachable again" in line
    assert hook.calls == 0


def test_malformed_signal_body_is_a_failure_not_a_fire():
    hook = Hook(200)
    state, _, _ = relay.step(target(), {}, 1000, polls((200, {"v": 1}, None)), hook)
    assert hook.calls == 0 and state["poll_failures"] == 1


def test_log_lines_never_carry_a_secret():
    secret_sig = "https://memory.example.com/memory/signal/sig_SECRETKEY"
    secret_hook = "https://hooks.example.com/run/SECRETROUTINE"
    t = target(signal_url=secret_sig, webhook_url=secret_hook, bearer="SECRETBEARER")
    lines = []
    for poll_answer, hook in (
        (reading("c1", 1), Hook(200)),
        (reading("c1", 1), Hook(500)),
        ((404, None, None), Hook()),
        ((0, None, None), Hook()),
    ):
        _, _, line = relay.step(t, {}, 1000, polls(poll_answer), hook)
        lines.append(line or "")
    assert not any("SECRET" in line for line in lines)


# --- the webhook request ---------------------------------------------------

class _Resp:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


def test_webhook_request_shape(monkeypatch):
    seen = {}

    def fake_open(req, timeout):
        seen["req"] = req
        return _Resp()

    monkeypatch.setattr(relay._opener, "open", fake_open)
    status = relay.call_webhook(target(headers={"X-Routine": "r1"}), 3, "t")
    req = seen["req"]
    assert status == 204 and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer k"
    assert req.get_header("X-routine") == "r1"
    body = json.loads(req.data)
    assert body["target"] == "hand" and body["pending"] == 3
    assert set(body) == {"source", "event", "target", "pending", "latest_at"}


def test_webhook_custom_payload_and_no_bearer(monkeypatch):
    seen = {}
    monkeypatch.setattr(relay._opener, "open",
                        lambda req, timeout: seen.setdefault("req", req) and _Resp())
    relay.call_webhook(target(bearer="", payload={"text": "mail"}), 1, None)
    assert json.loads(seen["req"].data) == {"text": "mail"}
    assert seen["req"].get_header("Authorization") is None


def test_redirects_are_refused():
    handler = relay._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://x") is None


# --- config ----------------------------------------------------------------

def write_config(tmp_path, data, mode=0o600):
    path = tmp_path / "relay.json"
    path.write_text(json.dumps(data))
    os.chmod(path, mode)
    return path


def one_target(**kw):
    entry = {"name": "hand", "signal_url": SIG, "webhook_url": HOOK}
    entry.update(kw)
    return {"targets": [entry]}


def test_config_loads_with_defaults(tmp_path):
    config = relay.load_config(write_config(tmp_path, one_target()))
    t = config.targets[0]
    assert config.poll_seconds == relay.DEFAULT_POLL_SECONDS
    assert t.min_interval == relay.DEFAULT_MIN_INTERVAL
    assert t.refire_after == 0 and t.bearer == ""


def test_config_readable_by_others_is_refused(tmp_path):
    with pytest.raises(relay.ConfigError, match="chmod 600"):
        relay.load_config(write_config(tmp_path, one_target(), mode=0o644))


def test_secret_file_is_read_and_must_be_private(tmp_path):
    key = tmp_path / "hook.key"
    key.write_text("  bearer-value\n")
    os.chmod(key, 0o600)
    cfg = write_config(tmp_path, one_target(webhook_bearer_file=str(key)))
    assert relay.load_config(cfg).targets[0].bearer == "bearer-value"

    os.chmod(key, 0o640)
    with pytest.raises(relay.ConfigError, match="chmod 600"):
        relay.load_config(cfg)


def test_inline_and_file_together_is_refused(tmp_path):
    key = tmp_path / "hook.key"
    key.write_text("v")
    os.chmod(key, 0o600)
    cfg = write_config(tmp_path, one_target(
        webhook_bearer="a", webhook_bearer_file=str(key)))
    with pytest.raises(relay.ConfigError, match="not both"):
        relay.load_config(cfg)


@pytest.mark.parametrize("field", ["signal_url", "webhook_url"])
def test_plain_http_to_a_remote_host_is_refused(tmp_path, field):
    cfg = write_config(tmp_path, one_target(**{field: "http://example.com/x"}))
    with pytest.raises(relay.ConfigError, match="https"):
        relay.load_config(cfg)


def test_http_to_loopback_is_allowed(tmp_path):
    cfg = write_config(tmp_path, one_target(
        signal_url="http://localhost:8920/memory/signal/sig_abc"))
    assert relay.load_config(cfg).targets


@pytest.mark.parametrize("data,match", [
    ({"targets": []}, "non-empty"),
    ({"targets": [{"name": "a b", "signal_url": SIG, "webhook_url": HOOK}]}, "name"),
    ({"targets": [{"name": "a", "webhook_url": HOOK}]}, "signal_url"),
    ({"poll_seconds": 1, **one_target()}, "at least"),
    ({"poll_seconds": "fast", **one_target()}, "number"),
    (one_target(payload=[1]), "payload"),
    (one_target(headers={"a": 1}), "headers"),
    ({"targets": [one_target()["targets"][0]] * 2}, "duplicate"),
])
def test_bad_config_is_refused_with_a_reason(tmp_path, data, match):
    with pytest.raises(relay.ConfigError, match=match):
        relay.load_config(write_config(tmp_path, data))


def test_bad_config_exits_2_so_a_service_manager_does_not_crashloop(tmp_path, capsys):
    rc = relay.main(["--config", str(tmp_path / "missing.json")])
    assert rc == relay.EXIT_BAD_CONFIG
    assert "config error" in capsys.readouterr().err


# --- state -----------------------------------------------------------------

def test_state_round_trips_and_survives_a_corrupt_file(tmp_path):
    relay.save_state(tmp_path, "hand", {"fired_cursor": "c1"})
    assert relay.load_state(tmp_path, "hand") == {"fired_cursor": "c1"}
    (tmp_path / "hand.json").write_text("{not json")
    assert relay.load_state(tmp_path, "hand") == {}
    assert relay.load_state(tmp_path, "absent") == {}


# --- review findings (2026-11-15): each of these was a reproduced defect ----

def test_missing_or_bad_pending_is_a_failure_and_does_not_swallow_the_wake():
    t = target()
    for body in ({"v": 1, "cursor": "c9"}, {"cursor": "c9", "pending": "3"},
                 {"cursor": "c9", "pending": True}, {"cursor": 9, "pending": 1}):
        hook = Hook(200)
        state, _, _ = relay.step(t, {}, 1000, polls((200, body, None)), hook)
        assert hook.calls == 0 and "fired_cursor" not in state
        assert state["poll_failures"] == 1
        state, _, _ = relay.step(t, state, 1010, polls(reading("c9", 3)), hook)
        assert hook.calls == 1


def test_no_answer_from_the_webhook_keeps_the_minimum_spacing():
    """A webhook that starts the run but answers late must not be hammered."""
    state, wait, _ = relay.step(target(), {}, 1000, polls(reading("c1", 1)), Hook(0))
    assert wait >= 60.0
    state, wait, _ = relay.step(target(), {}, 1000, polls(reading("c1", 1)), Hook(500))
    assert wait == relay.backoff(1)


def test_backward_clock_does_not_stall_a_wake():
    state = {"fired_cursor": "c1", "last_fire_at": 100000}
    assert relay.decide(state, "c2", 1, 100000 - 86400, 60, 0)[0] == "fire"


def test_garbage_in_the_state_file_does_not_crash_decide():
    for bad in ("yesterday", None, [], float("inf"), float("nan"), True):
        state = {"fired_cursor": "c1", "last_fire_at": bad}
        assert relay.decide(state, "c2", 1, 5000, 60, 0)[0] == "fire"


def test_failure_count_is_cleared_once_the_mail_is_read():
    t = target()
    state = {}
    for i in range(4):
        state, _, _ = relay.step(t, state, 1000 + i, polls(reading("c1", 1)), Hook(500))
    assert state["hook_failures"] == 4
    state, _, _ = relay.step(t, state, 2000, polls(reading("c1", 0)), Hook())
    assert "hook_failures" not in state
    state, wait, _ = relay.step(t, state, 3000, polls(reading("c2", 1)), Hook(500))
    assert wait == relay.backoff(1)


def test_idle_poll_does_not_change_state():
    """An unchanged state is not rewritten to disk on every poll."""
    before = {"fired_cursor": "c1", "last_fire_at": 1000, "fires": 1}
    after, _, _ = relay.step(target(), before, 5000, polls(reading("c1", 0)), Hook())
    assert after == before


@pytest.mark.parametrize("entry,match", [
    (dict(webhook_bearer="# my key\nREALVALUE"), "whitespace"),
    (dict(webhook_url="https://hooks.example.com/run/A B"), "whitespace"),
    (dict(signal_url="https://memory.example.com/memory/signal/sig A"), "whitespace"),
    (dict(webhook_url="https://user:pw@hooks.example.com/x"), "user name"),
    (dict(webhook_url="https://hooks.example.com:notaport/x"), "valid URL"),
    (dict(headers={"X-A": "v\nInjected: 1"}), "header"),
    (dict(headers={"Bad Name": "v"}), "header"),
    (dict(refire_after_seconds=1), "refire_after_seconds"),
    (dict(refire_after_seconds=400, min_interval_seconds=900), "refire_after_seconds"),
])
def test_values_that_would_fail_inside_the_http_library_are_refused_at_load(
        tmp_path, entry, match):
    with pytest.raises(relay.ConfigError, match=match) as err:
        relay.load_config(write_config(tmp_path, one_target(**entry)))
    assert "REALVALUE" not in str(err.value) and "pw@" not in str(err.value)


def test_refire_at_or_above_the_floor_is_accepted(tmp_path):
    cfg = write_config(tmp_path, one_target(refire_after_seconds=900))
    assert relay.load_config(cfg).targets[0].refire_after == 900


def test_secret_file_with_a_comment_line_is_refused_without_echoing_it(tmp_path):
    key = tmp_path / "hook.key"
    key.write_text("# my key\nREALVALUE\n")
    os.chmod(key, 0o600)
    with pytest.raises(relay.ConfigError) as err:
        relay.load_config(write_config(tmp_path, one_target(webhook_bearer_file=str(key))))
    assert "REALVALUE" not in str(err.value)


def test_unreadable_or_non_utf8_config_is_a_config_error(tmp_path, capsys):
    path = tmp_path / "relay.json"
    path.write_bytes(b"\xff\xfe\x00bad")
    os.chmod(path, 0o600)
    assert relay.main(["--config", str(path)]) == relay.EXIT_BAD_CONFIG


def _serve(handler_body):
    """A one-thread local HTTP server; returns (base_url, hits, shutdown)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _any(self):
            hits.append((self.command, self.path, self.headers.get("Authorization")))
            handler_body(self)
        do_GET = do_POST = _any

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", hits, srv.shutdown


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_opener_does_not_follow_redirects_or_forward_the_bearer(code):
    def redirect(h):
        if h.path == "/elsewhere":
            h.send_response(200)
            h.end_headers()
            return
        h.send_response(code)
        h.send_header("Location", "/elsewhere")
        h.end_headers()

    base, hits, stop = _serve(redirect)
    try:
        t = target(signal_url=base + "/memory/signal/sig_x", webhook_url=base + "/hook")
        assert relay.call_webhook(t, 1, None) == code
        assert relay.poll_signal(t.signal_url)[0] == code
        assert not any(path == "/elsewhere" for _, path, _ in hits)
    finally:
        stop()


def test_a_non_http_answer_is_a_failure_not_a_crash():
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)

    def garbage():
        for _ in range(2):
            conn, _ = srv.accept()
            conn.recv(4096)
            conn.sendall(b"GARBAGE NOT HTTP\r\n\r\n")
            conn.close()

    threading.Thread(target=garbage, daemon=True).start()
    base = f"http://127.0.0.1:{srv.getsockname()[1]}"
    t = target(signal_url=base + "/memory/signal/sig_x", webhook_url=base + "/hook")
    assert relay.poll_signal(t.signal_url)[0] == 0
    assert relay.call_webhook(t, 1, None) == 0
    srv.close()


def test_run_survives_an_exception_in_step_and_logs_only_its_type(
        tmp_path, monkeypatch, capsys):
    calls = {"n": 0}

    def boom(target, state, now, *a, **k):
        calls["n"] += 1
        raise ValueError("Invalid header value b'Bearer SECRETBEARER'")

    monkeypatch.setattr(relay, "step", boom)
    monkeypatch.setattr(relay._signal, "signal", lambda *a: None)

    def fake_sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(relay.time, "sleep", fake_sleep)
    config = relay.Config(poll_seconds=12, state_dir=tmp_path, targets=[target()])
    with pytest.raises(KeyboardInterrupt):
        relay.run(config)
    out = capsys.readouterr().out
    assert calls["n"] == 1 and "unexpected ValueError" in out
    assert "SECRET" not in out
