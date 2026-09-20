from __future__ import annotations

"""Person-scoped checkpoints for incremental fetches."""

import json
import time
from pathlib import Path
from typing import Any, Optional

from health_sync.auth.storage import write_private_text


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
        write_private_text(path, json.dumps(state, indent=2))

    def get_last_sync(self, slug: str) -> Optional[str]:
        """Get ISO timestamp of last successful sync, or None."""
        state = self.load(slug)
        return state.get("last_sync")

    def record_sync(
        self,
        slug: str,
        resource_counts: dict[str, int],
        *,
        synced_at: str | None = None,
    ) -> None:
        """Record a successful sync."""
        state = self.load(slug)
        state["last_sync"] = synced_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["last_resource_counts"] = resource_counts
        # Keep history of last 10 syncs
        history = state.get("history", [])
        history.append({
            "timestamp": state["last_sync"],
            "total_resources": sum(resource_counts.values()),
        })
        state["history"] = history[-10:]
        self.save(slug, state)
