"""Both FirestoreProvider and ShopifyProvider must satisfy CommerceProvider
identically from a calling tool's perspective — this is what makes the
tier/backend choice a deployment decision, not a code-path decision (spec
section 9, migration path)."""

import inspect

import pytest

from customer_support_mas.providers.base import CommerceProvider
from customer_support_mas.providers.firestore_provider import FirestoreProvider
from customer_support_mas.providers.shopify_provider import ShopifyProvider

PROVIDER_FACTORIES = [
    ("firestore", lambda: FirestoreProvider({"database_id": "contract-test-db"})),
    ("shopify", lambda: ShopifyProvider({"shop_domain": "contract-test.myshopify.com"})),
]


def _protocol_method_names() -> list[str]:
    """Every callable CommerceProvider declares, read off base.py itself
    rather than hardcoded in this test — so this test can't silently drift
    out of sync when a method is added to (or removed from) the protocol.
    (typing.Protocol's __protocol_attrs__/get_protocol_members() helpers
    require Python 3.12+; this repo runs 3.11, so introspect vars()
    directly instead.)"""
    names = sorted(
        name for name, value in vars(CommerceProvider).items() if not name.startswith("_") and callable(value)
    )
    # Sanity check: if this ever comes back empty/tiny, the introspection
    # itself is broken (e.g. CommerceProvider stopped being a plain
    # typing.Protocol) and every assertion below would vacuously pass.
    assert len(names) >= 14, f"expected >=14 CommerceProvider methods, found {len(names)}: {names}"
    return names


@pytest.mark.parametrize("name,factory", PROVIDER_FACTORIES, ids=[p[0] for p in PROVIDER_FACTORIES])
def test_provider_implements_required_methods(name, factory):
    provider = factory()
    for method_name in _protocol_method_names():
        assert hasattr(provider, method_name), f"{name} provider missing {method_name}"
        assert callable(getattr(provider, method_name)), f"{name} provider's {method_name} is not callable"


@pytest.mark.parametrize("name,factory", PROVIDER_FACTORIES, ids=[p[0] for p in PROVIDER_FACTORIES])
def test_provider_methods_require_tenant_id_first(name, factory):
    """Global constraint (no implicit tenant, see base.py's module
    docstring): every CommerceProvider method takes tenant_id as its first
    parameter. Checked per-provider (not just on the Protocol) so a new
    implementation can't quietly drop it while still satisfying hasattr()."""
    provider = factory()
    for method_name in _protocol_method_names():
        sig = inspect.signature(getattr(provider, method_name))
        params = list(sig.parameters)
        assert params, f"{name} provider's {method_name} takes no arguments"
        assert params[0] == "tenant_id", f"{name} provider's {method_name} first param is {params[0]!r}, not tenant_id"
