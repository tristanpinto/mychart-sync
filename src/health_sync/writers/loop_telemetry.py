from __future__ import annotations

"""Rolling Loop telemetry summary for the output directory.

Writes one file (`loop_telemetry.md`) per person that aggregates daily
summaries into trailing 7-day, 30-day, and all-time periods, plus a glance
table of the most recent 14 days.

Idempotent full-overwrite from in-memory inputs. The all-time period is
optional: present when retained history spans more than 30 days.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable

from health_sync.parsers.loop_rollup import DailyLoopSummary

logger = logging.getLogger(__name__)


@dataclass
class PeriodRollup:
    """Aggregated metrics over a contiguous span of daily summaries."""

    start: date
    end: date
    n_days: int
    days_with_data: int
    low_confidence_days: int

    mean_tir_70_180_pct: float = 0.0
    mean_tbr_lt_70_pct: float = 0.0
    mean_tbr_lt_54_pct: float = 0.0
    mean_tar_gt_180_pct: float = 0.0
    mean_tar_gt_250_pct: float = 0.0
    mean_glucose_mgdl: float = 0.0
    mean_gmi_pct: float = 0.0
    mean_cv_pct: float = 0.0

    mean_tdd_units: float = 0.0
    mean_manual_bolus_units: float = 0.0
    mean_loop_delivered_units: float = 0.0
    mean_carbs_g: float = 0.0

    n_level_1_hypos: int = 0
    n_level_2_hypos: int = 0


def _mean_or_zero(values: list[float]) -> float:
    return mean(values) if values else 0.0


def compute_period(
    summaries: list[DailyLoopSummary], start: date, end: date
) -> PeriodRollup:
    """Aggregate daily summaries in [start, end] (inclusive both ends).

    Low-confidence days are EXCLUDED from glycemic averages (they distort
    TIR/mean/GMI), but COUNTED in the low_confidence_days tally so the user
    can see how many days the rollup discarded.
    """
    in_window = [s for s in summaries if start <= s.local_date <= end]
    n_days = (end - start).days + 1
    days_with_data = len(in_window)
    low_conf = [s for s in in_window if s.low_confidence]
    high_conf = [s for s in in_window if not s.low_confidence]

    return PeriodRollup(
        start=start,
        end=end,
        n_days=n_days,
        days_with_data=days_with_data,
        low_confidence_days=len(low_conf),
        mean_tir_70_180_pct=round(_mean_or_zero([s.tir_70_180_pct for s in high_conf]), 1),
        mean_tbr_lt_70_pct=round(_mean_or_zero([s.tbr_lt_70_pct for s in high_conf]), 1),
        mean_tbr_lt_54_pct=round(_mean_or_zero([s.tbr_lt_54_pct for s in high_conf]), 1),
        mean_tar_gt_180_pct=round(_mean_or_zero([s.tar_gt_180_pct for s in high_conf]), 1),
        mean_tar_gt_250_pct=round(_mean_or_zero([s.tar_gt_250_pct for s in high_conf]), 1),
        mean_glucose_mgdl=round(_mean_or_zero([s.mean_glucose_mgdl for s in high_conf]), 1),
        mean_gmi_pct=round(_mean_or_zero([s.gmi_pct for s in high_conf]), 2),
        mean_cv_pct=round(_mean_or_zero([s.cv_pct for s in high_conf]), 1),
        mean_tdd_units=round(_mean_or_zero([s.tdd_units for s in in_window]), 2),
        mean_manual_bolus_units=round(
            _mean_or_zero([s.bolus_units for s in in_window]), 2
        ),
        mean_loop_delivered_units=round(
            _mean_or_zero([s.basal_units for s in in_window]), 2
        ),
        mean_carbs_g=round(_mean_or_zero([s.total_carbs_g for s in in_window]), 0),
        n_level_1_hypos=sum(s.n_hypo_level_1 for s in in_window),
        n_level_2_hypos=sum(s.n_hypo_level_2 for s in in_window),
    )


def _render_period_table(p: PeriodRollup, label: str) -> str:
    return f"""## {label} ({p.start.isoformat()} → {p.end.isoformat()})

_{p.days_with_data}/{p.n_days} days have data; {p.low_confidence_days} excluded from glycemic averages for low CGM coverage._

| Metric | Value |
|---|---|
| Mean TIR (70–180) | **{p.mean_tir_70_180_pct}%** |
| Mean glucose | {p.mean_glucose_mgdl} mg/dL |
| GMI (estimated A1c) | **{p.mean_gmi_pct}%** |
| CV (variability) | {p.mean_cv_pct}% |
| Time below 70 | {p.mean_tbr_lt_70_pct}%   (below 54: {p.mean_tbr_lt_54_pct}%) |
| Time above 180 | {p.mean_tar_gt_180_pct}%   (above 250: {p.mean_tar_gt_250_pct}%) |
| Mean TDD insulin | {p.mean_tdd_units} U/day |
| Mean manual bolus | {p.mean_manual_bolus_units} U/day |
| Mean Loop-delivered | {p.mean_loop_delivered_units} U/day |
| Mean carbs logged | {p.mean_carbs_g:.0f} g/day |
| **Hypo events: level 1** | {p.n_level_1_hypos} |
| **Hypo events: level 2** | **{p.n_level_2_hypos}** |
"""


def _render_recent_glance(summaries: list[DailyLoopSummary], n: int = 14) -> str:
    recent = sorted(summaries, key=lambda s: s.local_date, reverse=True)[:n]
    if not recent:
        return ""

    rows = ["| Date | TIR % | GMI % | Mean | TDD | Hypos | Notes |", "|---|---|---|---|---|---|---|"]
    for s in recent:
        hypos = ""
        if s.n_hypo_level_2 > 0:
            hypos = f"L2×{s.n_hypo_level_2}"
            if s.n_hypo_level_1 > 0:
                hypos += f", L1×{s.n_hypo_level_1}"
        elif s.n_hypo_level_1 > 0:
            hypos = f"L1×{s.n_hypo_level_1}"

        notes = []
        if s.low_confidence:
            notes.append("low CGM coverage")
        if s.is_travel_day:
            notes.append("travel")
        note_str = "; ".join(notes)

        rows.append(
            f"| {s.local_date.isoformat()} | {s.tir_70_180_pct} | {s.gmi_pct} | "
            f"{s.mean_glucose_mgdl} | {s.tdd_units} | {hypos} | {note_str} |"
        )
    return "## Recent days at a glance\n\n" + "\n".join(rows) + "\n"


def render_loop_telemetry(
    summaries: list[DailyLoopSummary],
    today: date,
    person_tz: str = "America/Los_Angeles",
) -> str:
    """Render the loop_telemetry.md content from a set of daily summaries.

    Trailing periods anchor to `today` (inclusive). The "All-time" section is
    included when the retained data span is larger than 30 days.
    """
    if not summaries:
        return (
            "# Loop telemetry\n\n"
            "_No daily summaries available. Run a Tidepool sync._\n"
        )

    week_start = today - timedelta(days=6)
    month_start = today - timedelta(days=29)
    week_rollup = compute_period(summaries, week_start, today)
    month_rollup = compute_period(summaries, month_start, today)

    span_days = (
        max(s.local_date for s in summaries) - min(s.local_date for s in summaries)
    ).days

    sections = [
        "# Loop telemetry",
        "",
        f"_Auto-generated by mychart-sync from Tidepool. Anchor date: {today.isoformat()} ({person_tz})._",
        "_Daily detail at `loop/daily/YYYY-MM-DD.md`; raw records at `loop/raw/YYYY-MM-DD.jsonl`._",
        "_Low-confidence days (<70% CGM coverage) are excluded from glycemic averages but counted in the day tally._",
        "",
        _render_period_table(week_rollup, "Last 7 days"),
        _render_period_table(month_rollup, "Last 30 days"),
    ]

    if span_days > 30:
        all_start = min(s.local_date for s in summaries)
        all_rollup = compute_period(summaries, all_start, today)
        sections.append(
            _render_period_table(all_rollup, f"All-time ({all_rollup.n_days} days)")
        )

    glance = _render_recent_glance(summaries)
    if glance:
        sections.append(glance)

    return "\n".join(sections)


def write_loop_telemetry(
    summaries: Iterable[DailyLoopSummary],
    out_path: Path,
    today: date,
    person_tz: str = "America/Los_Angeles",
) -> Path:
    """Full-overwrite write of loop_telemetry.md. Idempotent for fixed inputs."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_loop_telemetry(list(summaries), today, person_tz=person_tz)
    out_path.write_text(content)
    return out_path
