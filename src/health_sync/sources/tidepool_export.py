from __future__ import annotations

"""Tidepool web-UI export file source.

Reads a JSON file exported from tidepool.org → Export Patient Data. The export
is a single JSON array of records. We load it once into memory (typical export
is ~40 MB for 90 days) and yield records that fall within the requested window.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


def _parse_time(time_str: str) -> datetime:
    """Parse Tidepool's ISO 8601 time string to a tz-aware UTC datetime.

    Tidepool times look like '2026-05-13T22:20:26.743Z' or '2026-05-13T22:05:46.09Z'.
    fromisoformat handles fractional seconds in Python 3.11+; for 3.10 we strip Z
    and append +00:00.
    """
    if time_str.endswith("Z"):
        time_str = time_str[:-1] + "+00:00"
    return datetime.fromisoformat(time_str).astimezone(timezone.utc)


class TidepoolExportSource:
    """DiabetesDataSource backed by a Tidepool web-UI JSON export."""

    def __init__(self, export_file: Path) -> None:
        self.export_file = export_file
        self._records: list[dict] | None = None

    def _load(self) -> list[dict]:
        if self._records is None:
            logger.info(f"Loading Tidepool export: {self.export_file}")
            with open(self.export_file) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(
                    f"Expected JSON array at top level of {self.export_file}, "
                    f"got {type(data).__name__}"
                )
            self._records = data
            logger.info(f"Loaded {len(data)} records from export")
        return self._records

    def fetch(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[dict]:
        """Yield records with record['time'] in [start, end)."""
        records = self._load()

        for record in records:
            time_str = record.get("time")
            if not time_str:
                continue
            try:
                t = _parse_time(time_str)
            except ValueError:
                logger.warning(f"Skipping record with unparseable time: {time_str}")
                continue

            if start is not None and t < start:
                continue
            if end is not None and t >= end:
                continue
            yield record

    def close(self) -> None:
        """Release the in-memory record list."""
        self._records = None
