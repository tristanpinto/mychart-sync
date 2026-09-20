from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync_person.sh"


@pytest.mark.parametrize("flags,fail_provider", [([], ""), (["--dry-run", "--full"], ""), ([], "hospital-a")])
def test_batch_scopes_every_provider_and_reports_failures(tmp_path, flags, fail_provider):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / SCRIPT.name
    shutil.copy(SCRIPT, script)
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> calls.txt\n'
        f'if [[ "$5" == "{fail_provider}" ]]; then exit 7; fi\n'
        'exit 0\n'
    )
    python.chmod(0o700)
    providers = ["hospital-a", "hospital-b"]
    result = subprocess.run(["bash", str(script), "me", *providers, "--", *flags], capture_output=True, text=True)
    assert result.returncode == (1 if fail_provider else 0)
    assert (tmp_path / "calls.txt").read_text().splitlines() == [
        " ".join(["-m", "health_sync.cli", "sync", "--provider", p, "--person", "me", *flags])
        for p in providers
    ]
    if fail_provider:
        assert f"{fail_provider}:7" in result.stderr


@pytest.mark.parametrize("args", [[], ["me"], ["me", "--"], ["../me", "hospital"], ["me", "hospital", "--", "--person", "someone-else"]])
def test_invalid_args_never_start_sync(args):
    result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Syncing" not in result.stdout
