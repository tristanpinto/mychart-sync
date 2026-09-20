from __future__ import annotations

import json

from health_sync.migrate import run_v2_multi_tenant_migration


def test_v2_multi_tenant_migration_moves_default_person_and_skips_sandbox(
    app_config,
    providers_file,
    person_a_legacy_state,
) -> None:
    root = person_a_legacy_state

    dry_run = run_v2_multi_tenant_migration(config=app_config, dry_run=True)
    assert "To rollback this migration after running" in dry_run.output
    assert "mount-sinai_token.json" in dry_run.output
    assert "epic-sandbox_token.json" in dry_run.output

    result = run_v2_multi_tenant_migration(config=app_config)
    assert result.output.startswith("Will move:")
    assert "Moved 1 token file(s)" in result.output
    assert "Moved 1 state file(s)" in result.output
    assert "Deleted 1 fhir cache tree(s)" in result.output

    moved_token = root / "secrets" / "tokens" / "person_a" / "mount-sinai_token.json"
    moved_state = root / "cache" / "state" / "person_a" / "mount-sinai_state.json"
    sandbox_token = root / "secrets" / "tokens" / "epic-sandbox_token.json"
    sandbox_state = root / "cache" / "epic-sandbox_state.json"
    legacy_cache = root / "cache" / "fhir" / "mount-sinai"
    legacy_snapshot = (
        root
        / "brain"
        / "local_snapshots"
        / "outputs"
        / ".pre_sync_snapshots"
        / "2026-04-09"
        / "clinical_extract.md"
    )

    assert moved_token.exists()
    assert json.loads(moved_token.read_text())["person"] == "person_a"
    assert moved_state.exists()
    assert sandbox_token.exists()
    assert sandbox_state.exists()
    assert not legacy_cache.exists()
    assert legacy_snapshot.exists()

    second = run_v2_multi_tenant_migration(config=app_config, dry_run=True)
    assert second.plan.is_noop()
    assert "no moved files to roll back" in second.output
