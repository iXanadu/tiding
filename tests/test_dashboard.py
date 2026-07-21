"""Dashboard/bridge page security posture (2026-07-21 audit: CDN supply-chain)."""

import pytest


@pytest.mark.asyncio
async def test_dashboard_serves_with_csp_and_no_cdn(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    body = resp.text
    # No remote script/style origins anywhere in the page
    assert "cdn.tailwindcss.com" not in body
    assert "cdn.jsdelivr.net" not in body
    # Token must not persist in localStorage
    assert "localStorage" not in body


@pytest.mark.asyncio
async def test_bridge_serves_with_csp_and_no_cdn(client):
    resp = await client.get("/bridge")
    assert resp.status_code == 200
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")
    assert "cdn." not in resp.text
    assert "localStorage" not in resp.text


@pytest.mark.asyncio
async def test_static_assets_served_without_auth(client):
    for path in (
        "/static/alpine-3.14.9.min.js",
        "/static/dashboard.css",
        "/static/bridge.css",
    ):
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert len(resp.content) > 1000, path
