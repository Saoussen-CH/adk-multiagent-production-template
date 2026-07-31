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
  unless declared in a conftest.py).
- The session-scoped `mock_db` fixture (tests/conftest.py) is a *different*
  MockFirestoreClient instance than the one actually patched into
  `customer_support_mas.agents.refund.tools.db_client` during each test
  (a fresh instance created per-test by the autouse `mock_backends` fixture
  in tests/unit/conftest.py - verified empirically: the two fixtures resolve
  to different object ids). Reading staged Firestore state therefore goes
  through the tools module's live `db_client` attribute, which always
  reflects whatever mock is actually active for the running test.
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
    mock_ctx.state = {}
    mock_ctx.user_id = "demo-user-001"
    mock_ctx.actions = MagicMock()
    return mock_ctx


def _active_db_client():
    """Return the Firestore mock currently patched into the refund tools module."""
    from customer_support_mas.agents.refund import tools as refund_tools

    return refund_tools.db_client


class TestRefundStaging:
    """process_refund stages a pending approval request instead of executing."""

    def test_valid_refund_is_staged_not_executed(self, mock_tool_context):
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund("ORD-12345", "item arrived damaged", mock_tool_context)

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

        result = process_refund("ORD-12345", "changed my mind", mock_tool_context)

        assert result["status"] == "reason_not_acceptable"

        db = _active_db_client()
        assert list(db.collection("refund_requests").stream()) == []
        assert list(db.collection("refunds").stream()) == []

    def test_duplicate_staging_is_idempotent(self, mock_tool_context):
        from customer_support_mas.agents.refund.tools import process_refund

        first = process_refund("ORD-12345", "item arrived damaged", mock_tool_context)
        second = process_refund("ORD-12345", "item arrived damaged", mock_tool_context)

        assert first["status"] == "pending_approval"
        assert second["status"] == "already_pending"
        assert second["request_id"] == first["request_id"]

        db = _active_db_client()
        assert len(list(db.collection("refund_requests").stream())) == 1

    def test_invalid_order_still_rejected_before_staging(self, mock_tool_context):
        """Ownership/validation checks still run before any staging occurs."""
        from customer_support_mas.agents.refund.tools import process_refund

        result = process_refund("ORD-99999", "item arrived damaged", mock_tool_context)

        assert result["status"] == "error"

        db = _active_db_client()
        assert list(db.collection("refund_requests").stream()) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
