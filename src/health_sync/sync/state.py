from __future__ import annotations

"""Person-scoped sync state tracking for incremental fetches.

Persists last sync timestamps and resource hashes per provider/person so
subsequent syncs only fetch new/changed data.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional


class SyncState:
    """Tracks sync state for one person's providers."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _state_path(self, slug: str) -> Path:
        return self.state_dir / f"{slug}_state.json"

    def load(self, slug: str) -> dict[str, Any]:
        """Load sync state for a provider."""
        path = self._state_path(slug)
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def save(self, slug: str, state: dict[str, Any]) -> None:
        """Save sync state for a provider."""
        path = self._state_path(slug)
        path.write_text(json.dumps(state, indent=2))

    def get_last_sync(self, slug: str) -> Optional[str]:
        """Get ISO timestamp of last successful sync, or None."""
        state = self.load(slug)
        return state.get("last_sync")

    def record_sync(
        self,
        slug: str,
        resource_counts: dict[str, int],
        resource_hashes: Optional[dict[str, str]] = None,
    ) -> None:
        """Record a successful sync."""
        state = self.load(slug)
        state["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["last_resource_counts"] = resource_counts
        if resource_hashes:
            state["resource_hashes"] = resource_hashes
        # Keep history of last 10 syncs
        history = state.get("history", [])
        history.append({
            "timestamp": state["last_sync"],
            "total_resources": sum(resource_counts.values()),
        })
        state["history"] = history[-10:]
        self.save(slug, state)

    def has_changes(
        self,
        slug: str,
        new_hashes: dict[str, str],
    ) -> tuple[bool, list[str]]:
        """Check if resources have changed since last sync.

        Args:
            slug: Provider slug.
            new_hashes: Dict of resource type → content hash.

        Returns:
            (has_changes, changed_types) tuple.
        """
        state = self.load(slug)
        old_hashes = state.get("resource_hashes", {})

        if not old_hashes:
            return True, list(new_hashes.keys())

        changed = []
        for rtype, new_hash in new_hashes.items():
            if old_hashes.get(rtype) != new_hash:
                changed.append(rtype)

        return len(changed) > 0, changed


def hash_resources(resources: list[dict[str, Any]]) -> str:
    """Compute a stable hash of a list of FHIR resources."""
    # Sort by ID for stability, then hash the JSON
    filtered = [r for r in resources if r.get("resourceType") != "OperationOutcome"]
    sorted_resources = sorted(filtered, key=lambda r: r.get("id", ""))
    content = json.dumps(sorted_resources, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]
