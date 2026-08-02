"""get_rag_search must cache one RAGProductSearch instance per tenant
database_id — a shared global instance would let one tenant's product
search see another tenant's embeddings.

Note: This test imports the real get_rag_search at module level (before
conftest patches it), so tests run against the actual caching logic in
customer_support_mas/services/rag_search.py, not a duplicate implementation.
"""
from unittest.mock import patch

# Import the real function and cache dict at module level before conftest
# autouse fixtures patch them
from customer_support_mas.services.rag_search import get_rag_search as real_get_rag_search
from customer_support_mas.services import rag_search as rag_module


def test_get_rag_search_caches_per_database_id():
    """Test that get_rag_search caches one RAGProductSearch instance per database_id."""
    created = []

    class FakeRAGProductSearch:
        def __init__(self, database_id, location):
            self.database_id = database_id
            created.append(database_id)

    # Reset the module's real cache before test
    rag_module._rag_search_instances.clear()

    # Patch RAGProductSearch to use our fake, test the real get_rag_search caching
    with patch.object(rag_module, "RAGProductSearch", FakeRAGProductSearch):
        # Call the real get_rag_search (captured at import time, immune to conftest patches)
        r1 = real_get_rag_search("tenant-a-db")
        r2 = real_get_rag_search("tenant-b-db")
        r1_again = real_get_rag_search("tenant-a-db")

        assert r1.database_id == "tenant-a-db"
        assert r2.database_id == "tenant-b-db"
        assert r1_again is r1  # cached, not re-created
        assert created == ["tenant-a-db", "tenant-b-db"]

    # Cleanup
    rag_module._rag_search_instances.clear()
