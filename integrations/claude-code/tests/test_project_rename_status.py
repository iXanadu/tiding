"""PROJECT-RENAME-1, bridge half: memory_status names a renamed project."""
import pytest

import engram_mcp.server as srv


class _FakeClient:
    def __init__(self, info):
        self.info = info
        self.asked = []

    async def project_info(self, name):
        self.asked.append(name)
        if isinstance(self.info, Exception):
            raise self.info
        return self.info


@pytest.mark.asyncio
async def test_renamed_project_is_named(tmp_path, monkeypatch):
    (tmp_path / ".tiding.cfg").write_text("project = oldname\n")
    fake = _FakeClient({"project": "oldname", "canonical": "newname",
                        "renamed": True, "old_names": ["oldname"]})
    monkeypatch.setattr(srv, "_client", fake)
    line = await srv._project_rename_line(str(tmp_path))
    assert fake.asked == ["oldname"]
    assert "'oldname' was RENAMED to 'newname'" in line
    assert "project = newname" in line


@pytest.mark.asyncio
async def test_current_project_says_nothing(tmp_path, monkeypatch):
    (tmp_path / ".engram.cfg").write_text("project = newname\n")
    monkeypatch.setattr(srv, "_client", _FakeClient(
        {"project": "newname", "canonical": "newname", "renamed": False,
         "old_names": ["oldname"]}))
    assert await srv._project_rename_line(str(tmp_path)) is None


@pytest.mark.asyncio
async def test_old_server_or_no_cfg_says_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "_client", _FakeClient(RuntimeError("404")))
    (tmp_path / ".engram.cfg").write_text("project = whatever\n")
    assert await srv._project_rename_line(str(tmp_path)) is None
    empty = tmp_path / "sub-no-cfg-anywhere"
    empty.mkdir()
    # walk-up finds the parent cfg; the failing client still yields silence
    assert await srv._project_rename_line(str(empty)) is None
