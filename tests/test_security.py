"""SEC-1 secure-by-default: bind guard tests."""

import pytest

from server.main import check_bind_security


def test_loopback_tokenless_ok():
    # Personal default: loopback needs no auth
    for host in ("127.0.0.1", "localhost", "::1"):
        check_bind_security(host, False, "", False)


def test_nonloopback_without_auth_refuses():
    with pytest.raises(RuntimeError, match="REFUSING TO START"):
        check_bind_security("0.0.0.0", False, "", False)


def test_nonloopback_with_require_auth_ok():
    check_bind_security("0.0.0.0", True, "", False)


def test_nonloopback_with_api_token_ok():
    check_bind_security("0.0.0.0", False, "engram_tok", False)


def test_nonloopback_with_explicit_optout_ok():
    # Trusted-network (Tailscale) deliberate opt-out
    check_bind_security("0.0.0.0", False, "", True)
