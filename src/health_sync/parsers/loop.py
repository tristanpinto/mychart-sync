from __future__ import annotations

"""Parse Tidepool diabetes records into a normalized internal form.

Supported Tidepool record types:
- cbg: continuous glucose (Dexcom)
- smbg: fingerstick (rare for Loop users)
- bolus: insulin bolus (sum 'normal' + optional 'extended')
- basal: insulin basal segment (rate × duration; duration may need forward-fill)
- food: carb entry; 'nutrition' is a JSON-encoded string

Daily binning uses each record's own 'timezoneOffset' (minutes from UTC) when
present; falls back to the person's configured timezone when missing.
"""

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Iterator, Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# mg/dL = mmol/L × 18.0182 (ADA standard conversion)
MMOL_TO_MGDL = 18.0182


@dataclass
class NormalizedLoopRecord:
    """One Tidepool record, normalized for rollup consumption."""

    kind: Literal["cgm", "bolus", "basal", "food"]
    time_utc: datetime
    local_date: date
    raw: dict  # original record, preserved for JSONL writer

    # Type-specific fields (None for irrelevant types)
    glucose_mgdl: float | None = None       # cgm
    insulin_units: float | None = None      # bolus (normal + extended)
    bolus_subtype: str | None = None        # bolus: 'normal' (manual) | 'automated' (Loop micro-bolus)
    rate_units_per_hour: float | None = None  # basal
    duration_minutes: float | None = None   # basal (0 if open-ended; rollup forward-fills)
    delivery_type: str | None = None        # basal: scheduled/automated/suspend
    open_ended: bool = False                # basal: duration was 0 or unknown
    carbs_g: float | None = None            # food

    # CGM sample-interval info (for hypo detection sample-spacing checks)
    cgm_interval_seconds: float | None = field(default=None)


def parse_time(time_str: str) -> datetime:
    """Parse Tidepool ISO 8601 to tz-aware UTC datetime.

    Tidepool emits 'YYYY-MM-DDTHH:MM:SS.fffZ' with variable fractional precision.
    """
    if time_str.endswith("Z"):
        time_str = time_str[:-1] + "+00:00"
    return datetime.fromisoformat(time_str).astimezone(timezone.utc)


def compute_local_date(record: dict, fallback_tz: str = "America/Los_Angeles") -> date:
    """Compute the local-tz date for a record.

    Prefers the record's own timezoneOffset (per Tidepool spec, minutes from UTC).
    Falls back to fallback_tz when timezoneOffset is missing.
    """
    time_utc = parse_time(record["time"])
    offset = record.get("timezoneOffset")
    if offset is not None:
        # offset is signed minutes from UTC; negative for the Americas
        local_dt = time_utc + timedelta(minutes=offset)
        return local_dt.date()
    return time_utc.astimezone(ZoneInfo(fallback_tz)).date()


def _parse_cgm(record: dict, fallback_tz: str) -> NormalizedLoopRecord | None:
    """Parse a cbg or smbg record."""
    value = record.get("value")
    if value is None:
        return None
    units = record.get("units", "mg/dL")
    if units == "mmol/L":
        glucose_mgdl = round(float(value) * MMOL_TO_MGDL, 1)
    else:
        glucose_mgdl = round(float(value), 1)

    return NormalizedLoopRecord(
        kind="cgm",
        time_utc=parse_time(record["time"]),
        local_date=compute_local_date(record, fallback_tz),
        raw=record,
        glucose_mgdl=glucose_mgdl,
    )


def _parse_bolus(record: dict, fallback_tz: str) -> NormalizedLoopRecord | None:
    """Parse a bolus record.

    subType distinguishes:
    - 'normal' / 'square' / 'dual' — user-issued manual bolus
    - 'automated' — Loop algorithm-issued micro-bolus (counts toward basal
      delivery in Tidepool's classification, since it's not user-decided)
    """
    amounts = [record[key] for key in ("normal", "extended") if key in record]
    if not amounts or any(value is None or isinstance(value, bool) for value in amounts):
        return None
    doses = [float(value) for value in amounts]
    if any(not math.isfinite(value) or value < 0 for value in doses):
        return None
    # Keep explicit zero corrections so they can replace earlier values by ID.
    total = sum(doses)

    return NormalizedLoopRecord(
        kind="bolus",
        time_utc=parse_time(record["time"]),
        local_date=compute_local_date(record, fallback_tz),
        raw=record,
        insulin_units=total,
        bolus_subtype=record.get("subType"),
    )


def _parse_basal(record: dict, fallback_tz: str) -> NormalizedLoopRecord | None:
    """Parse a basal segment.

    Tidepool basal records have:
    - rate: U/hr
    - duration: minutes (the API source converts from milliseconds)
    - deliveryType: 'scheduled' | 'automated' | 'suspend'
    - annotations (JSON string): may contain {'code':'basal/unknown-duration'}

    For suspend records, the pump is off — contributes zero insulin.
    For records with duration=0 or unknown-duration annotation, we flag open_ended;
    the rollup forward-fills duration from the successor record's time.

    """
    delivery_type = record.get("deliveryType")
    if delivery_type not in ("scheduled", "automated", "suspend"):
        return None

    rate = float(record.get("rate", 0) or 0)
    raw_duration = record.get("duration", 0) or 0
    duration_minutes = float(raw_duration)

    annotations_str = record.get("annotations") or "[]"
    try:
        annotations = json.loads(annotations_str)
    except (json.JSONDecodeError, TypeError):
        annotations = []
    open_ended = duration_minutes == 0 or any(
        a.get("code") == "basal/unknown-duration" for a in annotations
    )

    return NormalizedLoopRecord(
        kind="basal",
        time_utc=parse_time(record["time"]),
        local_date=compute_local_date(record, fallback_tz),
        raw=record,
        rate_units_per_hour=rate,
        duration_minutes=duration_minutes,
        delivery_type=delivery_type,
        open_ended=open_ended,
    )


def _parse_food(record: dict, fallback_tz: str) -> NormalizedLoopRecord | None:
    """Parse a food record (carb entry).

    nutrition field is a JSON-encoded string like:
    '{"carbohydrate":{"net":20,"units":"grams"},"estimatedAbsorptionDuration":10800}'
    """
    nutrition_raw = record.get("nutrition")
    if not nutrition_raw:
        return None
    try:
        nutrition = (
            json.loads(nutrition_raw) if isinstance(nutrition_raw, str) else nutrition_raw
        )
    except (json.JSONDecodeError, TypeError):
        return None

    carb_block = nutrition.get("carbohydrate") or {}
    net_carbs = carb_block.get("net")
    if net_carbs is None:
        return None

    return NormalizedLoopRecord(
        kind="food",
        time_utc=parse_time(record["time"]),
        local_date=compute_local_date(record, fallback_tz),
        raw=record,
        carbs_g=float(net_carbs),
    )


# Dispatch table keyed by Tidepool record type
_PARSERS = {
    "cbg": _parse_cgm,
    "smbg": _parse_cgm,
    "bolus": _parse_bolus,
    "basal": _parse_basal,
    "food": _parse_food,
}

# Unsupported types are omitted from both summaries and generated JSONL.
_KNOWN_IGNORED = {
    "pumpStatus",
    "controllerStatus",
    "dosingDecision",
    "deviceEvent",
    "cgmSettings",
    "pumpSettings.insulinSensitivities",
    "pumpSettings.carbRatios",
    "pumpSettings.basalSchedules",
    "pumpSettings.bgTargets",
    "alert",
}


def parse_records(
    records: Iterable[dict], fallback_tz: str = "America/Los_Angeles"
) -> Iterator[NormalizedLoopRecord]:
    """Parse an iterable of Tidepool records into normalized form.

    Unknown types log a warning once per type per call (to avoid spam).
    Known-ignored types are silently skipped.
    """
    seen_unknown: set[str] = set()
    for record in records:
        rtype = record.get("type")
        if rtype is None:
            continue
        if rtype in _KNOWN_IGNORED:
            continue
        parser = _PARSERS.get(rtype)
        if parser is None:
            if rtype not in seen_unknown:
                logger.warning(f"Unknown Tidepool record type, skipping: {rtype}")
                seen_unknown.add(rtype)
            continue
        try:
            normalized = parser(record, fallback_tz)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse {rtype} record: {e}")
            continue
        if normalized is not None:
            yield normalized
