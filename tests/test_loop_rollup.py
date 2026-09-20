from __future__ import annotations

"""Tests for parsers/loop_rollup.py — daily glycemic + insulin rollups."""

from datetime import date, datetime, timedelta, timezone

from health_sync.parsers.loop import NormalizedLoopRecord
from health_sync.parsers.loop_rollup import (
    DEXCOM_INTERVAL_MINUTES,
    compute_daily,
)


def _cgm(time_utc: datetime, value: float, local_date_v: date) -> NormalizedLoopRecord:
    return NormalizedLoopRecord(
        kind="cgm",
        time_utc=time_utc,
        local_date=local_date_v,
        raw={"type": "cbg", "value": value, "units": "mg/dL", "id": f"c{time_utc.isoformat()}"},
        glucose_mgdl=value,
    )


def _bolus(units: float, local_date_v: date) -> NormalizedLoopRecord:
    return NormalizedLoopRecord(
        kind="bolus",
        time_utc=datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
        local_date=local_date_v,
        raw={"type": "bolus", "id": f"b{units}"},
        insulin_units=units,
    )


def _basal(
    time_utc: datetime,
    rate: float,
    duration_min: float,
    local_date_v: date,
    delivery_type: str = "automated",
    open_ended: bool = False,
) -> NormalizedLoopRecord:
    return NormalizedLoopRecord(
        kind="basal",
        time_utc=time_utc,
        local_date=local_date_v,
        raw={"type": "basal"},
        rate_units_per_hour=rate,
        duration_minutes=duration_min,
        delivery_type=delivery_type,
        open_ended=open_ended,
    )


def _make_day_of_cgms(local_d: date, value: float) -> list[NormalizedLoopRecord]:
    """288 points at 5-min cadence covering the local day (in UTC)."""
    start = datetime.combine(local_d, datetime.min.time(), tzinfo=timezone.utc)
    return [_cgm(start + timedelta(minutes=5 * i), value, local_d) for i in range(288)]


def test_tir_perfect_100_in_range():
    d = date(2026, 5, 12)
    records = _make_day_of_cgms(d, 100)
    summary = compute_daily(records, d)
    assert summary.tir_70_180_pct == 100.0
    assert summary.tbr_lt_70_pct == 0.0
    assert summary.tar_gt_180_pct == 0.0
    assert summary.mean_glucose_mgdl == 100.0


def test_gmi_formula():
    # GMI = 3.31 + 0.02392 × mean. Mean = 154 → GMI = 7.00 (rounded)
    d = date(2026, 5, 12)
    records = _make_day_of_cgms(d, 154)
    summary = compute_daily(records, d)
    # 3.31 + 0.02392 * 154 = 3.31 + 3.68368 = 6.99368 → 6.99
    assert summary.gmi_pct == 6.99


def test_hypo_event_detected_when_sustained_15min():
    d = date(2026, 5, 12)
    base = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    # 4 consecutive points 5 min apart, all <70 — span = 15 min nominal
    records = [_cgm(base + timedelta(minutes=5 * i), 60, d) for i in range(4)]
    # Add normal CGM around it
    summary = compute_daily(records, d)
    assert summary.n_hypo_level_1 == 1
    assert summary.n_hypo_level_2 == 0
    assert summary.hypo_events[0].nadir_mgdl == 60


def test_hypo_level_2_when_any_point_below_54():
    d = date(2026, 5, 12)
    base = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    records = [
        _cgm(base, 65, d),
        _cgm(base + timedelta(minutes=5), 60, d),
        _cgm(base + timedelta(minutes=10), 50, d),  # <54
        _cgm(base + timedelta(minutes=15), 55, d),
    ]
    summary = compute_daily(records, d)
    assert summary.n_hypo_level_2 == 1
    assert summary.hypo_events[0].nadir_mgdl == 50


def test_hypo_run_broken_by_gap_exceeding_tolerance():
    d = date(2026, 5, 12)
    base = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    # Two points low, 10 minute gap, two more points low.
    # Gap > 7.5 min should break the run, each sub-run < 15 min so no event.
    records = [
        _cgm(base, 60, d),
        _cgm(base + timedelta(minutes=5), 62, d),
        _cgm(base + timedelta(minutes=15), 60, d),  # 10 min gap from previous
        _cgm(base + timedelta(minutes=20), 65, d),
    ]
    summary = compute_daily(records, d)
    assert summary.n_hypo_level_1 == 0
    assert summary.n_hypo_level_2 == 0


def test_basal_forward_fill_open_ended_uses_next_record_time():
    d = date(2026, 5, 12)
    base = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    records = [
        _basal(base, rate=1.0, duration_min=0, local_date_v=d, open_ended=True),
        _basal(base + timedelta(minutes=30), rate=0.5, duration_min=10, local_date_v=d),
    ]
    summary = compute_daily(records, d)
    # First basal: forward-filled to 30 min × 1.0 U/hr = 0.5 U
    # Second basal: 10 min × 0.5 U/hr = 0.0833 U
    # Total = ~0.58
    assert abs(summary.basal_units - 0.58) < 0.01


def test_basal_suspend_contributes_zero():
    d = date(2026, 5, 12)
    base = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    records = [
        _basal(base, rate=0, duration_min=30, local_date_v=d, delivery_type="suspend"),
    ]
    summary = compute_daily(records, d)
    assert summary.basal_units == 0.0


def test_basal_forward_fill_capped_at_30min():
    d = date(2026, 5, 12)
    base = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    # Open-ended record followed by another 2 hours later — must cap at 30 min
    records = [
        _basal(base, rate=1.0, duration_min=0, local_date_v=d, open_ended=True),
        _basal(base + timedelta(hours=2), rate=1.0, duration_min=10, local_date_v=d),
    ]
    summary = compute_daily(records, d)
    # First: capped at 30 min × 1.0 U/hr = 0.5 U
    # Second: 10 min × 1.0 = 0.167 U
    # Total ~0.67 (not 2.17 it would be without cap)
    assert summary.basal_units < 1.0


def test_tdd_is_basal_plus_bolus():
    d = date(2026, 5, 12)
    records = [
        _bolus(5.0, d),
        _bolus(3.0, d),
        _basal(
            datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
            rate=1.0,
            duration_min=60,
            local_date_v=d,
        ),
    ]
    summary = compute_daily(records, d)
    assert summary.bolus_units == 8.0
    assert summary.basal_units == 1.0
    assert summary.tdd_units == 9.0


def test_low_confidence_flag_when_coverage_below_70_pct():
    d = date(2026, 5, 12)
    # Only 100 readings × 5min = 500 min ≈ 34.7% coverage of a 24h day
    start = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
    records = [_cgm(start + timedelta(minutes=5 * i), 100, d) for i in range(100)]
    summary = compute_daily(records, d)
    assert summary.low_confidence is True
    assert summary.coverage_pct < 70


def test_empty_day_does_not_crash():
    d = date(2026, 5, 12)
    summary = compute_daily([], d)
    assert summary.cgm_count == 0
    assert summary.tir_70_180_pct == 0.0
    assert summary.tdd_units == 0.0
    assert summary.gmi_pct == 0.0
    assert summary.hypo_events == []
