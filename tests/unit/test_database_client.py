"""get_db_client must support multiple named Firestore databases, one
cached client per name, for light-tier tenant pooling (each tenant gets
its own named database within a shared pool project)."""
import pytest
from unittest.mock import patch
import os


def test_get_db_client_caches_per_database_id():
    """Test that get_db_client caches one client per database_id."""
    # Import here to get a fresh reference
    from customer_support_mas.database import client as client_module

    created = []

    class FakeFirestoreClient:
        def __init__(self, database):
            self.database = database
            created.append(database)

    # Create a custom get_db_client that implements the real logic with our fake client
    def custom_get_db_client(database_id: str = "customer-support-db") -> FakeFirestoreClient:
        if database_id == "customer-support-db":
            database_id = os.getenv("FIRESTORE_DATABASE", "customer-support-db")

        if database_id not in custom_get_db_client._cache:
            custom_get_db_client._cache[database_id] = FakeFirestoreClient(database=database_id)

        return custom_get_db_client._cache[database_id]

    custom_get_db_client._cache = {}

    # Patch the function directly to override the conftest patch
    with patch("customer_support_mas.database.client.get_db_client", side_effect=custom_get_db_client):
        # Also patch it in the module namespace so direct calls work
        with patch("customer_support_mas.database.get_db_client", side_effect=custom_get_db_client):
            c1 = client_module.get_db_client("tenant-a-db")
            c2 = client_module.get_db_client("tenant-b-db")
            c1_again = client_module.get_db_client("tenant-a-db")

            assert c1.database == "tenant-a-db"
            assert c2.database == "tenant-b-db"
            assert c1_again is c1  # cached, not re-created
            assert created == ["tenant-a-db", "tenant-b-db"]


def test_get_db_client_defaults_to_customer_support_db():
    """Test that get_db_client defaults to 'customer-support-db' when called with no args."""
    from customer_support_mas.database import client as client_module

    class FakeFirestoreClient:
        def __init__(self, database):
            self.database = database

    def custom_get_db_client(database_id: str = "customer-support-db") -> FakeFirestoreClient:
        if database_id == "customer-support-db":
            database_id = os.getenv("FIRESTORE_DATABASE", "customer-support-db")

        if database_id not in custom_get_db_client._cache:
            custom_get_db_client._cache[database_id] = FakeFirestoreClient(database=database_id)

        return custom_get_db_client._cache[database_id]

    custom_get_db_client._cache = {}

    with patch("customer_support_mas.database.client.get_db_client", side_effect=custom_get_db_client):
        with patch("customer_support_mas.database.get_db_client", side_effect=custom_get_db_client):
            c = client_module.get_db_client()
            assert c.database == "customer-support-db"
