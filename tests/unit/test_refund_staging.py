"""process_refund stages a PENDING_APPROVAL request; it never executes.

Task 8 (HITL refund staging): process_refund now writes to a new
`refund_requests` collection instead of `refunds`, and returns
`status: "pending_approval"` / `"already_pending"` instead of `"success"`.

Note on fixtures: the task-8 plan brief sketched these tests against
fictional fixtures (`refund_tool_context`, `mock_db` as a directly-injectable
fixture available repo-wide). Neither exists as written in this repo:
- `mock_tool_context` (a MagicMock ToolContext for demo-user-001) is defined
  locally in tests/unit/test_tools.py, not in conftest.py, so it is
  redefined here identically (pytest fixtures are not visible across files
  unless declared in a conftest.py). Since Task 6 (multi-tenant), it also
  carries `state["tenant_id"] = "test-tenant"` — every refund tool now
  resolves tenant_id via customer_support_mas.tenancy.context.get_tenant_id
  and fails hard without it (no default/implicit tenant).
- The session-scoped `mock_db` fixture (tests/conftest.py) is a *different*
  MockFirestoreClient instance than the one actually patched in during each
  test (a fresh instance created per-test by the autouse `mock_backends`
  fixture in tests/unit/conftest.py - verified empirically: the two
  fixtures resolve to different object ids). Reading staged Firestore state
  therefore goes through the same path process_refund itself uses -
  `get_provider(tenant_id)._db` (Task 6) - which always reflects whatever
  mock is actually active for the running test.
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_tool_context():
    """Mock ToolContext for demo-user-001, matching test_tools.py's fixture.

    demo-user-001 owns ORD-12345 (In Transit, 2 items) among others.
    process_refund only re-checks ownership (not delivery status) when
    called directly without a prior validate_refund_request/
    check_refund_eligibility session state, so ORD-12345 works fine as a
    standalone target for these staging tests.
    """
    mock_ctx = MagicMock()
    mock_ctx.state = {"tenant_id": "test-tenant"}
    mock_ctx.user_id = "demo-user-001"
    mock_ctx.actions = MagicMock()
    return mock_ctx


def _active_db_client():
    """Return the Firestore mock actually used by refund/tools.py for "test-tenant"."""
    from customer_support_mas.providers.registry import get_provider

    return get_provider("test-tenant")._db


class TestRefundStaging:
    """process_refund stages a pending approval request instead of executing."""

    def test_valid_refund_is_staged_not_executed(self, mock_tool_context):
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund("ORD-12345", "damaged", mock_tool_context)

        assert result["status"] == "pending_approval"
        assert result["request_id"]
        assert result["order_id"] == "ORD-12345"
        assert result["refund_amount"] > 0
        assert "items" in result
        assert "message" in result

        db = _active_db_client()
        staged = db.collection("refund_requests").document(result["request_id"]).get().to_dict()
        assert staged is not None
        assert staged["status"] == "PENDING_APPROVAL"
        assert staged["order_id"] == "ORD-12345"
        assert staged["user_id"] == "demo-user-001"
        assert staged["reason_category"]  # classified before staging
        assert staged["refund_amount"] == result["refund_amount"]
        assert "requested_at" in staged
        assert "expires_at" in staged

        # Nothing executed: the refunds collection must remain untouched.
        assert list(db.collection("refunds").stream()) == []

    def test_unacceptable_reason_still_rejected_before_staging(self, mock_tool_context):
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund("ORD-12345", "changed_mind", mock_tool_context)

        assert result["status"] == "reason_not_acceptable"

        db = _active_db_client()
        assert list(db.collection("refund_requests").stream()) == []
        assert list(db.collection("refunds").stream()) == []

    def test_duplicate_staging_is_idempotent(self, mock_tool_context):
        from customer_support_mas.agents.refund.tools import process_refund

        first = process_refund("ORD-12345", "damaged", mock_tool_context)
        second = process_refund("ORD-12345", "damaged", mock_tool_context)

        assert first["status"] == "pending_approval"
        assert second["status"] == "already_pending"
        assert second["request_id"] == first["request_id"]

        db = _active_db_client()
        assert len(list(db.collection("refund_requests").stream())) == 1

    def test_invalid_order_still_rejected_before_staging(self, mock_tool_context):
        """Ownership/validation checks still run before any staging occurs."""
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund("ORD-99999", "damaged", mock_tool_context)

        assert result["status"] == "error"

        db = _active_db_client()
        assert list(db.collection("refund_requests").stream()) == []


class TestProviderWithoutARefundStore:
    """Finding I3: process_refund reaches through to `provider._db`, which
    only FirestoreProvider has. For a Shopify-backed tenant that used to be a
    bare AttributeError, swallowed by @tool_error_handler into a generic
    "something went wrong" that told nobody the store's whole refund workflow
    was unavailable.

    Note this deliberately does NOT fail open the way policy.py's guard does:
    policy.py can fall back to DEFAULT_POLICY, whereas there is no safe
    default answer to "where do I stage a refund request".
    """

    @pytest.fixture
    def shopify_tool_context(self, mock_db, monkeypatch):
        r"""A Shopify-backed tenant, with a mock order that clears every check
        BEFORE the `provider._db` reach-through — otherwise the tool would
        bail out on ownership first and never reach the code under test.

        (The stub's own `_MOCK_ORDERS` ids don't satisfy validate_order_id's
        `ORD-\d{5,10}` pattern, so an "ORD-"-shaped one is added here rather
        than changing the stub's fixture data.)
        """
        from customer_support_mas.providers import shopify_provider
        from customer_support_mas.tenancy import config as config_module

        monkeypatch.setenv("SHOPIFY_MOCK", "true")
        monkeypatch.setitem(
            shopify_provider._MOCK_ORDERS,
            "ORD-12345",
            {
                "customer_id": "shopify-customer-1",
                "status": "Delivered",
                "items": [{"product_id": "SHOPIFY-PROD-1", "name": "Mock Shopify Product", "price": 29.99, "qty": 1}],
                "total": 29.99,
            },
        )
        config_module.invalidate_tenant_config_cache()
        mock_db.collection("tenants").document("shopify-tenant").set(
            {
                "tenant_id": "shopify-tenant",
                "tier": "light",
                "provider_type": "shopify",
                "provider_config": {"shop_domain": "mock.myshopify.com"},
                "pool_id": "test-pool",
            }
        )
        ctx = MagicMock()
        ctx.state = {"tenant_id": "shopify-tenant"}
        ctx.user_id = "shopify-customer-1"
        ctx.actions = MagicMock()
        yield ctx
        config_module.invalidate_tenant_config_cache()

    def test_staging_reports_unavailable_instead_of_crashing(self, shopify_tool_context):
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund("ORD-12345", "damaged", shopify_tool_context)

        # "unavailable", not the generic "error" that @tool_error_handler
        # would have produced from a swallowed AttributeError.
        assert result["status"] == "unavailable", result
        assert "support" in result["message"].lower()

    def test_no_refund_request_is_written_anywhere(self, shopify_tool_context, mock_db):
        from customer_support_mas.agents.refund.tools import process_refund

        process_refund("ORD-12345", "damaged", shopify_tool_context)

        assert list(mock_db.collection("refund_requests").stream()) == []
        assert list(mock_db.collection("refunds").stream()) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
