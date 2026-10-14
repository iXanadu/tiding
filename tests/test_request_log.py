"""OBS-REQLOG-1 — the request trail, and what it deliberately does not record."""
import pytest

from server.services.request_log import (
    prune_request_log,
    record_request,
    should_log,
)


class TestSkipRules:
    def test_health_is_skipped(self):
        # Polled by the service manager and every uptime check on the fleet —
        # it would dominate the table and answer nothing anyone asks of it.
        assert should_log("/health") is False

    def test_static_is_skipped(self):
        assert should_log("/static/app.js") is False

    def test_memory_routes_are_logged(self):
        assert should_log("/memory/search") is True
        assert should_log("/admin/principals") is True
        assert should_log("/whoami") is True


@pytest.mark.asyncio
class TestRecording:
    async def test_records_a_row(self, db_pool):
        await record_request(
            principal="test-principal", auth_source="principal",
            method="POST", path="/memory/search", status=200, duration_ms=12,
        )
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM request_log WHERE principal='test-principal' "
                "ORDER BY id DESC LIMIT 1"
            )
        assert row is not None
        assert row["method"] == "POST"
        assert row["path"] == "/memory/search"
        assert row["status"] == 200
        assert row["auth_source"] == "principal"

    async def test_anonymous_request_records_null_principal(self, db_pool):
        await record_request(
            principal=None, auth_source="anonymous",
            method="GET", path="/whoami", status=401, duration_ms=3,
        )
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM request_log WHERE path='/whoami' "
                "ORDER BY id DESC LIMIT 1"
            )
        # A rejected credential is exactly the event the trail exists for —
        # it must not be dropped for lacking a principal.
        assert row is not None
        assert row["principal"] is None
        assert row["status"] == 401

    async def test_records_the_public_edge_flag(self, db_pool):
        """PUBLIC-SURFACE-1 has to know which requests came in from the
        internet before the allowlist is cut. The flag is the same header
        PUBLIC-SURFACE-2 keys on."""
        await record_request(
            principal="edge-caller", auth_source="principal",
            method="POST", path="/memory/search", status=200, duration_ms=5,
            via_public=True,
        )
        await record_request(
            principal="edge-caller", auth_source="principal",
            method="POST", path="/memory/search", status=200, duration_ms=5,
        )
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT via_public FROM request_log WHERE principal='edge-caller' "
                "ORDER BY id"
            )
        assert [r["via_public"] for r in rows][-2:] == [True, False]

    async def test_write_failure_never_raises(self, monkeypatch):
        """Observability must not be able to fail a request that succeeded."""
        async def boom():
            raise RuntimeError("pool is down")
        monkeypatch.setattr("server.services.request_log.get_pool", boom)
        # Must return normally, not raise.
        await record_request(
            principal="x", auth_source="principal",
            method="GET", path="/memory/get", status=200, duration_ms=1,
        )


@pytest.mark.asyncio
class TestRetention:
    async def test_prunes_only_rows_past_the_window(self, db_pool):
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM request_log")
            await conn.execute(
                "INSERT INTO request_log "
                "(principal, auth_source, method, path, status, duration_ms, created_at) "
                "VALUES ('old', 'principal', 'GET', '/memory/get', 200, 1, "
                "NOW() - INTERVAL '40 days')"
            )
            await conn.execute(
                "INSERT INTO request_log "
                "(principal, auth_source, method, path, status, duration_ms, created_at) "
                "VALUES ('recent', 'principal', 'GET', '/memory/get', 200, 1, "
                "NOW() - INTERVAL '2 days')"
            )
        removed = await prune_request_log(30)
        assert removed == 1
        async with db_pool.acquire() as conn:
            survivors = await conn.fetch("SELECT principal FROM request_log")
        assert [r["principal"] for r in survivors] == ["recent"]


@pytest.mark.asyncio
async def test_prune_runs_in_batches_until_done(db_pool):
    # WAL-TESTDB-1: the first prod run faced 2.5M overdue rows; the prune
    # must keep going past one batch, and leave in-window rows alone.
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM request_log")
        await conn.execute(
            "INSERT INTO request_log (principal, auth_source, method, path, "
            "status, duration_ms, created_at) "
            "SELECT 'old', 'principal', 'GET', '/memory/get', 200, 1, "
            "NOW() - INTERVAL '40 days' FROM generate_series(1, 25)")
        await conn.execute(
            "INSERT INTO request_log (principal, auth_source, method, path, "
            "status, duration_ms, created_at) VALUES "
            "('recent', 'principal', 'GET', '/memory/get', 200, 1, NOW())")
    assert await prune_request_log(30, batch_size=10) == 25
    async with db_pool.acquire() as conn:
        left = await conn.fetch("SELECT principal FROM request_log")
    assert [r["principal"] for r in left] == ["recent"]


def test_retention_does_not_depend_on_memory_expiry():
    # Prod runs with cleanup (memory expiry) DISABLED by owner ruling; the
    # request-log prune must still be scheduled.
    import inspect
    import server.main as main
    src = inspect.getsource(main.lifespan)
    assert "request_log_prune_loop" in src
    from server.services import cleanup_task
    assert "prune_request_log" not in inspect.getsource(
        cleanup_task.expiration_cleanup_loop)
