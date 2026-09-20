from __future__ import annotations

"""Protocol for diabetes data sources.

A DiabetesDataSource yields type-tagged records (Tidepool record format) for a
given time window. Each record is a dict with at least 'type' and 'time'.

Implementations:
- TidepoolExportSource (sources/tidepool_export.py) — reads a JSON export file
- TidepoolClient (sources/tidepool_client.py, Phase 2) — legacy session-token API

The downstream parser/rollup/writer pipeline is source-agnostic.
"""

from datetime import datetime
from typing import Iterator, Protocol, runtime_checkable


@runtime_checkable
class DiabetesDataSource(Protocol):
    """A source of typed diabetes records within a time window."""

    def fetch(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[dict]:
        """Yield records with record['time'] in [start, end].

        Args:
            start: Inclusive start of the window. If None, no lower bound.
            end: Exclusive end of the window. If None, no upper bound.

        Yields:
            Tidepool-format record dicts. Each has 'type' and 'time' (ISO8601 UTC).
        """
        ...

    def close(self) -> None:
        """Release any resources (file handles, HTTP sessions)."""
        ...
