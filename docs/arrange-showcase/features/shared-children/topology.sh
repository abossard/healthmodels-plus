#!/usr/bin/env bash
# Temporary demo relationships on anbotrai-stability, one variant at a time.
#   a: the assistant also watches the itinerary MCP server (auto keeps layered)
#   b: the LangChain assistant and the destinations MCP server both use AI generation (auto keeps tree)
#   topology.sh add|remove a|b
#   topology.sh names <file>     sorted entity and relationship names, to compare before and after
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../../.env"
B=/subscriptions/$SUBSCRIPTION/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.CloudHealth/healthmodels/anbotrai-stability
V=api-version=2026-11-01

edges() {
  case "${1:?variant a|b}" in
    a) echo "demo-assistant-itinerary assistant itinerary-resource" ;;
    b) echo "demo-langchain-generation assistant-langchain-resource generation"
       echo "demo-destinations-mcp-generation destinations-mcp-resource generation-resource" ;;
    *) echo "unknown variant $1" >&2; exit 2 ;;
  esac
}

case "${1:?add|remove|names}" in
  add)
    edges "${2:-}" | while read -r name parent child; do
      az rest --method put --url "https://management.azure.com$B/relationships/$name?$V" \
        --body "$(jq -nc --arg p "$parent" --arg c "$child" '{properties: {parentEntityName: $p, childEntityName: $c}}')" \
        | jq -c '{name, parent: .properties.parentEntityName, child: .properties.childEntityName}'
    done
    ;;
  remove)
    edges "${2:-}" | while read -r name _; do
      az rest --method delete --url "https://management.azure.com$B/relationships/$name?$V"
      echo "removed $name"
    done
    ;;
  names)
    {
      az rest --method get --url "https://management.azure.com$B/entities?$V" | jq -c '{entities: [.value[].name] | sort}'
      az rest --method get --url "https://management.azure.com$B/relationships?$V" | jq -c '{relationships: [.value[].name] | sort}'
    } | jq -s 'add' > "${2:?file}"
    ;;
esac
