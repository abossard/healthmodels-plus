#!/usr/bin/env bash
# The backup-restore showcase on anbotrai-experience, one step per call: steps.sh <step>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"
S="$ROOT/docs/arrange-showcase/scripts/showcase.sh"
LOG="$ROOT/docs/arrange-showcase/logs/anbotrai-experience"
M=anbotrai-experience
export AZURE_EXTENSION_DIR="$ROOT/.azext" AZURE_CORE_USE_COMMAND_INDEX=false
source "$ROOT/docs/arrange-showcase/.env"
decisions() { grep -E '^(INFO|WARNING|ERROR): ' | grep -vE "^INFO: ( |\{|\[|None|Request|Response|Command ran|Starting)|in preview" || true; }
# import with the extension's decisions, like showcase.sh arrange does
import() {
  az monitor health-models layout import --subscription "$SUBSCRIPTION" -g "$RESOURCE_GROUP" \
    -n "$M" --layout "$2" "${@:3}" --verbose -o table 2> >(decisions > "$LOG.backup-$1.log")
  wait; cat "$LOG.backup-$1.log"
}
# 0 and 0.0 are the same position
norm() { jq -S 'map((.x, .y) |= (if . == null then . else . + 0 end)) | sort_by(.name)' "$1"; }
same() { diff <(norm "$1") <(norm "$2") && echo "same positions: $(basename "$1") $(basename "$2")"; }
case "$1" in
  prepare)  import prepare "$HERE/prepare.json" ;;
  backup)   "$S" export "$M" "$HERE/backup.json"; same "$HERE/backup.json" "$HERE/prepare.json" ;;
  dry)      "$S" arrange "$M" backup-arrange-dry --dry-run; "$S" export "$M" "$HERE/after-dry.json"; same "$HERE/after-dry.json" "$HERE/backup.json" ;;
  arrange)  "$S" arrange "$M" backup-arrange ;;
  again)    "$S" arrange "$M" backup-arrange-again ;;
  preview)  import import-dry "$HERE/backup.json" --dry-run ;;
  stale)    import stale "$HERE/stale.json" --dry-run ;;
  import)   import import "$HERE/backup.json"; "$S" export "$M" "$HERE/after-import.json"; same "$HERE/after-import.json" "$HERE/backup.json" ;;
  restore)  import restore "$ROOT/docs/arrange-showcase/layouts/$M.mangled.json"; "$S" export "$M" "$HERE/after-restore.json"
            same "$HERE/after-restore.json" "$ROOT/docs/arrange-showcase/layouts/$M.mangled.json" ;;
esac
