"""Tenant configuration: loaded from the `tenants` Firestore collection.

No default tenant exists — an unrecognized tenant_id is a hard
TenantNotFoundError, never a silent fallback (Global Constraints, plan
docs/superpowers/plans/2026-08-02-multi-tenant-provider-architecture.md).
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional

from customer_support_mas.database import get_db_client

logger = logging.getLogger(__name__)

TENANTS_COLLECTION = "tenants"


class TenantNotFoundError(Exception):
    """No tenant config document exists for the given tenant_id."""


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    tier: Literal["light", "heavy"]
    provider_type: Literal["firestore", "shopify"]
    provider_config: dict
    pool_id: Optional[str] = None
    project_id: Optional[str] = None
    refund_policy_ref: Optional[str] = None


_tenant_config_cache: dict[str, TenantConfig] = {}


def load_tenant_config(tenant_id: str) -> TenantConfig:
    """Load a tenant's config, cached in-process after the first read.

    Args:
        tenant_id: The tenant to load config for.

    Raises:
        TenantNotFoundError: No `tenants/{tenant_id}` document exists.
    """
    if tenant_id in _tenant_config_cache:
        return _tenant_config_cache[tenant_id]

    doc = get_db_client().collection(TENANTS_COLLECTION).document(tenant_id).get()
    if not doc.exists:
        logger.warning("Unknown tenant_id requested: %s", tenant_id)
        raise TenantNotFoundError(f"No tenant config found for tenant_id={tenant_id!r}")

    data = doc.to_dict()
    config = TenantConfig(
        tenant_id=data["tenant_id"],
        tier=data["tier"],
        provider_type=data["provider_type"],
        provider_config=data.get("provider_config", {}),
        pool_id=data.get("pool_id"),
        project_id=data.get("project_id"),
        refund_policy_ref=data.get("refund_policy_ref"),
    )
    _tenant_config_cache[tenant_id] = config
    return config


def invalidate_tenant_config_cache(tenant_id: Optional[str] = None) -> None:
    """Clear one tenant's cached config, or all of them if tenant_id is None."""
    if tenant_id is None:
        _tenant_config_cache.clear()
    else:
        _tenant_config_cache.pop(tenant_id, None)
