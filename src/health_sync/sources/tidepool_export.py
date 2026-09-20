from __future__ import annotations

"""Read Tidepool JSON exports, optionally filtering by time window."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

from health_sync.parsers.loop import parse_time

logger = logging.getLogger(__name__)


class TidepoolExportSource:
    """Load an export once and yield its dated records."""

    def __init__(self, export_file: Path) -> None:
        self.export_file = export_file
        self._records: list[dict] | None = None

    def _load(self) -> list[dict]:
        if self._records is None:
            logger.info(f"Loading Tidepool export: {self.export_file}")
            with open(self.export_file) as f:
                data = json.load(f)
            if not isinstance(data, list) or not all(isinstance(record, dict) for record in data):
                raise ValueError(
                    "Expected a JSON array of Tidepool records"
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
            if not isinstance(time_str, str) or not time_str:
                continue
            try:
                t = parse_time(time_str)
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
