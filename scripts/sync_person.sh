#!/bin/bash
# Explicit person routing; continue after individual provider failures.
set -uo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT/src"

usage() {
    echo "Usage: bash scripts/sync_person.sh PERSON PROVIDER... [-- --dry-run --full]" >&2
    exit 2
}

(( $# >= 2 )) || usage
PERSON="$1"
[[ "$PERSON" =~ ^[a-zA-Z0-9_-]+$ && "$PERSON" != -* ]] || usage
shift
PROVIDERS=()
while (( $# > 0 )) && [[ "$1" != "--" ]]; do
    [[ "$1" =~ ^[a-z0-9][a-z0-9-]*$ ]] || usage
    PROVIDERS+=("$1")
    shift
done
(( ${#PROVIDERS[@]} > 0 )) || usage
if (( $# > 0 )); then shift; fi
for arg in "$@"; do
    case "$arg" in --dry-run|--full) ;; *) usage ;; esac
done

FAILED=()
for provider in "${PROVIDERS[@]}"; do
    echo "==> Syncing $provider for $PERSON"
    if .venv/bin/python -m health_sync.cli sync --provider "$provider" --person "$PERSON" "$@"; then
        echo "==> $provider complete"
    else
        status=$?
        FAILED+=("$provider:$status")
        echo "==> $provider failed with exit code $status" >&2
    fi
done
if (( ${#FAILED[@]} > 0 )); then
    echo "Sync completed with failures: ${FAILED[*]}" >&2
    exit 1
fi
echo "Sync complete."
