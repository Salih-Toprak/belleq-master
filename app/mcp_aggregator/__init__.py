"""Per-container MCP aggregator.

Serves one MCP endpoint per container at ``/mcp/{container_id}``. On connect it
builds a FastMCP proxy that mounts only the connectors whitelisted for that
container (namespaced by connector id), so an AI client sees a merged, filtered
toolset. The whitelist is enforced because non-whitelisted connectors are never
mounted.
"""
