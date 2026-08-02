"""get_tenant_id(tool_context) reads tenant_id from ADK session state — the
same place existing tools already stash cross-call values (see
tool_context.state usage in agents/refund/tools.py). Missing tenant_id is a
hard error, never a default."""
from unittest.mock import MagicMock

import pytest


def test_get_tenant_id_present():
    from customer_support_mas.tenancy.context import get_tenant_id

    ctx = MagicMock()
    ctx.state = {"tenant_id": "acme-electronics"}

    assert get_tenant_id(ctx) == "acme-electronics"


def test_get_tenant_id_missing_raises():
    from customer_support_mas.tenancy.context import MissingTenantError, get_tenant_id

    ctx = MagicMock()
    ctx.state = {}

    with pytest.raises(MissingTenantError):
        get_tenant_id(ctx)
