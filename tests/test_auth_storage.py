from pathlib import Path
import stat

import pytest

from health_sync.auth import storage


def test_replacement_is_private_before_it_becomes_visible(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    path.write_text("original")
    replace = storage.os.replace

    def inspect_replace(source, target):
        assert path.read_text() == "original"
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert Path(source).read_text() == "replacement"
        replace(source, target)

    monkeypatch.setattr(storage.os, "replace", inspect_replace)
    storage.write_private_text(path, "replacement")

    assert path.read_text() == "replacement"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("operation", ["fchmod", "replace"])
def test_failed_write_preserves_credentials_and_removes_temporary_file(
    tmp_path, monkeypatch, operation
):
    path = tmp_path / "credentials.json"
    path.write_text("original")

    def fail(*args):
        raise PermissionError("permission denied")

    monkeypatch.setattr(storage.os, operation, fail)
    with pytest.raises(PermissionError):
        storage.write_private_text(path, "replacement")

    assert path.read_text() == "original"
    assert list(tmp_path.iterdir()) == [path]
