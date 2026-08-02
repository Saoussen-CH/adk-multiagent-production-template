"""get_db_client must support multiple named Firestore databases, one
cached client per name, for light-tier tenant pooling (each tenant gets
its own named database within a shared pool project).

Note: This test imports the real get_db_client at module level (before
conftest patches it), so tests run against the actual caching logic in
customer_support_mas/database/client.py, not a duplicate implementation.
"""
from unittest.mock import patch

# Import the real function and cache dict at module level before conftest
# autouse fixtures patch them
from customer_support_mas.database.client import get_db_client as real_get_db_client
from customer_support_mas.database import client as client_module


def test_get_db_client_caches_per_database_id():
    """Test that get_db_client caches one client per database_id."""
    created = []

    class FakeFirestoreClient:
        def __init__(self, database):
            self.database = database
            created.append(database)

    # Reset the module's real cache before test
    client_module._db_clients.clear()

    # Patch firestore.Client to use our fake, test the real get_db_client caching
    with patch.object(client_module.firestore, "Client", FakeFirestoreClient):
        # Call the real get_db_client (captured at import time, immune to conftest patches)
        c1 = real_get_db_client("tenant-a-db")
        c2 = real_get_db_client("tenant-b-db")
        c1_again = real_get_db_client("tenant-a-db")

        assert c1.database == "tenant-a-db"
        assert c2.database == "tenant-b-db"
        assert c1_again is c1  # cached, not re-created
        assert created == ["tenant-a-db", "tenant-b-db"]

    # Cleanup
    client_module._db_clients.clear()


def test_get_db_client_defaults_to_customer_support_db():
    """Test that get_db_client defaults to 'customer-support-db' when called with no args."""
    class FakeFirestoreClient:
        def __init__(self, database):
            self.database = database

    # Reset the module's real cache before test
    client_module._db_clients.clear()

    with patch.object(client_module.firestore, "Client", FakeFirestoreClient):
        # Call the real get_db_client
        c = real_get_db_client()
        assert c.database == "customer-support-db"

    # Cleanup
    client_module._db_clients.clear()
