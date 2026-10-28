#!/usr/bin/env bash
# Temporary relationships on anbotrai-stability that make services share resources.
#
#   topology.sh snapshot <file>   entity and relationship names, sorted, as JSON
#   topology.sh add               create the demo-* relationships
#   topology.sh remove            delete them again
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../../.env"
MODEL="https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CloudHealth/healthmodels/anbotrai-stability"
API=api-version=2026-11-01

# name parent child: the assistant calls the itinerary and preferences MCP servers, the itinerary
# MCP server calls the destinations MCP server, destinations and generation use each other's AI Services
LINKS=(
  "demo-assistant-itinerary-mcp assistant itinerary-resource"
  "demo-assistant-preferences-mcp assistant preferences-resource"
  "demo-itinerary-destinations-mcp itinerary destinations-mcp-resource"
  "demo-destinations-generation-ai destinations generation-resource"
  "demo-generation-destinations-ai generation destinations-ai-resource"
)

names() { az rest --method get --url "$MODEL/$1?$API" | jq '[.value[].name] | sort'; }

case "${1:?snapshot|add|remove}" in
  snapshot)
    jq -n --argjson e "$(names entities)" --argjson r "$(names relationships)" '{entities: $e, relationships: $r}' > "${2:?file}"
    ;;
  add)
    for link in "${LINKS[@]}"; do
      read -r name parent child <<< "$link"
      az rest --method put --url "$MODEL/relationships/$name?$API" \
        --body "$(jq -n --arg p "$parent" --arg c "$child" '{properties: {parentEntityName: $p, childEntityName: $c}}')" \
        | jq -r '"\(.name): \(.properties.parentEntityName) -> \(.properties.childEntityName)"'
    done
    ;;
  remove)
    for link in "${LINKS[@]}"; do
      read -r name _ <<< "$link"
      az rest --method delete --url "$MODEL/relationships/$name?$API" && echo "deleted $name"
    done
    ;;
  *) echo "unknown command $1" >&2; exit 2 ;;
esac
