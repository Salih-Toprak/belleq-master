"""Upstream MCP connector registry and per-container access control.

A *connector* is an upstream MCP server the user has registered once
(Notion MCP, GitHub MCP, etc.). Each Belleq container (context profile)
gets a whitelist of connector ids it is allowed to see. The aggregator
exposes one merged, filtered MCP endpoint per container.
"""
