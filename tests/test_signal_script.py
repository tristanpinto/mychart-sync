import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/health_sync.sh"


@pytest.mark.skipif(shutil.which("jq") is None, reason="Optional signal wrapper requires jq")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_signal_retains_fields_and_exit_code(tmp_path, exit_code):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / SCRIPT.name
    shutil.copy(SCRIPT, script)
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" > calls.txt\n'
        'mkdir -p "$OUTPUT_DIR/documents"\n'
        'printf "test" > "$OUTPUT_DIR/clinical_extract.md"\n'
        'printf "test" > "$OUTPUT_DIR/documents/example.txt"\n'
        f'exit {exit_code}\n'
    )
    python.chmod(0o700)
    output = tmp_path / "output"
    env = {**os.environ, "PERSON": "me", "OUTPUT_DIR": str(output), "SIGNAL_FILE": str(output / ".sync_changes.json")}
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == exit_code
    assert (tmp_path / "calls.txt").read_text().strip() == "-m health_sync.cli sync --person me"
    signal = json.loads((output / ".sync_changes.json").read_text())
    assert signal["exit_code"] == exit_code
    assert signal["person"] == "me"
    assert signal["has_changes"] is True
    assert signal["changed_files"] == [str(output / "clinical_extract.md")]
    assert signal["new_documents"] == [str(output / "documents/example.txt")]


def test_signal_wrapper_rejects_another_person():
    result = subprocess.run(["bash", str(SCRIPT), "--person", "other"], env={**os.environ, "PERSON": "me"}, capture_output=True, text=True)
    assert result.returncode == 2
