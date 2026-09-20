"""Atomic generated Markdown with backups for preexisting or edited files."""

import hashlib
import logging
from pathlib import Path

from health_sync.auth.storage import write_private_text

logger = logging.getLogger(__name__)
MARKER = "<!-- mychart-sync generated; sha256:"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_generated(path: Path, body: str) -> str:
    text = f"{MARKER}{_digest(body)} -->\n{body}"
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if old == text:
            return text
        first, separator, rest = old.partition("\n")
        untouched = separator and first == f"{MARKER}{_digest(rest)} -->"
        if not untouched:
            backup = path.with_name(f"{path.name}.backup-{_digest(old)[:16]}")
            if backup.exists() and backup.read_text(encoding="utf-8") != old:
                raise RuntimeError(
                    f"Backup conflict at {backup}; summary left unchanged"
                )
            write_private_text(backup, old)
            logger.warning(
                "Preserved existing or edited summary in %s; keep manual notes in health_profile.md",
                backup,
            )
    write_private_text(path, text)
    return text
