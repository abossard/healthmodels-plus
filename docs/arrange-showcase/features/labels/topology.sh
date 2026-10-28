#!/usr/bin/env bash
# Temporary labelled demo-* relationships on anbotrai-stability: topology.sh snapshot|add|remove|verify
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../../.env"
M="https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CloudHealth/healthmodels/anbotrai-stability"
V="api-version=2026-11-01"
names() {  # entities as names, relationships as name, parent, child, label
  az rest --method get --url "$M/entities?$V" | jq '[.value[].name] | sort'
  az rest --method get --url "$M/relationships?$V" \
    | jq '[.value[] | {name, p: .properties.parentEntityName, c: .properties.childEntityName, label: .properties.displayName}] | sort_by(.name)'
}
rel() {  # rel <name> <parent> <child> <label>
  az rest --method put --url "$M/relationships/$1?$V" \
    --body "$(jq -n --arg p "$2" --arg c "$3" --arg l "$4" '{properties: {parentEntityName: $p, childEntityName: $c, displayName: $l}}')" \
    | jq -c '{name, p: .properties.parentEntityName, c: .properties.childEntityName, label: .properties.displayName}'
}
case "$1" in
  snapshot) names | jq -s '.[0]' > "$HERE/entities.before.json"; names | jq -s '.[1]' > "$HERE/relationships.before.json" ;;
  add)
    rel demo-generation-interface generation interface-resource 'sends drafts to'
    rel demo-interface-generation interface generation-resource 'renders results of'
    ;;
  remove)
    for r in demo-generation-interface demo-interface-generation; do az rest --method delete --url "$M/relationships/$r?$V"; done
    ;;
  verify)
    diff <(names | jq -s '.[0]') "$HERE/entities.before.json" && diff <(names | jq -s '.[1]') "$HERE/relationships.before.json" \
      && echo "entities and relationships match the snapshot"
    ;;
esac
