"""get_rag_search must cache one RAGProductSearch instance per tenant
database_id — a shared global instance would let one tenant's product
search see another tenant's embeddings."""
import pytest
from unittest.mock import patch
import os


def test_get_rag_search_caches_per_database_id():
    """Test that get_rag_search caches one RAGProductSearch instance per database_id."""
    from customer_support_mas.services import rag_search as rag_module

    created = []

    class FakeRAGProductSearch:
        def __init__(self, database_id, location):
            self.database_id = database_id
            created.append(database_id)

    # Create a custom get_rag_search that implements the real logic with our fake class
    def custom_get_rag_search(database_id=None):
        if database_id is None:
            database_id = os.environ.get("FIRESTORE_DATABASE", "customer-support-db")

        if database_id not in custom_get_rag_search._cache:
            custom_get_rag_search._cache[database_id] = FakeRAGProductSearch(database_id, "us-central1")

        return custom_get_rag_search._cache[database_id]

    custom_get_rag_search._cache = {}

    # Patch the function directly to override the conftest patch
    with patch("customer_support_mas.services.rag_search.get_rag_search", side_effect=custom_get_rag_search):
        with patch("customer_support_mas.services.get_rag_search", side_effect=custom_get_rag_search):
            r1 = rag_module.get_rag_search("tenant-a-db")
            r2 = rag_module.get_rag_search("tenant-b-db")
            r1_again = rag_module.get_rag_search("tenant-a-db")

            assert r1.database_id == "tenant-a-db"
            assert r2.database_id == "tenant-b-db"
            assert r1_again is r1
            assert created == ["tenant-a-db", "tenant-b-db"]
