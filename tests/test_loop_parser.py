from __future__ import annotations

"""Tests for parsers/loop.py — Tidepool record → NormalizedLoopRecord."""

from datetime import date

from health_sync.parsers.loop import (
    MMOL_TO_MGDL,
    compute_local_date,
    parse_records,
    parse_time,
)


def test_parse_time_handles_iso_with_fractional_seconds_and_Z():
    t = parse_time("2026-05-13T22:20:00.123Z")
    assert t.year == 2026 and t.month == 5 and t.day == 13
    assert t.hour == 22 and t.minute == 20
    assert t.tzinfo is not None


def test_compute_local_date_uses_per_record_offset():
    # 04:16 UTC with offset -420 (PDT) = 21:16 the prior day local
    record = {"time": "2026-05-13T04:16:00.000Z", "timezoneOffset": -420}
    assert compute_local_date(record) == date(2026, 5, 12)


def test_compute_local_date_falls_back_to_person_timezone():
    # No timezoneOffset → use fallback_tz (America/Los_Angeles by default)
    record = {"time": "2026-05-13T04:16:00.000Z"}
    assert compute_local_date(record, fallback_tz="America/Los_Angeles") == date(2026, 5, 12)


def test_compute_local_date_eastern_offset_record():
    # A synthetic UTC-4 record at 23:30 local = 03:30 next UTC.
    record = {"time": "2024-07-15T03:30:00.000Z", "timezoneOffset": -240}
    assert compute_local_date(record) == date(2024, 7, 14)


def test_parse_cgm_mg_dl_passthrough_with_rounding():
    record = {
        "type": "cbg",
        "time": "2026-05-13T22:20:00.123Z",
        "timezoneOffset": -420,
        "value": 132.00001,
        "units": "mg/dL",
    }
    out = list(parse_records([record]))
    assert len(out) == 1
    assert out[0].kind == "cgm"
    assert out[0].glucose_mgdl == 132.0  # rounded to 1 dp


def test_parse_cgm_mmol_conversion():
    record = {
        "type": "cbg",
        "time": "2026-05-13T22:20:00.123Z",
        "timezoneOffset": -420,
        "value": 5.5,  # mmol/L
        "units": "mmol/L",
    }
    out = list(parse_records([record]))
    assert len(out) == 1
    # 5.5 mmol/L × 18.0182 = 99.1001 → round to 99.1
    assert out[0].glucose_mgdl == round(5.5 * MMOL_TO_MGDL, 1)


def test_parse_bolus_sums_normal_and_extended():
    record = {
        "type": "bolus",
        "time": "2026-05-13T18:40:00.000Z",
        "timezoneOffset": -420,
        "normal": 0.25,
        "extended": 0.75,
        "subType": "normal",
    }
    out = list(parse_records([record]))
    assert len(out) == 1
    assert out[0].kind == "bolus"
    assert out[0].insulin_units == 1.0


def test_parse_basal_unknown_duration_flagged_open_ended():
    record = {
        "type": "basal",
        "time": "2026-05-13T22:05:00.000Z",
        "timezoneOffset": -420,
        "duration": 0,
        "rate": 0.1,
        "deliveryType": "automated",
        "annotations": '[{"code":"basal/unknown-duration"}]',
    }
    out = list(parse_records([record]))
    assert len(out) == 1
    assert out[0].kind == "basal"
    assert out[0].open_ended is True
    assert out[0].rate_units_per_hour == 0.1


def test_parse_basal_duration_in_minutes():
    record = {
        "type": "basal",
        "time": "2026-05-13T21:25:00.000Z",
        "timezoneOffset": -420,
        "duration": 37.5,
        "rate": 0.8,
        "deliveryType": "automated",
    }
    out = list(parse_records([record]))
    assert out[0].duration_minutes == 37.5
    assert out[0].open_ended is False


def test_parse_food_extracts_net_carbs_from_json_string_nutrition():
    record = {
        "type": "food",
        "time": "2026-05-13T14:27:00.000Z",
        "timezoneOffset": -420,
        "nutrition": '{"carbohydrate":{"net":20,"units":"grams"},"estimatedAbsorptionDuration":10800}',
    }
    out = list(parse_records([record]))
    assert len(out) == 1
    assert out[0].kind == "food"
    assert out[0].carbs_g == 20.0


def test_unknown_record_type_skipped_with_warning(caplog):
    record = {"type": "totally_unknown_type", "time": "2026-05-13T00:00:00.000Z"}
    out = list(parse_records([record]))
    assert out == []
    assert any("totally_unknown_type" in r.message for r in caplog.records)


def test_known_ignored_types_silent_skip(caplog):
    # pumpStatus is intentionally skipped — should not log a warning
    record = {"type": "pumpStatus", "time": "2026-05-13T00:00:00.000Z"}
    out = list(parse_records([record]))
    assert out == []
    assert not any("pumpStatus" in r.message for r in caplog.records)


def test_suspend_basal_keeps_zero_rate():
    record = {
        "type": "basal",
        "time": "2026-05-13T12:00:00.000Z",
        "timezoneOffset": -420,
        "duration": 30,
        "rate": 0,
        "deliveryType": "suspend",
    }
    out = list(parse_records([record]))
    assert out[0].delivery_type == "suspend"
    assert out[0].rate_units_per_hour == 0
