"""Anonymous user creation must mint a real, verifiable token — not just a
bare id — through the same create_token/verify_token mechanism registered
users already get after login."""
import os

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

import pytest  # noqa: E402

from backend.app.database import Database  # noqa: E402


@pytest.fixture
def db(mock_db):
    return Database(project_id="test-project", database_id="test-tenant-db", tenant_id="test-tenant", client=mock_db)


def test_create_anonymous_user_returns_a_verifiable_token(db):
    user_id, token = db.create_anonymous_user()

    assert user_id.startswith("anon-")
    assert token  # non-empty
    verified_user_id = db.verify_token(token)
    assert verified_user_id == user_id


def test_two_anonymous_users_get_different_tokens(db):
    _, token_a = db.create_anonymous_user()
    _, token_b = db.create_anonymous_user()

    assert token_a != token_b
