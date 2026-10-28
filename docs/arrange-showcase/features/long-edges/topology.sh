#!/usr/bin/env bash
# Temporary topology of the long-edges showcase on anbotrai-stability.
#
#   topology.sh snapshot <file>   entity and relationship names, sorted
#   topology.sh add               the model root -> preferences, skipping the plan-trip row
#   topology.sh remove            delete it again
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../../.env"
B=/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CloudHealth/healthmodels/anbotrai-stability
V=api-version=2026-11-01
REL=demo-root-preferences

case "${1:?snapshot|add|remove}" in
  snapshot)
    { az rest --method get --url "$B/entities?$V" | jq -r '.value[].name | "entity \(.)"'
      az rest --method get --url "$B/relationships?$V" | jq -r '.value[].name | "relationship \(.)"'
    } | sort > "${2:?file}"
    ;;
  add)
    az rest --method put --url "$B/relationships/$REL?$V" \
      --body '{"properties":{"parentEntityName":"anbotrai-stability","childEntityName":"preferences"}}' \
      | jq -c '{name, parent: .properties.parentEntityName, child: .properties.childEntityName}'
    ;;
  remove)
    az rest --method delete --url "$B/relationships/$REL?$V"
    ;;
esac
