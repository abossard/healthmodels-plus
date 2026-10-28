#!/usr/bin/env bash
# Repeatable steps of the arrange showcase. Run from anywhere; paths are relative to this folder.
#
#   showcase.sh setup                          build the extension and install it into .azext/
#   showcase.sh save <model>                   export the current layout to layouts/<model>.mangled.json (once)
#   showcase.sh restore <model> [file]         import layouts/<model>.mangled.json, or file
#   showcase.sh export <model> <file>          export the current layout
#   showcase.sh import <model> <file>          import a layout
#   showcase.sh arrange <model> <tag> [args]   arrange (args go to az, e.g. --engine tree), keep the
#                                              decision log in logs/<model>.<tag>.log
#   showcase.sh url <model> designer|graph     portal URL of the designer or graph view
#   showcase.sh lock <model> <owner>           take the model, wait up to 9 minutes while another owner has it
#   showcase.sh unlock <model> <owner>         give the model back
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
export AZURE_EXTENSION_DIR="$ROOT/.azext"
# several sessions run az at once with their own extension dir; a shared command index goes stale
export AZURE_CORE_USE_COMMAND_INDEX=false
source "$HERE/.env"  # SUBSCRIPTION, RESOURCE_GROUP, TENANT, not committed
LOCKS="$ROOT/.locks"

layout() { az monitor health-models layout "$@" --subscription "$SUBSCRIPTION" -g "$RESOURCE_GROUP"; }

# keep the extension's own decisions from --verbose, drop the Azure CLI's request and response log
decisions() { grep -E '^(INFO|WARNING|ERROR): ' | grep -vE "^INFO: ( |\{|\[|None|Request|Response|Command ran|Starting)|in preview" || true; }

cmd="${1:?command}"; model="${2:-}"
case "$cmd" in
  setup)
    (cd "$ROOT/src/healthmodels-plus" && uv build --wheel)
    az extension add --yes --upgrade --source "$(ls -t "$ROOT"/src/healthmodels-plus/dist/healthmodels_plus-*.whl | head -1)"
    ;;
  save)
    out="$HERE/layouts/$model.mangled.json"
    [[ -e "$out" ]] && { echo "keeping $out, delete it to save again" >&2; exit 0; }
    mkdir -p "$HERE/layouts"
    layout export -n "$model" -o json > "$out"
    echo "saved $out"
    ;;
  restore)
    layout import -n "$model" --layout "${3:-$HERE/layouts/$model.mangled.json}" -o table
    ;;
  export)
    layout export -n "$model" -o json > "${3:?file}"
    ;;
  import)
    layout import -n "$model" --layout "${3:?file}" -o table
    ;;
  arrange)
    tag="${3:?tag}"; shift 3
    mkdir -p "$HERE/logs"
    layout arrange -n "$model" "$@" --verbose -o table 2> >(decisions > "$HERE/logs/$model.$tag.log")
    wait
    cat "$HERE/logs/$model.$tag.log"
    ;;
  url)
    base="https://portal.azure.com/#@$TENANT/resource/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CloudHealth/healthmodels/$model"
    case "${3:?designer|graph}" in
      designer) echo "$base/designer" ;;
      graph) echo "$base/graph" ;;
    esac
    ;;
  lock)
    owner="${3:?owner}"; mkdir -p "$LOCKS"
    for _ in $(seq 54); do
      if mkdir "$LOCKS/$model" 2>/dev/null; then echo "$owner" > "$LOCKS/$model/owner"; echo "locked $model for $owner"; exit 0; fi
      [[ "$(cat "$LOCKS/$model/owner" 2>/dev/null)" == "$owner" ]] && { echo "$owner already has $model"; exit 0; }
      sleep 10
    done
    echo "$model is still locked by $(cat "$LOCKS/$model/owner"), try again later" >&2; exit 1
    ;;
  unlock)
    owner="${3:?owner}"
    [[ "$(cat "$LOCKS/$model/owner" 2>/dev/null)" == "$owner" ]] && rm -rf "${LOCKS:?}/$model" && echo "unlocked $model"
    ;;
  *) echo "unknown command $cmd" >&2; exit 2 ;;
esac
