from __future__ import annotations

"""Tidepool export and API sync regression tests."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from health_sync.config import AppConfig
from health_sync.sync.loop_engine import LoopSyncError, sync_from_export


@pytest.fixture
def minimal_export_file(tmp_path: Path) -> Path:
    """Three readings spanning two local dates and two timezone offsets."""
    records = [
        {"id": rid, "type": "cbg", "time": time, "timezoneOffset": offset,
         "value": 100, "units": "mg/dL"}
        for rid, time, offset in [
            ("main-day", "2026-05-12T12:00:00Z", -420),
            ("boundary-pdt", "2026-05-12T03:30:00Z", -420),
            ("boundary-edt", "2026-05-12T03:30:00Z", -240),
        ]
    ]
    path = tmp_path / "tidepool_export.json"
    path.write_text(json.dumps(records))
    return path


@pytest.fixture
def minimal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setattr("health_sync.config._project_root", lambda: tmp_path)
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


def test_sync_from_export_writes_files_by_local_date(
    minimal_export_file: Path, minimal_config: AppConfig
):
    counts = sync_from_export(minimal_export_file, "person_b", minimal_config)
    assert counts["raw_records"] == counts["parsed_records"] == 3
    assert counts["days"] == counts["daily_files_written"] == 2
    raw_dir = minimal_config.output_path("loop_raw_dir", "person_b")
    daily_dir = minimal_config.output_path("loop_daily_dir", "person_b")
    # 03:30 UTC belongs to the previous day in both Pacific and Eastern time.
    prior_day = raw_dir / "2026-05-11.jsonl"
    assert {json.loads(line)["id"] for line in prior_day.read_text().splitlines()} == {
        "boundary-pdt", "boundary-edt",
    }
    assert json.loads((raw_dir / "2026-05-12.jsonl").read_text())["id"] == "main-day"
    assert {path.name for path in daily_dir.iterdir()} == {"2026-05-11.md", "2026-05-12.md"}


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


@pytest.fixture(params=["export", "api"])
def import_records(request, tmp_path, minimal_config, monkeypatch):
    """Exercise both entry points without accessing a real account."""
    from health_sync.auth.tidepool_credentials import TidepoolCredentials
    from health_sync.providers.registry import Provider
    from health_sync.sync import loop_engine

    current_records = []
    closed = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def fetch(self, start, end):
            return iter(current_records)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(loop_engine, "TidepoolClient", FakeClient)
    monkeypatch.setattr(loop_engine, "load_credentials", lambda *args: TidepoolCredentials(
        email="example@example.com", password="test-only", userid="test-user"
    ))
    monkeypatch.setattr(loop_engine, "_determine_fetch_window", lambda **kwargs: (
        datetime(2026, 5, 12, tzinfo=timezone.utc),
        datetime(2026, 5, 13, tzinfo=timezone.utc),
    ))
    provider = Provider(slug="tidepool", name="Tidepool", kind="tidepool", patient="person_b")

    def run(records, dry_run=False):
        if request.param == "export":
            path = tmp_path / "incoming.json"
            path.write_text(json.dumps(records))
            return sync_from_export(path, "person_b", minimal_config, dry_run=dry_run)
        current_records[:] = records
        before = len(closed)
        counts = loop_engine.sync_tidepool_api(provider, "person_b", minimal_config, dry_run=dry_run)
        assert len(closed) == before + 1
        return counts

    return run


def _bolus(record_id, hour, units=3, day="2026-05-12"):
    return {"id": record_id, "type": "bolus", "time": f"{day}T{hour}:00:00Z",
            "timezoneOffset": 0, "normal": units}


def test_sync_deduplicates_before_raw_and_summary(import_records, minimal_config):
    bolus = _bolus("same", "12")
    food = {"id": "food", "type": "food", "time": bolus["time"],
            "timezoneOffset": 0, "nutrition": {"carbohydrate": {"net": 20}}}
    counts = import_records([bolus, bolus, food, food])
    assert counts["parsed_records"] == 2
    raw = minimal_config.output_path("loop_raw_dir", "person_b") / "2026-05-12.jsonl"
    daily = minimal_config.output_path("loop_daily_dir", "person_b") / "2026-05-12.md"
    assert len(raw.read_text().splitlines()) == 2
    assert "| Manual bolus | 3.0 U |" in daily.read_text()
    assert "| Carbs logged | 20 g |" in daily.read_text()


def test_partial_sync_preserves_day_and_updates_matching_id(import_records, minimal_config):
    morning = _bolus("morning", "08", 2)
    afternoon = _bolus("afternoon", "15", 3)
    import_records([morning, afternoon])
    updated = _bolus("afternoon", "15", 4)
    import_records([updated])
    raw = minimal_config.output_path("loop_raw_dir", "person_b") / "2026-05-12.jsonl"
    daily = minimal_config.output_path("loop_daily_dir", "person_b") / "2026-05-12.md"
    assert [r["normal"] for r in map(json.loads, raw.read_text().splitlines())] == [2, 4]
    assert "| Manual bolus | 6.0 U |" in daily.read_text()
    before = (raw.read_bytes(), daily.read_bytes())
    import_records([updated])
    assert (raw.read_bytes(), daily.read_bytes()) == before


def test_latest_window_preserves_all_time_summary(import_records, minimal_config):
    import_records([_bolus("old", "12", day="2026-01-01"), _bolus("recent", "12")])
    import_records([_bolus("recent", "12", 4)])
    telemetry = minimal_config.output_path("loop_telemetry", "person_b").read_text()
    assert "All-time" in telemetry
    assert "2026-01-01" in telemetry


def test_dry_run_does_not_update_stored_history(import_records, minimal_config):
    import_records([_bolus("same", "12")])
    output = minimal_config.person("person_b").allowed_root()
    before = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
    counts = import_records([_bolus("same", "12", 9)], dry_run=True)
    assert counts["daily_files_written"] == 0
    assert {p: p.read_bytes() for p in output.rglob("*") if p.is_file()} == before


def test_updated_record_moves_between_days(import_records, minimal_config):
    import_records([_bolus("same", "12")])
    import_records([_bolus("same", "12", day="2026-05-13")])
    raw_dir = minimal_config.output_path("loop_raw_dir", "person_b")
    assert (raw_dir / "2026-05-12.jsonl").read_text() == ""
    assert len((raw_dir / "2026-05-13.jsonl").read_text().splitlines()) == 1


def test_merge_preserves_cross_day_basal_forward_fill(import_records, minimal_config):
    first = {"id": "first", "type": "basal", "time": "2026-05-12T23:50:00Z",
             "timezoneOffset": 0, "deliveryType": "automated", "rate": 1, "duration": 0}
    successor = {**first, "id": "next", "time": "2026-05-13T00:10:00Z", "duration": 5}
    import_records([first])
    import_records([successor])
    daily = minimal_config.output_path("loop_daily_dir", "person_b") / "2026-05-12.md"
    assert "| Loop-delivered | 0.33 U |" in daily.read_text()


def test_corrupt_stored_history_is_not_overwritten(import_records, minimal_config):
    raw = minimal_config.output_path("loop_raw_dir", "person_b") / "2026-05-12.jsonl"
    raw.write_text("not JSON\n")
    with pytest.raises(LoopSyncError, match="no files were changed"):
        import_records([_bolus("new", "12")])
    assert raw.read_text() == "not JSON\n"


def test_repeat_import_of_idless_records_is_stable(import_records, minimal_config):
    record = _bolus("unused", "12")
    del record["id"]
    import_records([record])
    import_records([record])
    raw = minimal_config.output_path("loop_raw_dir", "person_b") / "2026-05-12.jsonl"
    assert len(raw.read_text().splitlines()) == 1


def test_zero_correction_replaces_prior_bolus(import_records, minimal_config):
    import_records([_bolus("corrected", "12", 5)])
    import_records([_bolus("corrected", "12", 0)])
    raw = minimal_config.output_path("loop_raw_dir", "person_b") / "2026-05-12.jsonl"
    daily = minimal_config.output_path("loop_daily_dir", "person_b") / "2026-05-12.md"
    telemetry = minimal_config.output_path("loop_telemetry", "person_b")
    assert json.loads(raw.read_text())["normal"] == 0
    assert "| Manual bolus | 0 U |" in daily.read_text()
    assert "| Mean TDD insulin | 0.0 U/day |" in telemetry.read_text()
    import_records([])
    assert json.loads(raw.read_text())["normal"] == 0


@pytest.mark.parametrize("changes", [
    {"normal": None}, {"normal": "invalid"}, {"normal": float("nan")},
    {"normal": -1}, {"normal": False}, {"time": "invalid"},
])
def test_invalid_correction_does_not_replace_valid_record(import_records, minimal_config, changes):
    import_records([_bolus("same", "12", 5)])
    import_records([{**_bolus("same", "12", 0), **changes}])
    raw = minimal_config.output_path("loop_raw_dir", "person_b") / "2026-05-12.jsonl"
    assert json.loads(raw.read_text())["normal"] == 5


def test_empty_history_replaces_stale_telemetry(import_records, minimal_config):
    telemetry = minimal_config.output_path("loop_telemetry", "person_b")
    telemetry.write_text("Old metrics no longer supported by stored history")
    import_records([])
    assert "No daily summaries available" in telemetry.read_text()
    assert "Old metrics" not in telemetry.read_text()


@pytest.mark.parametrize("invalid", [{"unexpected": []}, [None], "not JSON"])
def test_invalid_export_is_a_clear_error(tmp_path, minimal_config, invalid):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(invalid))
    with pytest.raises(LoopSyncError, match="JSON array of records"):
        sync_from_export(path, "person_b", minimal_config)
