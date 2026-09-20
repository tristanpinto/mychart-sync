from __future__ import annotations

"""JSONL writer for raw Tidepool records, grouped by local date.

Each local date gets one .jsonl file: `{out_dir}/YYYY-MM-DD.jsonl`. One record
per line, sorted by (time, id), deduplicated by id. Rewriting a date produces
a byte-identical file given the same input records.
"""

import json
from datetime import date
from pathlib import Path

from health_sync.auth.storage import write_private_text
from health_sync.parsers.loop import NormalizedLoopRecord

def deduplicate_records(
    records: list[NormalizedLoopRecord],
) -> list[NormalizedLoopRecord]:
    """Keep the first record for each ID, retaining records without IDs."""
    seen_ids: set[str] = set()
    result = []
    for record in records:
        rid = record.raw.get("id")
        if rid is not None:
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
        result.append(record)
    return result


def write_raw_jsonl(
    records: list[NormalizedLoopRecord],
    out_dir: Path,
    local_date: date,
) -> Path:
    """Atomically write a day's raw records; leave identical files untouched."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{local_date.isoformat()}.jsonl"

    # Filter to the requested date and dedup by id (Tidepool may export the
    # same record multiple times if a pump resyncs the same window).
    day_records = [r for r in records if r.local_date == local_date]
    deduped = deduplicate_records(day_records)

    # Stable sort by (time, id) for byte-identical output
    deduped.sort(key=lambda r: (r.time_utc, r.raw.get("id") or ""))

    lines = [
        json.dumps(r.raw, sort_keys=True, separators=(",", ":")) for r in deduped
    ]
    content = "\n".join(lines) + "\n" if lines else ""
    if out_path.exists() and out_path.read_text() == content:
        out_dir.chmod(0o700)
        out_path.chmod(0o600)
    else:
        write_private_text(out_path, content)
    return out_path
