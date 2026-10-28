#!/usr/bin/env bash
# Temporary demo-* cross-links on anbotrai-stability for the crossings showcase.
#   topology.sh add | remove | names
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../.env"
B=https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CloudHealth/healthmodels/anbotrai-stability
V=api-version=2026-11-01
case "${1:?add|remove|names}" in
  add)
    jq -c '.[]' "$HERE/demo.json" | while read -r r; do
      name=$(jq -r .name <<<"$r")
      az rest --method put --url "$B/relationships/$name?$V" \
        --body "$(jq -c '{properties: {parentEntityName: .p, childEntityName: .c}}' <<<"$r")" -o none
      echo "added $name"
    done ;;
  remove)
    for name in $(jq -r '.[].name' "$HERE/demo.json"); do
      az rest --method delete --url "$B/relationships/$name?$V" -o none
      echo "removed $name"
    done ;;
  names)  # entity and relationship names, sorted, to compare with the snapshot
    jq -n --argjson e "$(az rest --method get --url "$B/entities?$V")" \
          --argjson r "$(az rest --method get --url "$B/relationships?$V")" \
          '{entities: [$e.value[].name] | sort, relationships: [$r.value[].name] | sort}' ;;
esac
