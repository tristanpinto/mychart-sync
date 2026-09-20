from __future__ import annotations

"""Tests for sync/loop_engine.py — export-first sync orchestration."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from health_sync.config import AppConfig
from health_sync.sync.loop_engine import LoopSyncError, sync_from_export


@pytest.fixture
def minimal_export_file(tmp_path: Path) -> Path:
    """Create a tiny export covering one full day plus UTC-midnight boundary records."""
    records: list[dict[str, Any]] = []
    # 288 cbg records (5-min cadence) starting 00:00 PDT 2026-05-12 = 07:00 UTC.
    # Use timedelta arithmetic so hours don't overflow ISO 8601.
    start = datetime(2026, 5, 12, 7, 0, 0, tzinfo=timezone.utc)
    for i in range(288):
        t = start + timedelta(minutes=5 * i)
        records.append(
            {
                "id": f"cbg-{i}",
                "type": "cbg",
                "time": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "timezoneOffset": -420,
                "value": 100,
                "units": "mg/dL",
            }
        )
    # UTC-midnight boundary: this record at 03:30 UTC (= 20:30 PDT prior day,
    # = 23:30 EDT prior day) should bin to 2026-05-11 local Pacific
    records.append(
        {
            "id": "boundary-pdt",
            "type": "cbg",
            "time": "2026-05-12T03:30:00.000Z",
            "timezoneOffset": -420,  # Pacific
            "value": 110,
            "units": "mg/dL",
        }
    )
    # Same wall-clock UTC time but with Eastern offset (a travel day).
    # This is 23:30 local EDT on 2026-05-11.
    records.append(
        {
            "id": "boundary-edt",
            "type": "cbg",
            "time": "2026-05-12T03:30:00.000Z",
            "timezoneOffset": -240,  # Eastern
            "value": 115,
            "units": "mg/dL",
        }
    )
    path = tmp_path / "tidepool_export.json"
    path.write_text(json.dumps(records))
    return path


@pytest.fixture
def minimal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    brain = tmp_path / "brain"
    (brain / "loop" / "raw").mkdir(parents=True)
    (brain / "loop" / "daily").mkdir(parents=True)
    return AppConfig(
        client_id="cid",
        default_person="person_b",
        persons={
            "person_b": {
                "brain_dir": str(brain),
                "timezone": "America/Los_Angeles",
                "paths": {
                    "clinical_extract": "clinical.md",
                    "health_profile": "profile.md",
                    "lab_results": "labs.md",
                    "records_dir": "records",
                    "loop_raw_dir": "loop/raw",
                    "loop_daily_dir": "loop/daily",
                    "loop_telemetry": "loop/loop_telemetry.md",
                },
            }
        },
    )


def test_sync_from_export_writes_per_day_files(
    minimal_export_file: Path, minimal_config: AppConfig
):
    counts = sync_from_export(minimal_export_file, "person_b", minimal_config)
    assert counts["raw_records"] == 290  # 288 + 2 boundary
    assert counts["parsed_records"] == 290
    # Days covered: 2026-05-11 (boundary records) + 2026-05-12 (288 records)
    assert counts["days"] >= 2
    assert counts["daily_files_written"] >= 2


def test_sync_from_export_dry_run_writes_nothing(
    minimal_export_file: Path, minimal_config: AppConfig
):
    raw_dir = minimal_config.brain_path("loop_raw_dir", "person_b")
    daily_dir = minimal_config.brain_path("loop_daily_dir", "person_b")

    counts = sync_from_export(minimal_export_file, "person_b", minimal_config, dry_run=True)
    assert counts["daily_files_written"] == 0
    assert list(raw_dir.iterdir()) == []
    assert list(daily_dir.iterdir()) == []


def test_sync_from_export_handles_missing_file(
    tmp_path: Path, minimal_config: AppConfig
):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(LoopSyncError):
        sync_from_export(missing, "person_b", minimal_config)


def test_utc_midnight_boundary_records_bin_to_local_date(
    minimal_export_file: Path, minimal_config: AppConfig, tmp_path: Path
):
    """Pacific record at 03:30 UTC (= 20:30 PDT day-before) bins to 2026-05-11."""
    sync_from_export(minimal_export_file, "person_b", minimal_config)

    # The two boundary records both have local_date = 2026-05-11 (one in
    # Pacific local, one in Eastern local — both ended up on the same local day
    # because EDT 23:30 on 5/11 and PDT 20:30 on 5/11 are both "May 11" local).
    raw_dir = minimal_config.brain_path("loop_raw_dir", "person_b")
    file_2026_05_11 = raw_dir / "2026-05-11.jsonl"
    assert file_2026_05_11.exists()
    ids = [json.loads(line)["id"] for line in file_2026_05_11.read_text().strip().split("\n")]
    assert "boundary-pdt" in ids
    assert "boundary-edt" in ids
