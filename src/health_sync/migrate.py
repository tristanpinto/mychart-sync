from __future__ import annotations

"""One-shot migrations for chartstash local state."""

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import health_sync.config as config_module
from health_sync.config import AppConfig, load_config
from health_sync.providers.registry import ProviderRegistry


SKIP_SLUGS = {"epic-sandbox"}


@dataclass
class MigrationPlan:
    token_moves: list[tuple[Path, Path]] = field(default_factory=list)
    state_moves: list[tuple[Path, Path]] = field(default_factory=list)
    cache_deletes: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)

    def is_noop(self) -> bool:
        return not self.token_moves and not self.state_moves and not self.cache_deletes


@dataclass
class MigrationResult:
    plan: MigrationPlan
    output: str


def run_v2_multi_tenant_migration(
    *,
    config: AppConfig | None = None,
    dry_run: bool = False,
) -> MigrationResult:
    """Migrate legacy token/state layout into person-scoped directories."""
    if config is None:
        config = load_config()

    plan = build_v2_multi_tenant_plan(config)
    output_lines = render_plan(plan, dry_run=dry_run)

    if not dry_run and not plan.is_noop():
        _apply_plan(plan, person=config.default_person)
        output_lines.extend(
            [
                f"Moved {len(plan.token_moves)} token file(s)",
                f"Moved {len(plan.state_moves)} state file(s)",
                f"Deleted {len(plan.cache_deletes)} fhir cache tree(s)",
                "Migration complete.",
            ]
        )
    elif not dry_run:
        output_lines.append("Migration complete. No legacy files to move.")

    return MigrationResult(plan=plan, output="\n".join(output_lines))


def build_v2_multi_tenant_plan(config: AppConfig) -> MigrationPlan:
    root = config_module._project_root()
    registry = ProviderRegistry(root / "config" / "providers.json")
    providers = {p.slug: p for p in registry.list_all()}
    default_person = config.default_person

    legacy_tokens_dir = root / "secrets" / "tokens"
    legacy_state_dir = root / "cache"
    legacy_fhir_dir = root / "cache" / "fhir"
    person_tokens_dir = root / "secrets" / "tokens" / default_person
    person_state_dir = root / "cache" / "state" / default_person

    slugs: set[str] = set()
    slugs.update(
        p.stem.removesuffix("_token")
        for p in legacy_tokens_dir.glob("*_token.json")
        if p.is_file()
    )
    slugs.update(
        p.stem.removesuffix("_state")
        for p in legacy_state_dir.glob("*_state.json")
        if p.is_file()
    )

    plan = MigrationPlan()
    for slug in sorted(slugs):
        token_path = legacy_tokens_dir / f"{slug}_token.json"
        state_path = legacy_state_dir / f"{slug}_state.json"
        fhir_cache = legacy_fhir_dir / slug

        if slug in SKIP_SLUGS:
            if token_path.exists():
                plan.skipped.append(token_path)
            if state_path.exists():
                plan.skipped.append(state_path)
            continue

        provider = providers.get(slug)
        if provider is None or provider.patient != default_person:
            if token_path.exists():
                plan.skipped.append(token_path)
            if state_path.exists():
                plan.skipped.append(state_path)
            continue

        if token_path.exists():
            plan.token_moves.append(
                (token_path, person_tokens_dir / f"{slug}_token.json")
            )
        if state_path.exists():
            plan.state_moves.append(
                (state_path, person_state_dir / f"{slug}_state.json")
            )
        if fhir_cache.exists():
            plan.cache_deletes.append(fhir_cache)

    return plan


def render_plan(plan: MigrationPlan, *, dry_run: bool) -> list[str]:
    prefix = "Would move" if dry_run else "Will move"
    lines: list[str] = [f"{prefix}:"]
    if plan.token_moves or plan.state_moves:
        for src, dst in [*plan.token_moves, *plan.state_moves]:
            lines.append(f"  {src} -> {dst}")
    else:
        lines.append("  (none)")

    lines.append("Would delete (cache reconstructable):" if dry_run else "Will delete (cache reconstructable):")
    if plan.cache_deletes:
        for path in plan.cache_deletes:
            entry_count = sum(1 for _ in path.rglob("*")) if path.exists() else 0
            lines.append(f"  {path}/ ({entry_count} entries)")
    else:
        lines.append("  (none)")

    lines.append("Would skip (not migrated):" if dry_run else "Will skip (not migrated):")
    if plan.skipped:
        for path in plan.skipped:
            lines.append(f"  {path}")
    else:
        lines.append("  (none)")

    lines.extend(["", "To rollback this migration after running, execute:"])
    if plan.token_moves or plan.state_moves:
        for src, dst in [*plan.token_moves, *plan.state_moves]:
            lines.append(f"  mv {dst} {src}")
        cleanup_dirs = sorted({dst.parent for _, dst in [*plan.token_moves, *plan.state_moves]})
        lines.append("  rmdir " + " ".join(str(path) for path in cleanup_dirs))
    else:
        lines.append("  # no moved files to roll back")
    lines.append("")
    lines.append("(Deleted FHIR cache will rebuild on next sync; no rollback needed.)")
    return lines


def _apply_plan(plan: MigrationPlan, *, person: str) -> None:
    for src, dst in plan.token_moves:
        if dst.exists():
            raise FileExistsError(f"Destination already exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(src.read_text())
        data["person"] = person
        src.write_text(json.dumps(data, indent=2) + "\n")
        src.rename(dst)

    for src, dst in plan.state_moves:
        if dst.exists():
            raise FileExistsError(f"Destination already exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    for path in plan.cache_deletes:
        shutil.rmtree(path)
