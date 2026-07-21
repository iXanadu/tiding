"""Preflight doctor — pure-logic checks (the Host-allowlist coverage is the
one that encodes the 2026-07-21 fleet near-miss)."""
import pytest

from server import preflight as pf


def test_host_allowed_exact_and_wildcard():
    pats = ["localhost", "127.0.0.1", "macmini", "*.tailnet-demo.ts.net"]
    assert pf._host_allowed("macmini", pats)
    assert pf._host_allowed("macmini:8920", pats)            # port stripped
    assert pf._host_allowed("MacMini", pats)                 # case-insensitive
    assert pf._host_allowed("box.tailnet-demo.ts.net", pats)     # subdomain wildcard
    assert not pf._host_allowed("evil.com", pats)
    assert not pf._host_allowed("192.0.2.9", pats)


def test_wildcard_star_allows_all():
    assert pf._host_allowed("anything.example", ["*"])


def test_trusted_hosts_warns_when_bind_network_and_names_uncovered(monkeypatch):
    monkeypatch.setattr(pf.settings, "host", "0.0.0.0")
    monkeypatch.setattr(pf.settings, "trusted_hosts", "localhost,127.0.0.1")
    monkeypatch.setattr(pf, "_local_reachable_names", lambda: ["boxname", "192.0.2.5"])
    level, msg, fix = pf.check_trusted_hosts()
    assert level == pf.WARN
    assert "boxname" in msg and "192.0.2.5" in msg
    assert "ENGRAM_TRUSTED_HOSTS=" in fix


def test_trusted_hosts_pass_when_covered(monkeypatch):
    monkeypatch.setattr(pf.settings, "host", "0.0.0.0")
    monkeypatch.setattr(pf.settings, "trusted_hosts", "localhost,boxname,192.0.2.5")
    monkeypatch.setattr(pf, "_local_reachable_names", lambda: ["boxname", "192.0.2.5"])
    assert pf.check_trusted_hosts()[0] == pf.PASS


def test_trusted_hosts_pass_on_loopback(monkeypatch):
    monkeypatch.setattr(pf.settings, "host", "127.0.0.1")
    assert pf.check_trusted_hosts()[0] == pf.PASS


def test_bind_security_fails_insecure(monkeypatch):
    monkeypatch.setattr(pf.settings, "host", "0.0.0.0")
    monkeypatch.setattr(pf.settings, "require_auth", False)
    monkeypatch.setattr(pf.settings, "api_token", "")
    monkeypatch.setattr(pf.settings, "allow_insecure_bind", False)
    assert pf.check_bind_security()[0] == pf.FAIL


def test_bind_security_pass_loopback(monkeypatch):
    monkeypatch.setattr(pf.settings, "host", "127.0.0.1")
    assert pf.check_bind_security()[0] == pf.PASS


def test_bind_security_pass_network_with_auth(monkeypatch):
    monkeypatch.setattr(pf.settings, "host", "0.0.0.0")
    monkeypatch.setattr(pf.settings, "require_auth", True)
    assert pf.check_bind_security()[0] == pf.PASS
