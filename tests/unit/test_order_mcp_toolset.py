"""The FedEx toolset must be strictly env-gated: absent by default."""
import importlib

import pytest


def test_no_toolset_when_env_unset(monkeypatch):
    monkeypatch.delenv("MCP_FEDEX_URL", raising=False)
    from customer_support_mas.agents.order import mcp as order_mcp
    importlib.reload(order_mcp)
    assert order_mcp.build_fedex_toolset() is None


def test_toolset_built_when_env_set(monkeypatch):
    monkeypatch.setenv("MCP_FEDEX_URL", "https://fedex-mcp.example.run.app/mcp")
    from customer_support_mas.agents.order import mcp as order_mcp
    importlib.reload(order_mcp)
    toolset = order_mcp.build_fedex_toolset()
    assert toolset is not None
    # Only the read-only tracking tool is exposed to the agent.
    assert toolset.tool_filter == ["track_shipment"]


def test_order_agent_tools_unchanged_by_default(monkeypatch):
    monkeypatch.delenv("MCP_FEDEX_URL", raising=False)
    from customer_support_mas.agents.order import agent as order_agent_module
    importlib.reload(order_agent_module)
    tool_names = [
        getattr(t, "name", getattr(getattr(t, "func", None), "__name__", type(t).__name__))
        for t in order_agent_module.order_agent.tools
    ]
    assert not any("Mcp" in type(t).__name__ for t in order_agent_module.order_agent.tools), tool_names
    # The system prompt must not advertise a tool that isn't actually
    # attached — otherwise the model can narrate fabricated live-tracking
    # info it has no function-declaration backing for.
    assert "track_shipment" not in order_agent_module.order_agent.instruction


def test_order_agent_instruction_mentions_track_shipment_when_set(monkeypatch):
    monkeypatch.setenv("MCP_FEDEX_URL", "https://fedex-mcp.example.run.app/mcp")
    from customer_support_mas.agents.order import agent as order_agent_module
    importlib.reload(order_agent_module)
    assert "track_shipment" in order_agent_module.order_agent.instruction
