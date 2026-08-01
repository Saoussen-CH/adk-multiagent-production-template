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


def test_header_provider_uses_impersonation_when_invoker_sa_set(monkeypatch):
    """Cloud Run 401s on a token minted from an AGENT_IDENTITY engine's own
    ADC (see docs/MCP_FEDEX.md section 7) — when an invoker SA is configured,
    the impersonation path must be used, never the direct-ADC fallback."""
    monkeypatch.setenv("FEDEX_MCP_INVOKER_SA_EMAIL", "invoker@project.iam.gserviceaccount.com")
    from customer_support_mas.agents.order import mcp as order_mcp

    calls = {}

    def fake_impersonated(audience, invoker_sa_email):
        calls["audience"] = audience
        calls["invoker_sa_email"] = invoker_sa_email
        return "impersonated-token"

    def fake_direct(audience):
        raise AssertionError("direct ADC path must not be used when an invoker SA is configured")

    monkeypatch.setattr(order_mcp, "_impersonated_id_token_sync", fake_impersonated)
    monkeypatch.setattr(order_mcp, "_direct_adc_id_token_sync", fake_direct)

    headers = order_mcp._fedex_id_token_headers_sync("https://fedex-mcp.example.run.app")

    assert headers == {"Authorization": "Bearer impersonated-token"}
    assert calls == {
        "audience": "https://fedex-mcp.example.run.app",
        "invoker_sa_email": "invoker@project.iam.gserviceaccount.com",
    }


def test_header_provider_falls_back_to_direct_adc_when_invoker_sa_unset(monkeypatch):
    """Envs whose Terraform hasn't been re-applied with the invoker SA yet
    must keep working via the pre-existing direct-ADC path."""
    monkeypatch.delenv("FEDEX_MCP_INVOKER_SA_EMAIL", raising=False)
    from customer_support_mas.agents.order import mcp as order_mcp

    def fake_impersonated(audience, invoker_sa_email):
        raise AssertionError("impersonation path must not be used when no invoker SA is configured")

    def fake_direct(audience):
        return "direct-token"

    monkeypatch.setattr(order_mcp, "_impersonated_id_token_sync", fake_impersonated)
    monkeypatch.setattr(order_mcp, "_direct_adc_id_token_sync", fake_direct)

    headers = order_mcp._fedex_id_token_headers_sync("https://fedex-mcp.example.run.app")

    assert headers == {"Authorization": "Bearer direct-token"}
