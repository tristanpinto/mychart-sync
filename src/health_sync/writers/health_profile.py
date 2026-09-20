from __future__ import annotations

"""Compatibility guard for manual-only health_profile.md files.

FHIR-derived structured facts belong in clinical_extract.md and lab_results.md.
The health profile is a curated summary, so sync must never merge provider data
into it automatically.
"""

from pathlib import Path
from typing import Any


def update_health_profile(
    file_path: Path,
    fhir_data: dict[str, list[dict[str, Any]]],
) -> str:
    """Preserve health_profile.md unchanged.

    This function is intentionally a no-op for older callers that still import
    it. New sync code should avoid calling it entirely.
    """
    return file_path.read_text() if file_path.exists() else ""
