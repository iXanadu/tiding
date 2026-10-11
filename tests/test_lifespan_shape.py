"""Startup wiring guard. A background loop inserted above `lifespan` once
stole its @asynccontextmanager decorator: every test passed (they never run
the app's lifespan) and prod refused to start (2026-10-11, ~1-2 min down)."""
import inspect

from server import main


def test_lifespan_is_the_context_manager_and_loops_are_coroutines():
    assert not inspect.iscoroutinefunction(main.lifespan)
    assert hasattr(main.lifespan(main.app), "__aenter__")
    assert inspect.iscoroutinefunction(main._project_alias_refresh_loop)
