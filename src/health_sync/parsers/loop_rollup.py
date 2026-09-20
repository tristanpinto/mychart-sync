from __future__ import annotations

"""Per-day glycemic and insulin rollups for Loop data.

Pure functions, no I/O. Inputs are NormalizedLoopRecord lists for a single
local date. Outputs are typed DailyLoopSummary dataclasses ready for writers.

Clinical references:
- ADA Standards of Medical Care 2025 — TIR/TBR/TAR bands
- Bergenstal et al. 2018 — GMI = 3.31 + 0.02392 × mean_glucose_mgdl
- ADA hypoglycemia classification: level 1 = <70, level 2 = <54
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from health_sync.parsers.loop import NormalizedLoopRecord

logger = logging.getLogger(__name__)

# CGM sample cadence and gap tolerance
DEXCOM_INTERVAL_MINUTES = 5.0
SAMPLE_GAP_TOLERANCE_MULT = 1.5  # gap > 7.5 min breaks a continuous run

# Glycemic thresholds (mg/dL) per ADA
TIR_LOW = 70
TIR_HIGH = 180
HYPO_LEVEL_2 = 54
HYPER_LEVEL_2 = 250

# Coverage thresholds for confidence flagging
LOW_CONFIDENCE_COVERAGE_PCT = 70

# Basal forward-fill defensive cap (minutes). A gap longer than this is
# more likely missing data than a sustained basal segment.
BASAL_FORWARD_FILL_CAP_MIN = 30

# Hypo event minimum duration per ADA (level 1 hypoglycemia)
HYPO_EVENT_MIN_DURATION_MIN = 15


@dataclass
class HypoEvent:
    start_utc: datetime
    end_utc: datetime
    duration_minutes: float
    nadir_mgdl: float
    level: int  # 1 or 2


@dataclass
class DailyLoopSummary:
    local_date: date

    # Timezone recorded for the majority of this local day. When multiple
    # offsets appear in one day, this is the most common one (travel day flag).
    primary_timezone_offset_minutes: int | None = None
    is_travel_day: bool = False  # True if multiple distinct offsets in this day

    # CGM coverage
    cgm_count: int = 0
    coverage_pct: float = 0.0
    low_confidence: bool = False

    # Time-in-range (% of covered time, CGM-only)
    tir_70_180_pct: float = 0.0
    tbr_lt_70_pct: float = 0.0
    tbr_lt_54_pct: float = 0.0
    tar_gt_180_pct: float = 0.0
    tar_gt_250_pct: float = 0.0

    # Central tendency + variability
    mean_glucose_mgdl: float = 0.0
    gmi_pct: float = 0.0
    cv_pct: float = 0.0  # std / mean × 100

    # Insulin + carbs
    bolus_units: float = 0.0
    basal_units: float = 0.0
    tdd_units: float = 0.0
    total_carbs_g: float = 0.0

    # Hypo events (level 1 and level 2)
    hypo_events: list[HypoEvent] = field(default_factory=list)
    n_hypo_level_1: int = 0
    n_hypo_level_2: int = 0


def _compute_basal_units(
    all_basals: list[NormalizedLoopRecord], target_date: date
) -> float:
    """Sum delivered basal insulin units for a target local date.

    Forward-fill open-ended durations using the GLOBAL chronological successor,
    not just same-day. Critical for end-of-day records where the next basal
    falls on the following local date (without this, the last record of the
    day defaults to 5 min and we under-count).

    Cap each segment at BASAL_FORWARD_FILL_CAP_MIN to avoid runaway sums from
    gaps in the data (e.g., missing uploads).

    Records are attributed to the local date of their START time (Tidepool's
    convention) — a record at 23:55 local with 30-min duration is credited
    entirely to that day, not split across midnight.

    Suspend records contribute zero (pump off).
    """
    sorted_basals = sorted(all_basals, key=lambda r: r.time_utc)
    total = 0.0
    for i, record in enumerate(sorted_basals):
        if record.local_date != target_date:
            continue
        if record.delivery_type == "suspend":
            continue
        if record.rate_units_per_hour is None:
            continue

        if record.open_ended:
            if i + 1 < len(sorted_basals):
                gap_min = (
                    sorted_basals[i + 1].time_utc - record.time_utc
                ).total_seconds() / 60.0
            else:
                gap_min = DEXCOM_INTERVAL_MINUTES
            duration = min(gap_min, BASAL_FORWARD_FILL_CAP_MIN)
        else:
            duration = record.duration_minutes or 0

        total += record.rate_units_per_hour * (duration / 60.0)
    return total


def _detect_hypo_events(cgms: list[NormalizedLoopRecord]) -> list[HypoEvent]:
    """Find continuous-run hypo events.

    A run is a contiguous sequence of CGM points <70 mg/dL where consecutive
    samples are no more than 7.5 min apart (1.5× Dexcom's 5-min nominal interval).
    A run is a "level 1" event if its duration ≥ 15 min. It becomes "level 2" if
    any point in the run is <54 mg/dL.

    Gaps > 7.5 min break a run — important to avoid false-positives when CGM
    drops out mid-low.
    """
    sorted_cgms = sorted(cgms, key=lambda r: r.time_utc)
    events: list[HypoEvent] = []
    run: list[NormalizedLoopRecord] = []

    def flush(run: list[NormalizedLoopRecord]) -> None:
        if not run:
            return
        # Duration: last sample time minus first sample time, plus an implicit
        # interval to "cover" the last reading.
        if len(run) < 2:
            duration_min = DEXCOM_INTERVAL_MINUTES
        else:
            span = (run[-1].time_utc - run[0].time_utc).total_seconds() / 60.0
            duration_min = span + DEXCOM_INTERVAL_MINUTES  # cover the last sample
        if duration_min < HYPO_EVENT_MIN_DURATION_MIN:
            return
        nadir = min(r.glucose_mgdl for r in run if r.glucose_mgdl is not None)
        level = 2 if nadir < HYPO_LEVEL_2 else 1
        events.append(
            HypoEvent(
                start_utc=run[0].time_utc,
                end_utc=run[-1].time_utc,
                duration_minutes=duration_min,
                nadir_mgdl=nadir,
                level=level,
            )
        )

    prev: NormalizedLoopRecord | None = None
    for r in sorted_cgms:
        if r.glucose_mgdl is None or r.glucose_mgdl >= TIR_LOW:
            flush(run)
            run = []
            prev = r
            continue
        # r is <70
        if prev is not None and run:
            gap_min = (r.time_utc - run[-1].time_utc).total_seconds() / 60.0
            if gap_min > DEXCOM_INTERVAL_MINUTES * SAMPLE_GAP_TOLERANCE_MULT:
                flush(run)
                run = []
        run.append(r)
        prev = r
    flush(run)
    return events


def _compute_cgm_time_weighted_bands(
    cgms: list[NormalizedLoopRecord], local_date: date
) -> tuple[dict[str, float], float, float]:
    """Compute time-weighted % time in each glycemic band, CGM coverage %, and mean.

    Returns (bands_pct, coverage_pct, mean_glucose_mgdl).

    Each CGM point is weighted by the interval to its next sample, capped at the
    nominal interval (so gaps don't inflate any single sample's weight). The
    total weighted time vs the day's 24h gives coverage_pct.
    """
    sorted_cgms = sorted(cgms, key=lambda r: r.time_utc)
    if not sorted_cgms:
        return {
            "tir_70_180": 0.0,
            "tbr_lt_70": 0.0,
            "tbr_lt_54": 0.0,
            "tar_gt_180": 0.0,
            "tar_gt_250": 0.0,
        }, 0.0, 0.0

    weights: list[float] = []
    values: list[float] = []
    for i, r in enumerate(sorted_cgms):
        if r.glucose_mgdl is None:
            continue
        if i + 1 < len(sorted_cgms):
            gap_min = (
                sorted_cgms[i + 1].time_utc - r.time_utc
            ).total_seconds() / 60.0
            weight = min(gap_min, DEXCOM_INTERVAL_MINUTES)
        else:
            weight = DEXCOM_INTERVAL_MINUTES
        weights.append(weight)
        values.append(r.glucose_mgdl)

    total_weight = sum(weights)
    if total_weight == 0:
        return {
            "tir_70_180": 0.0,
            "tbr_lt_70": 0.0,
            "tbr_lt_54": 0.0,
            "tar_gt_180": 0.0,
            "tar_gt_250": 0.0,
        }, 0.0, 0.0

    in_tir = sum(w for w, v in zip(weights, values) if TIR_LOW <= v <= TIR_HIGH)
    in_tbr_70 = sum(w for w, v in zip(weights, values) if v < TIR_LOW)
    in_tbr_54 = sum(w for w, v in zip(weights, values) if v < HYPO_LEVEL_2)
    in_tar_180 = sum(w for w, v in zip(weights, values) if v > TIR_HIGH)
    in_tar_250 = sum(w for w, v in zip(weights, values) if v > HYPER_LEVEL_2)

    bands = {
        "tir_70_180": 100.0 * in_tir / total_weight,
        "tbr_lt_70": 100.0 * in_tbr_70 / total_weight,
        "tbr_lt_54": 100.0 * in_tbr_54 / total_weight,
        "tar_gt_180": 100.0 * in_tar_180 / total_weight,
        "tar_gt_250": 100.0 * in_tar_250 / total_weight,
    }
    coverage_pct = 100.0 * total_weight / (24 * 60)
    mean_glucose = sum(w * v for w, v in zip(weights, values)) / total_weight
    return bands, coverage_pct, mean_glucose


def _compute_cv(values: list[float], mean: float) -> float:
    """Coefficient of variation (%): std / mean × 100.

    Note: not time-weighted. For Dexcom 5-min cadence with negligible gaps,
    this is a good approximation. Phase 3 may add time-weighted CV.
    """
    if not values or mean == 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return 100.0 * math.sqrt(variance) / mean


def compute_daily(
    records: Iterable[NormalizedLoopRecord], local_date: date
) -> DailyLoopSummary:
    """Build a DailyLoopSummary from a day's worth of records.

    Records should already be filtered to local_date by the caller.
    """
    # Full record set (not just the day) — needed for cross-day basal forward-fill.
    all_records = list(records)
    day_records = [r for r in all_records if r.local_date == local_date]
    cgms = [r for r in day_records if r.kind == "cgm" and r.glucose_mgdl is not None]
    cgms_cbg_only = [r for r in cgms if r.raw.get("type") == "cbg"]  # exclude smbg
    boluses = [r for r in day_records if r.kind == "bolus"]
    all_basals = [r for r in all_records if r.kind == "basal"]
    foods = [r for r in day_records if r.kind == "food"]

    summary = DailyLoopSummary(local_date=local_date)

    # Detect travel days and capture the day's primary timezone offset
    day_offsets = [
        r.raw.get("timezoneOffset")
        for r in day_records
        if r.raw.get("timezoneOffset") is not None
    ]
    if day_offsets:
        from collections import Counter

        offset_counts = Counter(day_offsets)
        summary.primary_timezone_offset_minutes = offset_counts.most_common(1)[0][0]
        summary.is_travel_day = len(offset_counts) > 1

    # CGM bands + coverage (CGM-only, time-weighted)
    bands, coverage_pct, mean_glu = _compute_cgm_time_weighted_bands(
        cgms_cbg_only, local_date
    )
    summary.cgm_count = len(cgms_cbg_only)
    # Cap at 100 for display. Travel days with a timezone shift can produce
    # a "local day" spanning 25-27 UTC hours; the underlying TIR bands are
    # still correct because they're percentages of total weighted time.
    summary.coverage_pct = round(min(coverage_pct, 100.0), 1)
    summary.low_confidence = coverage_pct < LOW_CONFIDENCE_COVERAGE_PCT
    summary.tir_70_180_pct = round(bands["tir_70_180"], 1)
    summary.tbr_lt_70_pct = round(bands["tbr_lt_70"], 1)
    summary.tbr_lt_54_pct = round(bands["tbr_lt_54"], 1)
    summary.tar_gt_180_pct = round(bands["tar_gt_180"], 1)
    summary.tar_gt_250_pct = round(bands["tar_gt_250"], 1)
    summary.mean_glucose_mgdl = round(mean_glu, 1)
    summary.gmi_pct = (
        round(3.31 + 0.02392 * mean_glu, 2) if mean_glu > 0 else 0.0
    )
    summary.cv_pct = round(
        _compute_cv([r.glucose_mgdl for r in cgms_cbg_only], mean_glu), 1
    )

    # Insulin
    # Tidepool's convention: only subType="normal" (and extended/dual variants)
    # boluses count as "bolus" (user-decided). subType="automated" boluses are
    # Loop algorithm-issued and belong in the basal-delivery bucket.
    manual_bolus_units = sum(
        r.insulin_units or 0
        for r in boluses
        if r.bolus_subtype != "automated"
    )
    automated_bolus_units = sum(
        r.insulin_units or 0
        for r in boluses
        if r.bolus_subtype == "automated"
    )
    summary.bolus_units = round(manual_bolus_units, 2)
    summary.basal_units = round(
        _compute_basal_units(all_basals, local_date) + automated_bolus_units, 2
    )
    summary.tdd_units = round(summary.bolus_units + summary.basal_units, 2)

    # Carbs
    summary.total_carbs_g = round(sum(r.carbs_g or 0 for r in foods), 0)

    # Hypo events
    events = _detect_hypo_events(cgms_cbg_only)
    summary.hypo_events = events
    summary.n_hypo_level_1 = sum(1 for e in events if e.level == 1)
    summary.n_hypo_level_2 = sum(1 for e in events if e.level == 2)

    return summary
