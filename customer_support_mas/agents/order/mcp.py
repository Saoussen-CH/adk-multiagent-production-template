"""Env-gated FedEx MCP toolset for the order agent.

When MCP_FEDEX_URL is unset the order agent is byte-for-byte its v1 self —
no MCP dependency is exercised at runtime. When set (deployed engines get it
via ENV_VARS in deployment/deploy.py), the agent gains the read-only
track_shipment tool served by the fedex-tracking-mcp Cloud Run service,
with egress governed per docs/MCP_FEDEX.md.

Cloud Run auth: deploy-mcp-fedex.sh deploys with --no-allow-unauthenticated,
so plain unauthenticated requests get a 403 from Cloud Run's IAM layer before
they ever reach the MCP server — confirmed live (curl without a token: 403;
with a fetched ID token: reaches the server). McpToolset's connection_params
carry no auth by default, so a header_provider is required to attach a fresh
ID token (audience = the Cloud Run service's own base URL) on every request.

Two token-minting paths, selected by whether FEDEX_MCP_INVOKER_SA_EMAIL is
set:

- **Impersonation (preferred, requires the env var):** the agent impersonates
  a dedicated invoker SA (terraform/modules/core/iam.tf's
  fedex_mcp_invoker) to mint the ID token, rather than using its own
  Agent-Identity-flavored ADC directly. This is required, not optional, once
  wired up: confirmed live that a token minted straight from an
  AGENT_IDENTITY engine's ADC gets a 401 "could not be verified" from Cloud
  Run's IAM invoker check — Agent Identity's SPIFFE/mTLS trust domain and
  Cloud Run's OIDC invoker verification are separate paths that don't
  interoperate directly. The invoker SA is a normal IAM principal Cloud Run
  already understands; the agent's Agent Identity only ever holds
  roles/iam.serviceAccountTokenCreator on it (never run.invoker directly).
  See docs/MCP_FEDEX.md section 7 for the full design.
- **Direct ADC (fallback):** used when FEDEX_MCP_INVOKER_SA_EMAIL is unset,
  e.g. an env whose Terraform hasn't been re-applied with the invoker SA yet.
  Known to hit the 401 above on an AGENT_IDENTITY engine; kept only so
  existing envs don't hard-fail before they opt in.

Independent of Agent Identity/CAA either way — the CAA opt-out
(GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES) governs the
platform's own internal API calls, not this Cloud Run invoker path.
"""
import asyncio
import os
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams


def _impersonated_id_token_sync(audience: str, invoker_sa_email: str) -> str:
    import google.auth
    import google.auth.impersonated_credentials
    import google.auth.transport.requests

    source_credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    target_credentials = google.auth.impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=invoker_sa_email,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    id_token_credentials = google.auth.impersonated_credentials.IDTokenCredentials(
        target_credentials=target_credentials,
        target_audience=audience,
        include_email=True,
    )
    id_token_credentials.refresh(google.auth.transport.requests.Request())
    return id_token_credentials.token


def _direct_adc_id_token_sync(audience: str) -> str:
    import google.auth.transport.requests
    import google.oauth2.id_token

    auth_req = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(auth_req, audience)


def _fedex_id_token_headers_sync(audience: str) -> dict[str, str]:
    invoker_sa_email = os.environ.get("FEDEX_MCP_INVOKER_SA_EMAIL")
    if invoker_sa_email:
        token = _impersonated_id_token_sync(audience, invoker_sa_email)
    else:
        token = _direct_adc_id_token_sync(audience)
    return {"Authorization": f"Bearer {token}"}


async def _fedex_header_provider(_readonly_context) -> dict[str, str]:
    url = os.environ.get("MCP_FEDEX_URL", "")
    parts = urlsplit(url)
    audience = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return await asyncio.to_thread(_fedex_id_token_headers_sync, audience)


def build_fedex_toolset() -> Optional[McpToolset]:
    url = os.environ.get("MCP_FEDEX_URL")
    if not url:
        return None
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=url),
        tool_filter=["track_shipment"],
        header_provider=_fedex_header_provider,
    )
