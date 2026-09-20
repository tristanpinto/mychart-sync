from __future__ import annotations

"""Tests for writers/loop_raw.py and writers/loop_daily.py."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from health_sync.parsers.loop import NormalizedLoopRecord
from health_sync.parsers.loop_rollup import DailyLoopSummary, HypoEvent
from health_sync.writers.loop_daily import write_daily_summary
from health_sync.writers.loop_raw import write_raw_jsonl


def _record(rid: str, time_str: str, local_d: date, kind: str = "cgm") -> NormalizedLoopRecord:
    return NormalizedLoopRecord(
        kind=kind,
        time_utc=datetime.fromisoformat(time_str.replace("Z", "+00:00")).astimezone(timezone.utc),
        local_date=local_d,
        raw={"id": rid, "type": "cbg", "value": 100, "time": time_str},
        glucose_mgdl=100.0,
    )


def test_jsonl_dedup_by_id(tmp_path: Path):
    d = date(2026, 5, 12)
    records = [
        _record("a", "2026-05-12T07:00:00.000Z", d),
        _record("a", "2026-05-12T07:00:00.000Z", d),  # duplicate
        _record("b", "2026-05-12T07:05:00.000Z", d),
    ]
    out = write_raw_jsonl(records, tmp_path, d)
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 2
    ids = [json.loads(line)["id"] for line in lines]
    assert ids == ["a", "b"]


def test_jsonl_failed_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    d = date(2026, 5, 12)
    first = _record("first", "2026-05-12T07:00:00Z", d)
    out = write_raw_jsonl([first], tmp_path, d)
    before = out.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("health_sync.auth.storage.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        write_raw_jsonl([first, _record("next", "2026-05-12T08:00:00Z", d)], tmp_path, d)
    assert out.read_bytes() == before
    assert list(tmp_path.iterdir()) == [out]


def test_jsonl_unchanged_content_does_not_replace_file(tmp_path, monkeypatch):
    d = date(2026, 5, 12)
    records = [
        _record("z", "2026-05-12T08:00:00Z", d),
        _record("a", "2026-05-12T07:00:00Z", d),
        _record("m", "2026-05-12T07:30:00Z", d),
    ]
    out = write_raw_jsonl(records, tmp_path, d)
    inode = out.stat().st_ino
    before = out.read_bytes()

    def unexpected_write(*args):
        raise AssertionError("Unchanged raw history should not be rewritten")

    monkeypatch.setattr("health_sync.writers.loop_raw.write_private_text", unexpected_write)
    write_raw_jsonl(records, tmp_path, d)
    assert out.stat().st_ino == inode
    assert out.read_bytes() == before
    assert out.stat().st_mode & 0o777 == 0o600


def test_jsonl_sorted_by_time(tmp_path: Path):
    d = date(2026, 5, 12)
    records = [
        _record("z", "2026-05-12T08:00:00.000Z", d),
        _record("a", "2026-05-12T07:00:00.000Z", d),
    ]
    out = write_raw_jsonl(records, tmp_path, d)
    lines = out.read_text().strip().split("\n")
    times = [json.loads(line)["time"] for line in lines]
    assert times == sorted(times)


def test_jsonl_only_includes_target_date(tmp_path: Path):
    d = date(2026, 5, 12)
    records = [
        _record("a", "2026-05-12T07:00:00.000Z", d),
        _record("b", "2026-05-11T07:00:00.000Z", date(2026, 5, 11)),  # different day
    ]
    out = write_raw_jsonl(records, tmp_path, d)
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "a"


def test_daily_summary_contains_key_fields(tmp_path: Path):
    summary = DailyLoopSummary(
        local_date=date(2026, 5, 12),
        cgm_count=288,
        coverage_pct=100.0,
        tir_70_180_pct=92.4,
        tbr_lt_70_pct=2.1,
        tbr_lt_54_pct=1.7,
        tar_gt_180_pct=5.6,
        tar_gt_250_pct=0.0,
        mean_glucose_mgdl=129.4,
        gmi_pct=6.41,
        cv_pct=24.7,
        bolus_units=25.05,
        basal_units=10.1,
        tdd_units=35.15,
        total_carbs_g=27.0,
    )
    out = write_daily_summary(summary, tmp_path)
    text = out.read_text()
    assert "Loop telemetry — 2026-05-12" in text
    assert "129.4 mg/dL" in text
    assert "6.41%" in text
    assert "92.4%" in text
    assert "35.15 U" in text
    assert "27 g" in text
    # Insulin labeling per post-spot-check decision
    assert "Manual bolus" in text
    assert "Loop-delivered" in text


def test_daily_summary_idempotent(tmp_path: Path):
    summary = DailyLoopSummary(local_date=date(2026, 5, 12))
    text1 = write_daily_summary(summary, tmp_path).read_bytes()
    text2 = write_daily_summary(summary, tmp_path).read_bytes()
    assert text1 == text2


def test_daily_summary_low_confidence_note(tmp_path: Path):
    summary = DailyLoopSummary(local_date=date(2026, 5, 12), low_confidence=True, coverage_pct=45)
    out = write_daily_summary(summary, tmp_path)
    assert "Low-confidence day" in out.read_text()


def test_daily_summary_hypo_times_use_record_offset(tmp_path: Path):
    # 13:55Z is 09:55 at UTC-4 but 06:55 in America/Los_Angeles; the rendered
    # time must match the UTC-4 offset the header claims.
    event = HypoEvent(
        start_utc=datetime(2026, 8, 11, 13, 55, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 11, 14, 40, tzinfo=timezone.utc),
        duration_minutes=50.0,
        nadir_mgdl=39.0,
        level=2,
    )
    summary = DailyLoopSummary(
        local_date=date(2026, 8, 11),
        primary_timezone_offset_minutes=-240,
        hypo_events=[event],
    )
    text = write_daily_summary(summary, tmp_path, tz="America/Los_Angeles").read_text()
    assert "Local timezone: UTC-4h (from record timezoneOffset)" in text
    assert "09:55–10:40" in text
    assert "06:55" not in text


def test_daily_summary_hypo_times_fall_back_to_person_tz(tmp_path: Path):
    event = HypoEvent(
        start_utc=datetime(2026, 8, 11, 13, 55, tzinfo=timezone.utc),
        end_utc=datetime(2026, 8, 11, 14, 40, tzinfo=timezone.utc),
        duration_minutes=50.0,
        nadir_mgdl=60.0,
        level=1,
    )
    summary = DailyLoopSummary(local_date=date(2026, 8, 11), hypo_events=[event])
    text = write_daily_summary(summary, tmp_path, tz="America/Los_Angeles").read_text()
    assert "Local timezone: America/Los_Angeles" in text
    assert "06:55–07:40" in text  # PDT rendering when no record offset
