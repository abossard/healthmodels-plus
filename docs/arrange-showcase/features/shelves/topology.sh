#!/usr/bin/env bash
# Temporary demo-* parts on anbotrai-stability for the shelves showcase: topology.sh snapshot|add|remove|verify
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../.env"
B="https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CloudHealth/healthmodels/anbotrai-stability"
V="api-version=2026-11-01"
ENTITIES=(
  "demo-booking|Demo booking (planned)"
  "demo-booking-api|Demo booking API"
  "demo-booking-db|Demo booking database"
  "demo-loyalty|Demo loyalty (planned)"
  "demo-loyalty-api|Demo loyalty API"
  "demo-orphan-a|Demo lone check A"
  "demo-orphan-b|Demo lone check B"
)
RELATIONSHIPS=(
  "demo-booking|demo-booking-api"
  "demo-booking|demo-booking-db"
  "demo-loyalty|demo-loyalty-api"
)
names() { az rest --method get --url "$B/$1?$V" | jq -S '[.value[].name] | sort'; }
case "$1" in
  snapshot)
    names entities > "$HERE/entities.before.json"
    names relationships > "$HERE/relationships.before.json"
    ;;
  add)
    for e in "${ENTITIES[@]}"; do
      az rest --method put --url "$B/entities/${e%%|*}?$V" \
        --body "$(jq -n --arg d "${e#*|}" '{properties: {displayName: $d}}')" -o none
    done
    for r in "${RELATIONSHIPS[@]}"; do
      p="${r%%|*}" c="${r#*|}"
      az rest --method put --url "$B/relationships/$p-$c?$V" \
        --body "$(jq -n --arg p "$p" --arg c "$c" '{properties: {parentEntityName: $p, childEntityName: $c}}')" -o none
    done
    ;;
  remove)
    for r in "${RELATIONSHIPS[@]}"; do
      az rest --method delete --url "$B/relationships/${r%%|*}-${r#*|}?$V" -o none
    done
    for e in "${ENTITIES[@]}"; do
      az rest --method delete --url "$B/entities/${e%%|*}?$V" -o none
    done
    ;;
  verify)
    diff "$HERE/entities.before.json" <(names entities) && diff "$HERE/relationships.before.json" <(names relationships) \
      && echo "entity and relationship names match the snapshot"
    ;;
  *) echo "usage: topology.sh snapshot|add|remove|verify" >&2; exit 2 ;;
esac
