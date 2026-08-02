"""get_provider(tenant_id): resolves and constructs the right
CommerceProvider for a tenant, per call. No live provider object is ever
stashed in ADK session state (session state must stay serializable) —
only tenant_id is, and this function re-resolves the provider from
tenant_id on every tool call, with the tenant CONFIG cached (Task 2) so
this isn't a Firestore read every time, just a construction."""

from customer_support_mas.providers.base import CommerceProvider
from customer_support_mas.providers.firestore_provider import FirestoreProvider
from customer_support_mas.tenancy.config import load_tenant_config


def get_provider(tenant_id: str) -> CommerceProvider:
    config = load_tenant_config(tenant_id)
    if config.provider_type == "firestore":
        return FirestoreProvider(config.provider_config)
    if config.provider_type == "shopify":
        from customer_support_mas.providers.shopify_provider import ShopifyProvider

        return ShopifyProvider(config.provider_config)
    raise ValueError(f"Unknown provider_type {config.provider_type!r} for tenant {tenant_id!r}")
