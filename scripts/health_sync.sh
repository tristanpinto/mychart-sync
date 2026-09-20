#!/bin/bash
# Optional local change-signal wrapper (requires jq).
# Set PERSON and OUTPUT_DIR to match config/app.json. Individual file paths
# and SIGNAL_FILE can also be overridden. Does not install a schedule.
#
# Usage:
#   bash scripts/health_sync.sh
#   bash scripts/health_sync.sh --full

set -uo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEALTH_SYNC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="$HEALTH_SYNC_DIR/.venv/bin/python"
OUTPUT_DIR="${OUTPUT_DIR:-$HEALTH_SYNC_DIR/output}"
PERSON="${PERSON:-me}"
SIGNAL_FILE="${SIGNAL_FILE:-$OUTPUT_DIR/.sync_changes.json}"

# Canonical output files written by health_sync parsers
CLINICAL_FILE="${CLINICAL_FILE:-$OUTPUT_DIR/clinical_extract.md}"
HEALTH_FILE="${HEALTH_FILE:-$OUTPUT_DIR/health_profile.md}"
LABS_FILE="${LABS_FILE:-$OUTPUT_DIR/lab_results.md}"
RECORDS_DIR="${RECORDS_DIR:-$OUTPUT_DIR/documents}"

# macOS uses stat -f; Linux uses stat -c. Return 0 if the file is absent.
mtime() {
    if [[ ! -f "$1" ]]; then
        echo "0"
    elif stat -f "%m" "$1" >/dev/null 2>&1; then
        stat -f "%m" "$1"
    else
        stat -c "%Y" "$1" 2>/dev/null || echo "0"
    fi
}

for ((i = 1; i <= $#; i++)); do
    arg="${!i}"
    if [[ "$arg" == "--person" ]]; then
        next_index=$((i + 1))
        person_arg="${!next_index:-}"
        if [[ "$person_arg" != "$PERSON" ]]; then
            echo "health_sync.sh is scoped to $PERSON; refusing --person ${person_arg:-<missing>}" >&2
            exit 2
        fi
    elif [[ "$arg" == --person=* ]]; then
        person_arg="${arg#--person=}"
        if [[ "$person_arg" != "$PERSON" ]]; then
            echo "health_sync.sh is scoped to $PERSON; refusing --person $person_arg" >&2
            exit 2
        fi
    fi
done

# List downloaded clinical documents (non-markdown files in records/)
doc_snapshot() {
    find "$RECORDS_DIR" -maxdepth 1 \
        \( -name "*.txt" -o -name "*.pdf" -o -name "*.html" \) \
        2>/dev/null | sort || true
}

# ── Snapshot state before sync ───────────────────────────────────────────────
CM0=$(mtime "$CLINICAL_FILE")
HM0=$(mtime "$HEALTH_FILE")
LM0=$(mtime "$LABS_FILE")
DOCS_BEFORE=$(doc_snapshot)

# ── Run the sync ─────────────────────────────────────────────────────────────
SYNC_EXIT=0
cd "$HEALTH_SYNC_DIR"
PYTHONPATH=src "$PYTHON" -m health_sync.cli sync --person "$PERSON" "$@" 2>&1 || SYNC_EXIT=$?

# ── Detect changed canonical files ───────────────────────────────────────────
CHANGED_FILES_JSON='[]'
HAS_CHANGES="false"

if [[ "$(mtime "$CLINICAL_FILE")" != "$CM0" && -f "$CLINICAL_FILE" ]]; then
    CHANGED_FILES_JSON=$(echo "$CHANGED_FILES_JSON" | jq --arg v "$CLINICAL_FILE" '. + [$v]')
    HAS_CHANGES="true"
fi
if [[ "$(mtime "$HEALTH_FILE")" != "$HM0" && -f "$HEALTH_FILE" ]]; then
    CHANGED_FILES_JSON=$(echo "$CHANGED_FILES_JSON" | jq --arg v "$HEALTH_FILE" '. + [$v]')
    HAS_CHANGES="true"
fi
if [[ "$(mtime "$LABS_FILE")" != "$LM0" && -f "$LABS_FILE" ]]; then
    CHANGED_FILES_JSON=$(echo "$CHANGED_FILES_JSON" | jq --arg v "$LABS_FILE" '. + [$v]')
    HAS_CHANGES="true"
fi

# ── Detect new downloaded documents ──────────────────────────────────────────
DOCS_AFTER=$(doc_snapshot)
NEW_DOCS_JSON='[]'
while IFS= read -r doc; do
    [[ -z "$doc" ]] && continue
    if ! echo "$DOCS_BEFORE" | grep -qxF "$doc" 2>/dev/null; then
        NEW_DOCS_JSON=$(echo "$NEW_DOCS_JSON" | jq --arg v "$doc" '. + [$v]')
        HAS_CHANGES="true"
    fi
done <<< "$DOCS_AFTER"

# ── Write signal file ─────────────────────────────────────────────────────────
mkdir -p "$(dirname "$SIGNAL_FILE")"
jq -n \
    --arg    date           "$(date -u +%Y-%m-%d)" \
    --arg    timestamp      "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" \
    --arg    person         "$PERSON" \
    --argjson has_changes   "$HAS_CHANGES" \
    --argjson changed_files "$CHANGED_FILES_JSON" \
    --argjson new_documents "$NEW_DOCS_JSON" \
    --argjson exit_code     "$SYNC_EXIT" \
    '{
        date:          $date,
        timestamp:     $timestamp,
        person:        $person,
        has_changes:   $has_changes,
        changed_files: $changed_files,
        new_documents: $new_documents,
        exit_code:     $exit_code
    }' > "$SIGNAL_FILE" 2>/dev/null || true

exit "$SYNC_EXIT"
