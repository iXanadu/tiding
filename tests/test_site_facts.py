"""docs/site-facts.json must agree with the code.

The product site prints these values. If one drifts from the code here, the
site publishes something false, so the drift has to fail in this repo first.
No database: the script parses source files.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "site_facts", REPO / "scripts" / "site-facts.py")
site_facts = importlib.util.module_from_spec(spec)
sys.modules["site_facts"] = site_facts
spec.loader.exec_module(site_facts)


def test_committed_file_matches_the_code():
    committed = (REPO / "docs" / "site-facts.json").read_text()
    assert committed == site_facts.render(), (
        "docs/site-facts.json is out of date: run "
        "`python3 scripts/site-facts.py` and commit the result")


def test_schema_shape():
    data = json.loads((REPO / "docs" / "site-facts.json").read_text())
    assert data["schema"] == 1
    for fact_id, fact in data["facts"].items():
        assert isinstance(fact["display"], str) and fact["display"], fact_id
        assert fact["source"], fact_id
        assert set(fact) <= {"display", "source", "measured_at"}, fact_id
        if fact["source"] == "measured":
            assert len(fact["measured_at"]) == 10, fact_id  # an ISO date
        else:
            assert "measured_at" not in fact, fact_id


def test_derived_values_come_from_the_running_code():
    """The parsed values equal what the server actually uses."""
    from server.config import Settings
    from server.models import INBOX_INTENTS
    from server.services.memory_service import INBOX_SCOPE

    facts = site_facts.build()["facts"]
    assert facts["port"]["display"] == str(Settings.model_fields["port"].default)
    assert Settings.model_fields["embed_device"].default == "cpu"
    assert facts["embed_device"]["display"] == "CPU"
    assert facts["scopes"]["display"].endswith(INBOX_SCOPE)
    for intent in site_facts.PUBLIC_INTENTS:
        assert intent in INBOX_INTENTS and intent in facts["intents"]["display"]


def test_a_new_intent_fails_until_someone_decides(monkeypatch):
    real = site_facts._module_constant

    def with_extra(rel, name):
        value = real(rel, name)
        return value | {"urgent"} if name == "INBOX_INTENTS" else value

    monkeypatch.setattr(site_facts, "_module_constant", with_extra)
    with pytest.raises(site_facts.FactError, match="urgent"):
        site_facts.build()


def test_a_model_bump_fails_until_the_size_is_remeasured(monkeypatch):
    real = site_facts._settings_default
    monkeypatch.setattr(
        site_facts, "_settings_default",
        lambda name: "deadbeef" if name == "embed_model_revision" else real(name))
    with pytest.raises(site_facts.FactError, match="embed_model_size"):
        site_facts.build()


def test_device_display_fits_the_site_sentence():
    assert site_facts._device_display("cpu") == "CPU"
    assert site_facts._device_display("mps") == "Apple GPU"
    assert site_facts._device_display("") == "device PyTorch selects"
