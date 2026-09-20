from __future__ import annotations

"""JSONL writer for raw Tidepool records, grouped by local date.

Each local date gets one .jsonl file: `{out_dir}/YYYY-MM-DD.jsonl`. One record
per line, sorted by (time, id), deduplicated by id. Rewriting a date produces
a byte-identical file given the same input records.
"""

import json
import logging
from datetime import date
from pathlib import Path

from health_sync.parsers.loop import NormalizedLoopRecord

logger = logging.getLogger(__name__)


def write_raw_jsonl(
    records: list[NormalizedLoopRecord],
    out_dir: Path,
    local_date: date,
) -> Path:
    """Write a day's worth of normalized records' RAW dicts to a JSONL file.

    Records are sorted by (time, id) and deduplicated by id. The output file
    contains one JSON object per line, with a final newline. Calling this
    function twice with the same inputs produces a byte-identical file.

    Returns the path written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{local_date.isoformat()}.jsonl"

    # Filter to the requested date and dedup by id (Tidepool may export the
    # same record multiple times if a pump resyncs the same window).
    day_records = [r for r in records if r.local_date == local_date]
    seen_ids: set[str] = set()
    deduped: list[NormalizedLoopRecord] = []
    for r in day_records:
        rid = r.raw.get("id")
        if rid is None:
            # Records without an id are rare but possible — keep them all,
            # since we can't dedup them.
            deduped.append(r)
            continue
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        deduped.append(r)

    # Stable sort by (time, id) for byte-identical output
    deduped.sort(key=lambda r: (r.time_utc, r.raw.get("id") or ""))

    lines = [
        json.dumps(r.raw, sort_keys=True, separators=(",", ":")) for r in deduped
    ]
    out_path.write_text("\n".join(lines) + "\n" if lines else "")
    return out_path


def also_write_unparseable_records(
    raw_records: list[dict],
    parsed_ids: set[str],
    out_dir: Path,
    local_date: date,
) -> Path | None:
    """Optional: archive records that the parser skipped (unknown types, etc.).

    Writes to `{out_dir}/YYYY-MM-DD.skipped.jsonl`. Returns the path if any
    records were skipped, None otherwise. Not used in MVP but available for
    Phase 2 debugging.
    """
    skipped = [r for r in raw_records if r.get("id") not in parsed_ids]
    if not skipped:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{local_date.isoformat()}.skipped.jsonl"
    skipped.sort(key=lambda r: (r.get("time") or "", r.get("id") or ""))
    lines = [json.dumps(r, sort_keys=True, separators=(",", ":")) for r in skipped]
    out_path.write_text("\n".join(lines) + "\n")
    return out_path
