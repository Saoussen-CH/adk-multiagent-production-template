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


class TenantConfigConflictError(Exception):
    """Two tenants in the same isolation scope resolve to the same physical
    datastore.

    Light-tier isolation is *physical*: each tenant gets its own named
    Firestore database inside a shared pool project (spec section 6) — the
    query-level `tenant_id` filter is defence in depth, not the primary
    guard. Nothing enforced that, and the shipped fixture points
    "acme-electronics" at `customer-support-db`, the shared default; a second
    tenant onboarded by copying that fixture would have collapsed isolation
    to zero, silently, with both tenants reading each other's orders.
    """


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    tier: Literal["light", "heavy"]
    provider_type: Literal["firestore", "shopify"]
    provider_config: dict
    pool_id: Optional[str] = None
    project_id: Optional[str] = None
    refund_policy_ref: Optional[str] = None
    # Which Firestore database holds this tenant's *backend account* data
    # (users / sessions / tokens / messages). Normally unset: a
    # firestore-backed tenant keeps its accounts in the same database as its
    # commerce data, so `account_database()` below derives it from
    # provider_config. It only has to be set explicitly for a tenant whose
    # commerce provider is not Firestore (a Shopify-backed tenant has no
    # Firestore database of its own, but accounts and chat sessions are this
    # product's data, not the merchant's, and still need somewhere to live).
    account_database_id: Optional[str] = None


_tenant_config_cache: dict[str, TenantConfig] = {}

# Reverse index over the cache: isolation scope -> tenant_id that owns it.
# Keyed by (scope, datastore) rather than datastore alone because the same
# database name in two different pool projects is perfectly fine — it is
# sharing *within* a pool that destroys isolation.
_datastore_owner: dict[tuple[str, str], str] = {}


def _scope(config: TenantConfig) -> str:
    # project_id for a (future) heavy-tier tenant, pool_id for light tier.
    return config.project_id or config.pool_id or "<unscoped>"


def isolation_key(config: TenantConfig) -> Optional[tuple[str, str]]:
    """The (scope, datastore) pair a tenant's *commerce* data physically
    lives in, or None for a provider whose store isn't ours to keep unique (a
    Shopify-backed tenant's data lives in that merchant's own Shopify shop,
    which is inherently theirs alone)."""
    if config.provider_type != "firestore":
        return None
    database_id = config.provider_config.get("database_id")
    if not database_id:
        return None
    return (_scope(config), database_id)


def account_database(config: TenantConfig) -> Optional[str]:
    """The Firestore database holding this tenant's backend accounts —
    `users`, `sessions`, `tokens` and each session's `messages`.

    A customer of Merchant A and a customer of Merchant B who sign up with
    the same email address are different accounts under different merchants,
    so the account layer needs the same physical per-tenant separation the
    commerce layer already has (docs/ARCHITECTURE.md, "Multi-tenancy"). That
    falls out of putting the accounts in the tenant's *own* database rather
    than a shared one, which is what this returns.

    Returns None when the tenant config names no such database — an explicit
    misconfiguration for the caller to reject, never a silent fallback to a
    shared default.
    """
    if config.account_database_id:
        return config.account_database_id
    if config.provider_type == "firestore":
        return config.provider_config.get("database_id")
    return None


def account_isolation_key(config: TenantConfig) -> Optional[tuple[str, str]]:
    """`isolation_key` for the account store. Identical to it for a
    firestore-backed tenant (same database); distinct only when
    `account_database_id` is set explicitly."""
    database_id = account_database(config)
    if not database_id:
        return None
    return (_scope(config), database_id)


def isolation_keys(config: TenantConfig) -> list[tuple[str, str]]:
    """Every (scope, datastore) pair this tenant claims exclusive use of.

    Both the commerce store and the account store are covered: two tenants
    pointing their account stores at one database would collide their user
    accounts exactly as two tenants sharing a commerce database collide their
    orders.
    """
    keys: list[tuple[str, str]] = []
    for key in (isolation_key(config), account_isolation_key(config)):
        if key is not None and key not in keys:
            keys.append(key)
    return keys


def assert_unique_datastores(configs) -> None:
    """Raise TenantConfigConflictError if any two of `configs` share one
    physical datastore within the same pool/project.

    Exposed separately from load_tenant_config so an onboarding or seeding
    step can validate the whole `tenants` collection up front, rather than
    only discovering the clash when the second tenant's first request
    arrives.
    """
    owners: dict[tuple[str, str], str] = {}
    for config in configs:
        for key in isolation_keys(config):
            existing = owners.get(key)
            if existing is not None and existing != config.tenant_id:
                raise TenantConfigConflictError(
                    f"Tenants {existing!r} and {config.tenant_id!r} both resolve to database "
                    f"{key[1]!r} in scope {key[0]!r} — light-tier isolation is per-database, "
                    "so two tenants sharing one database have no isolation at all"
                )
            owners[key] = config.tenant_id


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
        account_database_id=data.get("account_database_id"),
    )
    # Uniqueness is checked against every tenant resolved so far in this
    # process. That is necessarily partial (it can't see a tenant nobody has
    # requested yet), which is why assert_unique_datastores above exists for
    # whole-collection validation — but it is the check that fires on the
    # request that would actually have caused the leak, which is the one
    # that matters at runtime.
    #
    # Note the flip side: because the index has no TTL, retiring a tenant and
    # re-using its database_id for a different tenant within the life of one
    # process is (correctly) refused until invalidate_tenant_config_cache()
    # is called. Tenant offboarding is an operator action, not a hot path.
    keys = isolation_keys(config)
    for key in keys:
        owner = _datastore_owner.get(key)
        if owner is not None and owner != tenant_id:
            logger.error(
                "Tenant config conflict: %s and %s both map to database %s in scope %s",
                owner,
                tenant_id,
                key[1],
                key[0],
            )
            raise TenantConfigConflictError(
                f"Tenants {owner!r} and {tenant_id!r} both resolve to database {key[1]!r} "
                f"in scope {key[0]!r} — refusing to serve a configuration with no isolation"
            )
    for key in keys:
        _datastore_owner[key] = tenant_id

    _tenant_config_cache[tenant_id] = config
    return config


def invalidate_tenant_config_cache(tenant_id: Optional[str] = None) -> None:
    """Clear one tenant's cached config, or all of them if tenant_id is None.

    The datastore-ownership index is invalidated in lockstep — leaving a stale
    entry behind would make a legitimate re-point of a tenant to a new
    database look like a conflict.
    """
    if tenant_id is None:
        _tenant_config_cache.clear()
        _datastore_owner.clear()
    else:
        config = _tenant_config_cache.pop(tenant_id, None)
        if config is not None:
            for key in isolation_keys(config):
                if _datastore_owner.get(key) == tenant_id:
                    del _datastore_owner[key]
