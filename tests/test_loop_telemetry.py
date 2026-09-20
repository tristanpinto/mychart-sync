from __future__ import annotations

"""Tests for writers/loop_telemetry.py — rolling Loop summary."""

from datetime import date, timedelta
from pathlib import Path

from health_sync.parsers.loop_rollup import DailyLoopSummary
from health_sync.writers.loop_telemetry import (
    compute_period,
    render_loop_telemetry,
    write_loop_telemetry,
)


def _summary(d: date, tir: float = 85.0, gmi: float = 6.5, tdd: float = 25.0,
             hypo1: int = 0, hypo2: int = 0, low_conf: bool = False) -> DailyLoopSummary:
    return DailyLoopSummary(
        local_date=d,
        cgm_count=288,
        coverage_pct=99.0,
        low_confidence=low_conf,
        tir_70_180_pct=tir,
        tbr_lt_70_pct=2.0,
        tbr_lt_54_pct=0.5,
        tar_gt_180_pct=13.0,
        tar_gt_250_pct=1.0,
        mean_glucose_mgdl=130.0,
        gmi_pct=gmi,
        cv_pct=28.0,
        bolus_units=5.0,
        basal_units=tdd - 5.0,
        tdd_units=tdd,
        total_carbs_g=30.0,
        n_hypo_level_1=hypo1,
        n_hypo_level_2=hypo2,
    )


def test_compute_period_averages_high_confidence_only():
    today = date(2026, 5, 13)
    summaries = [
        _summary(today - timedelta(days=i), tir=80.0 + i, gmi=6.0 + i * 0.1)
        for i in range(7)
    ]
    # Add one low-confidence day with very different stats — should be excluded
    summaries.append(_summary(today - timedelta(days=2), tir=20.0, gmi=10.0, low_conf=True))

    rollup = compute_period(summaries, today - timedelta(days=6), today)
    # mean TIR across the 7 non-low-conf days = mean(80..86) = 83
    assert abs(rollup.mean_tir_70_180_pct - 83.0) < 0.1
    assert rollup.low_confidence_days == 1


def test_compute_period_counts_hypos():
    today = date(2026, 5, 13)
    summaries = [
        _summary(today, hypo1=2, hypo2=1),
        _summary(today - timedelta(days=1), hypo1=1, hypo2=0),
    ]
    rollup = compute_period(summaries, today - timedelta(days=1), today)
    assert rollup.n_level_1_hypos == 3
    assert rollup.n_level_2_hypos == 1


def test_compute_period_handles_empty_window():
    today = date(2026, 5, 13)
    rollup = compute_period([], today - timedelta(days=6), today)
    assert rollup.days_with_data == 0
    assert rollup.mean_tir_70_180_pct == 0.0
    assert rollup.n_level_1_hypos == 0


def test_render_skips_all_time_for_short_span():
    today = date(2026, 5, 13)
    summaries = [_summary(today - timedelta(days=i)) for i in range(30)]
    out = render_loop_telemetry(summaries, today)
    assert "Last 7 days" in out
    assert "Last 30 days" in out
    assert "All-time" not in out
    assert "| **Hypo events: level 1** | 0 |" in out


def test_render_includes_all_time_for_long_span():
    today = date(2026, 5, 13)
    summaries = [_summary(today - timedelta(days=i)) for i in range(0, 120, 7)]  # ~120 days
    out = render_loop_telemetry(summaries, today)
    assert "Last 7 days" in out
    assert "Last 30 days" in out
    assert "All-time" in out


def test_render_empty_summaries_does_not_crash():
    today = date(2026, 5, 13)
    out = render_loop_telemetry([], today)
    assert "Loop telemetry" in out
    assert "No daily summaries" in out


def test_render_recent_days_glance_marks_hypo_events():
    today = date(2026, 5, 13)
    summaries = [
        _summary(today, hypo1=1, hypo2=1),
        _summary(today - timedelta(days=1)),
    ]
    out = render_loop_telemetry(summaries, today)
    assert "L2×1, L1×1" in out


def test_render_marks_low_confidence_in_glance():
    today = date(2026, 5, 13)
    summaries = [
        _summary(today, low_conf=True),
        _summary(today - timedelta(days=1)),
    ]
    out = render_loop_telemetry(summaries, today)
    assert "low CGM coverage" in out


def test_write_loop_telemetry_idempotent(tmp_path: Path):
    today = date(2026, 5, 13)
    summaries = [_summary(today - timedelta(days=i)) for i in range(7)]
    out_path = tmp_path / "loop_telemetry.md"
    a = write_loop_telemetry(summaries, out_path, today=today).read_bytes()
    b = write_loop_telemetry(summaries, out_path, today=today).read_bytes()
    assert a == b
