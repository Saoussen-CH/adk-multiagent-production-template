"""
Database Client Configuration
===============================
Sets up Firestore database clients for the customer support system.

Multi-database: light-tier tenants each get their own named Firestore
database within a shared pool project (see docs/superpowers/specs/
2026-08-02-multi-tenant-provider-architecture-design.md section 6) — this
module caches one client per database name, not a single global client.

Uses lazy initialization to ensure environment variables are loaded
before creating any Firestore client. This is critical for Agent Engine
deployments where env vars may not be available at module import time.
"""

import logging
import os

from dotenv import load_dotenv
from google.cloud import firestore

logger = logging.getLogger(__name__)

load_dotenv()

DEFAULT_DATABASE_ID = "customer-support-db"

_db_clients: dict[str, firestore.Client] = {}


def get_db_client(database_id: str = DEFAULT_DATABASE_ID) -> firestore.Client:
    """Get or create a Firestore client for the given database (lazy, cached per name).

    Args:
        database_id: Firestore database name. Defaults to the env var
            FIRESTORE_DATABASE if set, else "customer-support-db" — this
            preserves today's single-database behavior when called with
            no arguments.
    """
    if database_id == DEFAULT_DATABASE_ID:
        database_id = os.getenv("FIRESTORE_DATABASE", DEFAULT_DATABASE_ID)

    if database_id not in _db_clients:
        _db_clients[database_id] = firestore.Client(database=database_id)
        logger.debug("Initialized Firestore client for database: %s", database_id)

    return _db_clients[database_id]


class _LazyDbClient:
    """Proxy for the default database's client (backward compatibility with
    existing `from customer_support_mas.database import db_client` call sites
    that predate multi-tenancy — these are being migrated to
    `get_provider(tenant_id)` over the course of this plan, but the proxy
    stays until Task 6 removes the last direct `db_client` usage)."""

    def __getattr__(self, name):
        return getattr(get_db_client(), name)


db_client = _LazyDbClient()
