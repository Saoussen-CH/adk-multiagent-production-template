"""
Database layer for user management and session tracking.

Uses Firestore for storing:
- Users: User accounts and profiles
- Sessions: Conversation threads for each user
- Session state is managed by Agent Engine, but we track metadata here

Tenancy
-------
This layer is **tenant-scoped exactly the way the commerce layer is**: each
tenant's `users`, `sessions`, `tokens` and per-session `messages` live in that
tenant's own physical Firestore database — the same one holding its orders,
products and invoices — resolved through the existing
`load_tenant_config(tenant_id)` / `get_db_client(database_id)` machinery. See
`customer_support_mas.tenancy.config.account_database`.

Two consequences worth stating outright, because they are the point:

1. A shopper who signs up as alice@example.com with Merchant A and a shopper
   who signs up as alice@example.com with Merchant B are *different accounts*.
   They are not merely filtered apart — they are in different databases, so
   there is no query that can return both, and the duplicate-email check in
   `create_user` correctly does not see across the boundary.
2. A session (and its messages, and the token that authenticates its owner)
   created under Merchant A is invisible to any request claiming Merchant B.
   Cross-tenant reads do not 403 — they simply find nothing.

Every document written here is *also* stamped with `tenant_id`, and reads
verify it. That is defence in depth behind the physical split, not the
primary guard: see `Database._belongs_to_tenant`.

Data Model:
-----------
1. Users can be:
   - Demo users: Pre-seeded accounts with order history (demo@example.com, jane@example.com)
   - New users: Fresh accounts that start with no order history

2. Demo users have known user_ids that link to pre-seeded order/billing data:
   - demo@example.com → user_id: "demo-user-001"
   - jane@example.com → user_id: "demo-user-002"

3. New users get random UUIDs and start with no orders, invoices, etc.
"""

import hashlib
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Dict, List, Optional

from google.api_core import exceptions as gcp_exceptions
from google.api_core import retry
from google.cloud import firestore

from customer_support_mas.database import get_db_client
from customer_support_mas.tenancy.config import account_database, load_tenant_config

from .logging_config import get_logger

logger = get_logger(__name__)


class TenantAccountStoreError(Exception):
    """A tenant's config names no database for its backend account data.

    Raised rather than falling back to a shared default — a silent fallback
    is precisely the bug this module's tenancy work exists to remove. For a
    firestore-backed tenant this means `provider_config.database_id` is
    missing; for any other provider it means the tenant doc needs an explicit
    `account_database_id`.
    """


# =============================================================================
# RETRY CONFIGURATION
# =============================================================================

# Firestore retry policy for transient errors
FIRESTORE_RETRY = retry.Retry(
    initial=0.1,  # Initial delay: 100ms
    maximum=10.0,  # Maximum delay: 10 seconds
    multiplier=2.0,  # Exponential backoff multiplier
    deadline=30.0,  # Total deadline: 30 seconds
    predicate=retry.if_exception_type(
        gcp_exceptions.ServiceUnavailable,  # 503
        gcp_exceptions.DeadlineExceeded,  # 504
        gcp_exceptions.InternalServerError,  # 500
        gcp_exceptions.Aborted,  # 409 (transaction conflict)
    ),
)


def with_retry(func):
    """
    Decorator to add retry logic to database operations.

    Retries on transient Firestore errors with exponential backoff.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        last_exception = None
        for attempt in range(3):  # Max 3 attempts
            try:
                return func(*args, **kwargs)
            except (
                gcp_exceptions.ServiceUnavailable,
                gcp_exceptions.DeadlineExceeded,
                gcp_exceptions.InternalServerError,
                gcp_exceptions.Aborted,
            ) as e:
                last_exception = e
                wait_time = (2**attempt) * 0.1  # 0.1s, 0.2s, 0.4s
                logger.warning(
                    "Firestore operation failed, retrying", attempt=attempt + 1, error=str(e), wait_seconds=wait_time
                )
                time.sleep(wait_time)
            except Exception:
                # Non-retryable error, raise immediately
                raise

        # All retries exhausted
        logger.error("Firestore operation failed after all retries", error=str(last_exception))
        raise last_exception

    return wrapper


# =============================================================================
# DEMO USER CONFIGURATION
# =============================================================================
# These must match the fixture data in customer_support_mas/database/fixtures.py

DEMO_USERS = {
    "demo@example.com": {
        "user_id": "demo-user-001",
        "name": "Demo User",
        "tier": "Gold",
        "password_hash": hashlib.sha256("demo123".encode()).hexdigest(),
    },
    "jane@example.com": {
        "user_id": "demo-user-002",
        "name": "Jane Smith",
        "tier": "Silver",
        "password_hash": hashlib.sha256("jane123".encode()).hexdigest(),
    },
}


def is_demo_email(email: str) -> bool:
    """Check if an email belongs to a demo account."""
    return email.lower() in DEMO_USERS


def get_demo_user_id(email: str) -> Optional[str]:
    """Get the pre-seeded user_id for a demo email."""
    demo = DEMO_USERS.get(email.lower())
    return demo["user_id"] if demo else None


class Database:
    def __init__(
        self,
        project_id: str,
        database_id: str,
        tenant_id: Optional[str] = None,
        client: Optional[firestore.Client] = None,
    ):
        """Initialize a Firestore-backed account store.

        Args:
            project_id: GCP project (only used when constructing our own client).
            database_id: Firestore database name this store lives in.
            tenant_id: The tenant this store belongs to. Stamped onto every
                document written and verified on every document read. None
                means "not tenant-scoped" — used only for the control-plane
                handle that backs the health check, never for serving
                account/session traffic.
            client: Pre-built Firestore client, for tests and for reusing
                `customer_support_mas.database.get_db_client`'s per-database
                cache.
        """
        self.database_id = database_id
        self.tenant_id = tenant_id
        self.db = client if client is not None else firestore.Client(project=project_id, database=database_id)
        logger.info(f"Connected to Firestore: {project_id}/{database_id} (tenant={tenant_id})")

    # =========================================================================
    # TENANT GUARD
    # =========================================================================

    def _belongs_to_tenant(self, data: Optional[Dict]) -> bool:
        """Defence in depth behind the physical per-tenant database split.

        The primary guard is that this client is bound to one tenant's own
        database, so a foreign document is not reachable to begin with. This
        check catches the residual cases: a store that was never re-pointed,
        a config edit that re-aimed a tenant at the wrong database, or a
        future move back to a shared database.

        A document with no `tenant_id` at all passes: it predates this field
        and can only have been written by the tenant that owns the database
        it is sitting in. Rejecting those would lock existing users out of
        their own accounts at deploy time.
        """
        if data is None:
            return False
        if self.tenant_id is None:
            return True
        doc_tenant = data.get("tenant_id")
        return doc_tenant is None or doc_tenant == self.tenant_id

    # =========================================================================
    # USER MANAGEMENT
    # =========================================================================

    @with_retry
    def create_user(self, email: str, name: str, password_hash: str) -> str:
        """
        Create a new user account.

        IMPORTANT: Demo user emails (demo@example.com, jane@example.com) are
        pre-seeded and cannot be registered again. Users should log in with
        these accounts instead.

        The duplicate-email check is deliberately scoped to this tenant's own
        database: the same email registering with a second merchant is a
        second, unrelated account, not a duplicate.

        Args:
            email: User email (unique identifier)
            name: User display name
            password_hash: Hashed password (use bcrypt in production)

        Returns:
            user_id: Generated user ID

        Raises:
            ValueError: If email belongs to a demo account
        """
        # Check if this is a demo email - demo users are pre-seeded
        if is_demo_email(email):
            logger.warning(f"Attempted to register demo email: {email}")
            raise ValueError(
                "This email is reserved for demo purposes. Please log in with password 'demo123' or 'jane123' instead."
            )

        # Check if user already exists
        existing = self.get_user_by_email(email)
        if existing:
            logger.warning(f"User already exists: {email}")
            raise ValueError("An account with this email already exists.")

        # Generate new user ID for non-demo users
        user_id = str(uuid.uuid4())

        user_data = {
            "user_id": user_id,
            "tenant_id": self.tenant_id,
            "email": email,
            "name": name,
            "password_hash": password_hash,
            "created_at": datetime.now(timezone.utc),
            "last_login": None,
            "is_demo": False,  # Mark as non-demo user
        }

        self.db.collection("users").document(user_id).set(user_data)
        logger.info(f"Created user: {user_id} ({email}) for tenant {self.tenant_id}")

        return user_id

    @with_retry
    def get_user_by_email(self, email: str) -> Optional[Dict]:
        """
        Get user by email address.

        This method checks Firestore for the user. Demo users should be
        pre-seeded in the database with known user_ids that link to
        their order/billing data.

        Args:
            email: User email

        Returns:
            User data dict or None if not found
        """
        email_lower = email.lower()

        # First, try to find in Firestore (works for both demo and regular users)
        # Scoped to this tenant's database — the same email under another
        # merchant is a different account and must not be found here.
        query = self.db.collection("users").where("email", "==", email_lower).limit(1)
        results = list(query.stream())

        if results:
            user_data = results[0].to_dict()
            if self._belongs_to_tenant(user_data):
                logger.info(f"Found user: {user_data['user_id']} ({email})")
                return user_data

        # Also check original case (for backwards compatibility)
        if email != email_lower:
            query = self.db.collection("users").where("email", "==", email).limit(1)
            results = list(query.stream())
            if results:
                user_data = results[0].to_dict()
                if self._belongs_to_tenant(user_data):
                    logger.info(f"Found user: {user_data['user_id']} ({email})")
                    return user_data

        # If this is a demo email but not found in DB, the seed hasn't run
        if is_demo_email(email):
            logger.warning(
                f"Demo user {email} not found in database. "
                f"Run the seed script: python -m customer_support_mas.database.fixtures --project YOUR_PROJECT"
            )

        logger.info(f"User not found: {email}")
        return None

    @with_retry
    def get_user(self, user_id: str) -> Optional[Dict]:
        """
        Get user by user_id.

        Args:
            user_id: User ID

        Returns:
            User data dict or None if not found
        """
        doc = self.db.collection("users").document(user_id).get()

        if doc.exists:
            data = doc.to_dict()
            if self._belongs_to_tenant(data):
                return data
            logger.warning("User doc rejected: tenant mismatch", user_id=user_id, tenant_id=self.tenant_id)

        return None

    @with_retry
    def update_last_login(self, user_id: str):
        """Update user's last login timestamp."""
        self.db.collection("users").document(user_id).update({"last_login": datetime.now(timezone.utc)})

    @with_retry
    def create_anonymous_user(self) -> str:
        """
        Create an anonymous user (for users who don't register).

        Returns:
            user_id: Generated anonymous user ID
        """
        user_id = f"anon-{uuid.uuid4()}"

        user_data = {
            "user_id": user_id,
            "tenant_id": self.tenant_id,
            "is_anonymous": True,
            "created_at": datetime.now(timezone.utc),
        }

        self.db.collection("users").document(user_id).set(user_data)
        logger.info(f"Created anonymous user: {user_id} for tenant {self.tenant_id}")

        return user_id

    # =========================================================================
    # SESSION MANAGEMENT
    #
    # The mutators below (update_session / rename_session / delete_session)
    # take a session_id and write to it without re-reading. They are safe
    # because (a) this client is bound to one tenant's database, so a foreign
    # session_id resolves to nothing, and (b) every caller in main.py first
    # loads the session through the tenant-checked `get_session` and returns
    # 404/403 before mutating. Doing the check again here would cost an extra
    # Firestore read on every chat turn (update_session runs per turn) for no
    # additional guarantee.
    # =========================================================================

    @with_retry
    def create_session(self, user_id: str, agent_engine_session_id: str, session_name: Optional[str] = None) -> str:
        """
        Create a new conversation session for a user.

        Args:
            user_id: User ID who owns this session
            agent_engine_session_id: The session ID from Agent Engine
            session_name: Optional name for the session

        Returns:
            session_id: Our internal session ID
        """
        session_id = str(uuid.uuid4())

        session_data = {
            "session_id": session_id,
            "tenant_id": self.tenant_id,
            "user_id": user_id,
            "agent_engine_session_id": agent_engine_session_id,
            "session_name": session_name or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "message_count": 0,
            "is_active": True,
        }

        self.db.collection("sessions").document(session_id).set(session_data)
        logger.info(f"Created session: {session_id} for user: {user_id}")

        return session_id

    @with_retry
    def get_session(self, session_id: str) -> Optional[Dict]:
        """
        Get session by session_id.

        A session belonging to another tenant is reported as "not found"
        rather than as a permission error — confirming that the id exists
        elsewhere would itself be a cross-tenant disclosure.

        Args:
            session_id: Session ID

        Returns:
            Session data dict or None if not found / not this tenant's
        """
        doc = self.db.collection("sessions").document(session_id).get()

        if doc.exists:
            data = doc.to_dict()
            if self._belongs_to_tenant(data):
                return data
            logger.warning("Session doc rejected: tenant mismatch", session_id=session_id, tenant_id=self.tenant_id)

        return None

    @with_retry
    def get_user_sessions(self, user_id: str, limit: int = 20) -> List[Dict]:
        """
        Get all sessions for a user.

        Args:
            user_id: User ID
            limit: Maximum number of sessions to return

        Returns:
            List of session data dicts, ordered by updated_at desc
        """
        # Simplified query - only filter by user_id to avoid composite index requirement
        # We'll sort and filter active sessions in Python
        query = (
            self.db.collection("sessions")
            .where("user_id", "==", user_id)
            .limit(limit * 2)  # Get more to account for inactive sessions
        )

        sessions = [doc.to_dict() for doc in query.stream()]

        # Filter for active sessions and sort by updated_at
        active_sessions = [s for s in sessions if s.get("is_active", True) and self._belongs_to_tenant(s)]
        active_sessions.sort(key=lambda x: x.get("updated_at", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

        # Limit to requested number
        result = active_sessions[:limit]

        logger.info(f"Found {len(result)} active sessions for user: {user_id}")

        return result

    @with_retry
    def update_session(self, session_id: str):
        """
        Update session's updated_at timestamp and increment message count.

        Args:
            session_id: Session ID
        """
        self.db.collection("sessions").document(session_id).update(
            {
                "updated_at": datetime.now(timezone.utc),
                "message_count": firestore.Increment(1),
            }
        )

    @with_retry
    def rename_session(self, session_id: str, new_name: str):
        """
        Rename a session.

        Args:
            session_id: Session ID
            new_name: New session name
        """
        self.db.collection("sessions").document(session_id).update(
            {
                "session_name": new_name,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        logger.info(f"Renamed session {session_id} to: {new_name}")

    @with_retry
    def delete_session(self, session_id: str):
        """
        Mark session as inactive (soft delete).

        Args:
            session_id: Session ID
        """
        self.db.collection("sessions").document(session_id).update(
            {
                "is_active": False,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        logger.info(f"Deleted session: {session_id}")

    # =========================================================================
    # TOKEN MANAGEMENT
    # =========================================================================

    @with_retry
    def create_token(self, user_id: str) -> str:
        """Generate and persist an auth token for user_id. Returns the token."""
        import secrets
        from datetime import timedelta

        token = secrets.token_urlsafe(32)
        self.db.collection("tokens").document(token).set(
            {
                "user_id": user_id,
                "tenant_id": self.tenant_id,
                "created_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            }
        )
        logger.info("Token created", user_id=user_id, tenant_id=self.tenant_id)
        return token

    @with_retry
    def verify_token(self, token: str) -> Optional[str]:
        """Return user_id for a valid, non-expired token, or None.

        Tokens live in their tenant's own database, so a token minted for
        Merchant A presented against Merchant B is simply not found — which
        is why every caller has to resolve the tenant *before* it can
        authenticate. See `backend/app/main.py`'s `get_current_user`.
        """
        doc = self.db.collection("tokens").document(token).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        if not self._belongs_to_tenant(data):
            logger.warning("Token rejected: tenant mismatch", tenant_id=self.tenant_id)
            return None
        if datetime.now(timezone.utc) > data["expires_at"]:
            self.db.collection("tokens").document(token).delete()
            logger.info("Token expired and deleted")
            return None
        return data["user_id"]

    @with_retry
    def revoke_token(self, token: str):
        """Delete a token (logout)."""
        self.db.collection("tokens").document(token).delete()
        logger.info("Token revoked")

    # =========================================================================
    # MESSAGE MANAGEMENT
    # =========================================================================

    @with_retry
    def save_message(self, session_id: str, role: str, content: str, message_id: Optional[str] = None) -> str:
        """
        Save a message to a session.

        Args:
            session_id: Session ID
            role: Message role ('user' or 'assistant')
            content: Message content
            message_id: Optional custom message ID

        Returns:
            message_id: The message ID
        """
        if not message_id:
            message_id = str(uuid.uuid4())

        message_data = {
            "message_id": message_id,
            "session_id": session_id,
            "tenant_id": self.tenant_id,
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc),
        }

        # Store in subcollection: sessions/{session_id}/messages/{message_id}
        self.db.collection("sessions").document(session_id).collection("messages").document(message_id).set(
            message_data
        )

        logger.info(f"Saved {role} message to session {session_id}")

        return message_id

    @with_retry
    def get_session_messages(self, session_id: str, limit: int = 100) -> List[Dict]:
        """
        Get all messages for a session.

        Args:
            session_id: Session ID
            limit: Maximum number of messages to return

        Returns:
            List of message dicts, ordered by timestamp asc
        """
        query = (
            self.db.collection("sessions")
            .document(session_id)
            .collection("messages")
            .order_by("timestamp")
            .limit(limit)
        )

        messages = [data for data in (doc.to_dict() for doc in query.stream()) if self._belongs_to_tenant(data)]

        logger.info(f"Retrieved {len(messages)} messages for session {session_id}")

        return messages


# Control-plane / default database instance, cached per (project, database)
# rather than as one global — the old single `_db_instance` silently returned
# the first database ever asked for, which is a landmine now that more than
# one database is in play.
_db_instances: Dict[tuple, Database] = {}


def get_database(project_id: str, database_id: str) -> Database:
    """Get or create a non-tenant-scoped database handle.

    Used for the control plane (the `tenants` collection) and the health
    check. Account and session traffic must go through
    `get_tenant_database(tenant_id)` instead — a handle returned here has
    `tenant_id=None` and therefore performs no tenant checks.
    """
    key = (project_id, database_id)
    if key not in _db_instances:
        _db_instances[key] = Database(project_id, database_id)
    return _db_instances[key]


# Tenant account stores, cached per tenant. Keyed on (tenant_id, database_id)
# so that re-pointing a tenant at a different database (an operator action,
# followed by invalidate_tenant_config_cache) yields a fresh handle instead
# of a stale one aimed at the old database.
_tenant_db_instances: Dict[tuple, Database] = {}


def get_tenant_database(tenant_id: str) -> Database:
    """Return the account store for `tenant_id` — its own Firestore database.

    Resolution goes through exactly the machinery the commerce layer uses:
    `load_tenant_config(tenant_id)` (in-process cached, so this is a dict
    lookup on the hot path) then `get_db_client(database_id)` (cached per
    database name). There is no default and no fallback.

    Raises:
        TenantNotFoundError: no such tenant (propagated from load_tenant_config).
        TenantConfigConflictError: two tenants resolve to one database.
        TenantAccountStoreError: the tenant names no account database.
    """
    config = load_tenant_config(tenant_id)
    database_id = account_database(config)
    if not database_id:
        raise TenantAccountStoreError(
            f"Tenant {tenant_id!r} (provider_type={config.provider_type!r}) has no account "
            "database configured — set provider_config.database_id or account_database_id "
            "on its tenants/ document"
        )

    key = (tenant_id, database_id)
    if key not in _tenant_db_instances:
        _tenant_db_instances[key] = Database(
            project_id=config.project_id or "",
            database_id=database_id,
            tenant_id=tenant_id,
            client=get_db_client(database_id),
        )
    return _tenant_db_instances[key]


def reset_tenant_database_cache() -> None:
    """Drop every cached tenant account store (tests; tenant re-pointing)."""
    _tenant_db_instances.clear()
