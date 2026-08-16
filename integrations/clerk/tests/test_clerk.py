"""Clerk gate tests — pure functions only, no network.

The gates are the security model: the model proposes, these decide. Every
test here is a claim about what an injected message can and cannot achieve.
"""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "clerk", Path(__file__).parent.parent / "clerk.py"
)
clerk = importlib.util.module_from_spec(spec)
sys.modules["clerk"] = clerk
spec.loader.exec_module(clerk)


# --- parse_llm_json --------------------------------------------------------

def test_parse_plain_json():
    assert clerk.parse_llm_json('{"action": "ack"}') == {"action": "ack"}


def test_parse_fenced_json():
    out = clerk.parse_llm_json('```json\n{"action": "ignore", "reason": "spam"}\n```')
    assert out["action"] == "ignore"


def test_parse_garbage_returns_none():
    assert clerk.parse_llm_json("I think you should ack this.") is None
    assert clerk.parse_llm_json("") is None
    assert clerk.parse_llm_json("[1,2,3]") is None


# --- validate_decision -----------------------------------------------------

def test_validate_unknown_action_refused():
    d, err = clerk.validate_decision({"action": "delete_everything"})
    assert d is None and "unknown action" in err


def test_validate_reply_requires_body():
    d, err = clerk.validate_decision({"action": "reply"})
    assert d is None and "reply_body" in err


def test_validate_store_requires_key_and_value():
    d, err = clerk.validate_decision({"action": "store_shared", "store": {"key": "x"}})
    assert d is None


def test_validate_good_store_normalizes():
    d, err = clerk.validate_decision({
        "action": "store_shared", "reason": "sunset notice",
        "store": {"key": "alert/model-sunset/foo", "value": "v", "tags": "alert"},
    })
    assert err == "" and d["store"]["key"] == "alert/model-sunset/foo"


# --- store_allowed: the injection containment ------------------------------

def test_store_refused_for_untrusted_sender():
    d, _ = clerk.validate_decision({
        "action": "store_shared",
        "store": {"key": "alert/x", "value": "v"},
    })
    ok, why = clerk.store_allowed(d, "grokbot")
    assert not ok and "trusted" in why


def test_store_refused_for_missing_principal():
    """Unauthenticated envelope can never store — the forge-a-body attack."""
    d, _ = clerk.validate_decision({
        "action": "store_shared",
        "store": {"key": "alert/x", "value": "v"},
    })
    ok, _ = clerk.store_allowed(d, None)
    assert not ok


def test_store_refused_outside_prefix_allowlist():
    """Even a trusted sender cannot make the clerk write startup/next or
    wip/current — the handoff-hijack attack surface stays closed."""
    d, _ = clerk.validate_decision({
        "action": "store_shared",
        "store": {"key": "startup/next", "value": "evil handoff"},
    })
    ok, why = clerk.store_allowed(d, "ixanadu")
    assert not ok and "prefixes" in why


def test_store_allowed_for_trusted_sender_and_prefix():
    d, _ = clerk.validate_decision({
        "action": "store_shared",
        "store": {"key": "reference/model-lifecycle/x", "value": "v"},
    })
    ok, why = clerk.store_allowed(d, "IXANADU")  # case-insensitive
    assert ok, why
