import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from customer_support_mas.providers.registry import get_provider
from customer_support_mas.rate_limiting import check_tenant_rate_limit
from customer_support_mas.tenancy.config import (
    TenantConfigConflictError,
    TenantNotFoundError,
    load_tenant_config,
)

from . import auth, refund_approvals
from .agent_client import agent_client
from .config import settings
from .database import Database, TenantAccountStoreError, get_database, get_tenant_database
from .health import HealthChecker, HealthStatus, liveness_check, readiness_check
from .logging_config import get_logger, logging_middleware, set_request_context, setup_logging
from .metrics import increment_chat_errors, increment_chat_requests, metrics, metrics_middleware
from .models import (
    AnonymousUserRequest,
    AnonymousUserResponse,
    AuthResponse,
    ChatRequest,
    ChatResponse,
    LoginRequest,
    MessageHistoryResponse,
    MessageInfo,
    RegisterRequest,
    RenameSessionRequest,
    SessionListResponse,
)
from .rate_limiter import RateLimitDependency

# Initialize structured logging
# Use JSON format in production, human-readable in development
is_production = os.getenv("ENVIRONMENT", "development") == "production"
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"), json_format=is_production, service_name="customer-support-api")
logger = get_logger(__name__)

# Model Armor (optional — only initialized when enabled)
_MODEL_ARMOR_ENABLED = os.getenv("MODEL_ARMOR_ENABLED", "false").lower() == "true"
_MODEL_ARMOR_TEMPLATE_ID = os.getenv("MODEL_ARMOR_TEMPLATE_ID", "")
_MODEL_ARMOR_MODE = os.getenv("MODEL_ARMOR_MODE", "INSPECT_AND_BLOCK").upper()
_model_armor_client = None
_modelarmor_v1 = None
_parse_ma_response = None
if _MODEL_ARMOR_ENABLED and _MODEL_ARMOR_TEMPLATE_ID:
    try:
        from google.api_core.client_options import ClientOptions as _ClientOptions
        from google.cloud import modelarmor_v1 as _modelarmor_v1

        from app.safety_util import parse_model_armor_response as _parse_ma_response

        _location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        _model_armor_client = _modelarmor_v1.ModelArmorClient(
            client_options=_ClientOptions(api_endpoint=f"modelarmor.{_location}.rep.googleapis.com")
        )
    except Exception as _ma_init_err:
        logging.getLogger(__name__).warning("Model Armor init failed: %s", _ma_init_err)

# Control-plane database handle. This is NOT where accounts or sessions live
# any more — those are per-tenant, resolved per request via
# resolve_tenant_database(). This handle exists for the `tenants` collection's
# own database and to give the health check something to ping; it is
# deliberately not tenant-scoped (tenant_id=None) and must never be used to
# read or write user/session/token/message data.
control_plane_db = get_database(
    project_id=settings.google_cloud_project,
    database_id=os.getenv("FIRESTORE_DATABASE", "customer-support-db"),
)

# Initialize health checker (agent_client added after import)
health_checker = HealthChecker(db=control_plane_db, agent_client=None)

app = FastAPI(
    title="Customer Support AI Backend",
    description="Backend API for Customer Support Multi-Agent System with User Management",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url, "http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add logging middleware for request context
app.middleware("http")(logging_middleware)

# Add metrics middleware for request tracking
app.middleware("http")(metrics_middleware)


# =============================================================================
# APPLICATION LIFECYCLE EVENTS
# =============================================================================


@app.on_event("startup")
async def startup_event():
    """
    Application startup handler.

    Initializes resources and logs startup information.
    """
    logger.info(
        "Application starting up",
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
        version="2.0.0",
    )

    # Set initial metrics
    metrics.set_gauge("app_info", 1)


@app.on_event("shutdown")
async def shutdown_event():
    """
    Graceful shutdown handler.

    Ensures clean shutdown of resources:
    - Logs shutdown event
    - Allows in-flight requests to complete (handled by uvicorn)
    - Could close database connections if needed
    """
    logger.info("Application shutting down - starting graceful shutdown")

    # Log final metrics before shutdown
    final_metrics = metrics.get_all_metrics()
    logger.info(
        "Final metrics before shutdown",
        total_requests=final_metrics["summary"]["total_requests"],
        total_errors=final_metrics["summary"]["total_errors"],
        uptime_seconds=final_metrics["uptime_seconds"],
    )

    # Note: Uvicorn handles waiting for in-flight requests by default
    # with its --timeout-graceful-shutdown option (default 30s)

    logger.info("Graceful shutdown complete")


# =============================================================================
# TENANT RESOLUTION + AUTHENTICATION DEPENDENCY
#
# Ordering constraint (the load-bearing bit of this module's tenancy design):
# auth tokens live in their tenant's own Firestore database, so `tenant_id`
# has to be known BEFORE a bearer token can be verified. That is why:
#   - every GET/DELETE endpoint takes `tenant_id` as a *required query
#     parameter*, which `get_current_user` declares too, so FastAPI has it in
#     hand while solving the dependency;
#   - `/api/chat`, whose tenant_id arrives in the request body, does NOT use
#     the dependency. A FastAPI dependency cannot read a body model that the
#     path operation also declares (two body params of the same name make
#     FastAPI switch to an embedded body and change the wire format), so chat
#     resolves the tenant and then authenticates inline, in that order.
# =============================================================================


def resolve_tenant_database(tenant_id: str) -> Database:
    """Resolve `tenant_id` to its own account store, or raise a clean HTTP error.

    Every tenant-facing entry point funnels through here so that the mapping
    from tenancy errors to status codes exists in exactly one place:
      - unknown tenant        -> 404 (names the id; it came from the caller)
      - two tenants, one db   -> 503 (generic: the real message names both
                                tenants and the shared database)
      - no account db config  -> 503 (generic: same reason)

    `load_tenant_config` is called explicitly first, before
    `get_tenant_database` (which calls it again, from its in-process cache).
    The duplicate is intentional and free: it keeps validation of the
    caller-supplied tenant_id visibly ahead of everything else in the request
    path, which is the property tests/unit/test_chat_tenant_validation.py
    pins down.
    """
    try:
        load_tenant_config(tenant_id)
    except TenantNotFoundError:
        logger.warning("Request for unknown tenant", tenant_id=tenant_id)
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tenant_id}")
    except TenantConfigConflictError as exc:
        # A misconfiguration, not a client error — and its message names
        # BOTH colliding tenant ids and the shared database name, so it must
        # never reach the caller. Log it server-side, return a generic 503.
        logger.error("Tenant config conflict", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(status_code=503, detail="Service temporarily unavailable for this tenant")

    try:
        return get_tenant_database(tenant_id)
    except TenantAccountStoreError as exc:
        logger.error("Tenant has no account store configured", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(status_code=503, detail="Service temporarily unavailable for this tenant")


def authenticate_bearer(tenant_db: Database, authorization: Optional[str]) -> Optional[str]:
    """Verify an Authorization header against ONE tenant's token store.

    Returns None when no header was supplied (the caller may still be an
    anonymous X-User-Id user). Raises 401 for a malformed header or a token
    that this tenant's database does not know — including a token that is
    perfectly valid for a *different* tenant, which is the point.
    """
    if not authorization:
        return None

    # Expected format: "Bearer <token>"
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    user_id = tenant_db.verify_token(parts[1])

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return user_id


def get_current_user(tenant_id: str, authorization: Optional[str] = Header(None)) -> Optional[str]:
    """
    Extract user_id from the Authorization header, within a tenant.

    `tenant_id` is a required query parameter on every endpoint using this
    dependency — a token can only be verified against the database of the
    tenant it was issued for.

    Returns:
        user_id if authenticated, None if anonymous
    """
    if not authorization:
        # Nothing to verify — don't pay for tenant resolution to say so.
        return None

    return authenticate_bearer(resolve_tenant_database(tenant_id), authorization)


def require_approver(tenant_id: str, user_id: Optional[str] = Depends(get_current_user)) -> str:
    """Dependency gating the refund-approval endpoints to approver-role users.

    401 if unauthenticated (get_current_user returned None because no/invalid
    Authorization header was supplied — get_current_user itself already
    raises 401 for a malformed/invalid token, so this only needs to handle
    the "no header at all" case where it returns None).
    403 if authenticated but the user's Firestore doc has no role or a role
    other than "approver". No pending-request data is returned in either
    error case.

    The role lookup reads the *tenant's own* users collection, so an
    approver of one merchant is not even visible while serving another.
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    user = resolve_tenant_database(tenant_id).get_user(user_id)
    if not user or user.get("role") != "approver":
        raise HTTPException(status_code=403, detail="Approver role required")
    return user_id


@app.get("/health")
async def health_check():
    """
    Comprehensive health check endpoint.

    Checks connectivity to:
    - Database (Firestore)
    - Agent Engine

    Returns:
        - 200: All systems healthy
        - 503: System degraded or unhealthy
    """
    from fastapi.responses import JSONResponse

    # Update health checker with agent client if available
    health_checker.agent_client = agent_client

    result = await health_checker.check_all()

    # Return 503 if unhealthy
    status_code = 200 if result.status != HealthStatus.UNHEALTHY else 503

    return JSONResponse(content=result.to_dict(), status_code=status_code)


@app.get("/health/live")
async def liveness_probe():
    """
    Kubernetes liveness probe.

    Returns 200 if the process is running.
    Used to determine if pod should be restarted.
    """
    return await liveness_check()


@app.get("/health/ready")
async def readiness_probe():
    """
    Kubernetes readiness probe.

    Returns 200 if service can handle requests.
    Used to determine if pod should receive traffic.
    """
    from fastapi.responses import JSONResponse

    health_checker.agent_client = agent_client
    result = await readiness_check(health_checker)

    status_code = 200 if result["ready"] else 503

    return JSONResponse(content=result, status_code=status_code)


# =============================================================================
# METRICS ENDPOINTS
# =============================================================================


@app.get("/metrics")
async def get_metrics():
    """
    Get application metrics in JSON format.

    Returns request counts, latencies, error rates by endpoint.
    """
    return metrics.get_all_metrics()


@app.get("/metrics/prometheus")
async def get_prometheus_metrics():
    """
    Get application metrics in Prometheus format.

    Use this endpoint for Prometheus scraping.
    """
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(content=metrics.get_prometheus_format(), media_type="text/plain")


# =============================================================================
# AUTHENTICATION ENDPOINTS
# =============================================================================


@app.post("/api/auth/register", response_model=AuthResponse)
async def register(request: RegisterRequest, _rate_check: bool = Depends(RateLimitDependency("auth"))):
    """Register a new user account with one merchant.

    The account is created in that merchant's own Firestore database, so the
    same email registering with a second merchant creates a second,
    independent account rather than colliding with the first.
    """
    try:
        tenant_db = resolve_tenant_database(request.tenant_id)

        # Hash password and create user
        # Note: create_user() handles demo email validation and duplicate check
        password_hash = auth.hash_password(request.password)
        user_id = tenant_db.create_user(email=request.email, name=request.name, password_hash=password_hash)

        # Generate auth token
        token = tenant_db.create_token(user_id)

        logger.info("User registered", user_id=user_id, email=request.email, tenant_id=request.tenant_id)

        return AuthResponse(user_id=user_id, token=token, name=request.name, email=request.email)

    except ValueError as e:
        # Handle demo email registration attempt or duplicate email
        logger.warning("Registration rejected", reason=str(e), email=request.email)
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Registration error", error=str(e), email=request.email)
        raise HTTPException(status_code=500, detail="Registration failed")


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: LoginRequest, _rate_check: bool = Depends(RateLimitDependency("auth"))):
    """Login with email and password, against one merchant's accounts.

    Credentials that are valid for another merchant do not authenticate here
    — that account simply does not exist in this tenant's database, and the
    response is the same generic "Invalid email or password" as for any
    unknown user (never a hint that the email exists elsewhere).
    """
    try:
        tenant_db = resolve_tenant_database(request.tenant_id)

        # Get user by email
        user = tenant_db.get_user_by_email(request.email)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Verify password
        if not auth.verify_password(request.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # Update last login
        tenant_db.update_last_login(user["user_id"])

        # Generate auth token
        token = tenant_db.create_token(user["user_id"])

        logger.info("User logged in", user_id=user["user_id"], email=request.email, tenant_id=request.tenant_id)

        return AuthResponse(user_id=user["user_id"], token=token, name=user["name"], email=user["email"])

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Login error", error=str(e), email=request.email)
        raise HTTPException(status_code=500, detail="Login failed")


@app.post("/api/auth/anonymous", response_model=AnonymousUserResponse)
async def create_anonymous(
    request: AnonymousUserRequest,
    _rate_check: bool = Depends(RateLimitDependency("auth")),
):
    """Create an anonymous user under one merchant.

    Takes a body now (it used to take none): the anonymous user document is
    written to the tenant's own database, so the tenant has to be named.
    """
    try:
        tenant_db = resolve_tenant_database(request.tenant_id)
        user_id = tenant_db.create_anonymous_user()

        return AnonymousUserResponse(user_id=user_id, is_anonymous=True)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Anonymous user creation error", error=str(e), tenant_id=request.tenant_id)
        raise HTTPException(status_code=500, detail="Failed to create anonymous user")


@app.post("/api/auth/logout")
async def logout(tenant_id: str, authorization: str = Header(...)):
    """Logout (revoke token).

    `tenant_id` is a required query parameter: the token document lives in
    that tenant's own database, so there is nowhere else to delete it from.
    A token issued by another tenant is not reachable here and revoking it
    is a no-op — deliberately reported as success, so this endpoint cannot
    be used to probe which tenant a token belongs to.
    """
    try:
        tenant_db = resolve_tenant_database(tenant_id)

        # Extract token
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
            tenant_db.revoke_token(token)
            return {"status": "logged_out"}

        raise HTTPException(status_code=400, detail="Invalid authorization header")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Logout error", error=str(e))
        raise HTTPException(status_code=500, detail="Logout failed")


# =============================================================================
# CHAT ENDPOINT
# =============================================================================


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("chat")),
):
    """
    Send a message to the customer support agent.

    Supports both:
    - Authenticated users (via Authorization: Bearer token header)
    - Anonymous users (via X-User-Id header with anon-* user_id)

    Unlike every other authenticated endpoint here, this one does NOT use the
    `get_current_user` dependency: its `tenant_id` arrives in the request
    body, and a FastAPI dependency cannot read a body model that the path
    operation also declares without changing the wire format (two body params
    make FastAPI embed the body). Tokens are stored per tenant, so the tenant
    must be resolved before the token can be verified — hence the explicit,
    ordered resolve-then-authenticate below.

    Args:
        request: ChatRequest with message, optional session_id, and required tenant_id
        authorization: Bearer token header (if authenticated)
        x_user_id: Extracted from X-User-Id header (if anonymous)

    Returns:
        ChatResponse with agent response, user_id, and session_id
    """
    try:
        # Resolve the tenant BEFORE anything else touches request.tenant_id.
        # It is a caller-supplied string: without this check an unknown value
        # would (a) be accepted as a rate-limit bucket key, letting any client
        # burn another tenant's budget by claiming their id, and grow
        # rate_limiting._buckets without bound on arbitrary strings, and
        # (b) survive all the way into a tool call, where the resulting
        # TenantNotFoundError is swallowed by @tool_error_handler into a
        # vague chat reply instead of a clean 404. load_tenant_config is
        # in-process cached, so on the hot path this is a dict lookup.
        #
        # It also has to come first for a second reason now: the auth token
        # is verified against this tenant's own token store, so there is no
        # authenticating anyone until the tenant is known.
        tenant_db = resolve_tenant_database(request.tenant_id)

        # Determine user_id (auth takes precedence over anonymous)
        user_id = authenticate_bearer(tenant_db, authorization)
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Use Authorization header or X-User-Id for anonymous users.",
            )

        # Per-tenant rate limit — an additional ceiling on top of the
        # per-user RateLimitDependency("chat") check above, not a
        # replacement for it: prevents one tenant from starving others
        # sharing the same light-tier pool project's quota (spec section 6).
        if not check_tenant_rate_limit(request.tenant_id):
            raise HTTPException(
                status_code=429, detail="Rate limit exceeded for this tenant. Please try again shortly."
            )

        # Set user context for logging
        set_request_context(user_id=actual_user_id, session_id=request.session_id)
        logger.info("Chat request received", message_preview=request.message[:50])

        # Track chat request metric
        increment_chat_requests()

        # Check if this is a new session or existing one
        if request.session_id:
            # Verify session belongs to user AND to this tenant.
            #
            # The tenant half matters more than it looks: agent_client only
            # writes tenant_id into the Agent Engine session's state at
            # *creation* and never re-passes it on continuation (by design —
            # see agent_client.query_agent's docstring). So a resumed
            # conversation keeps running under whatever tenant it was created
            # with, regardless of what this request claims. Without the check
            # below, a caller could hand a session_id created under Merchant A
            # to a request naming Merchant B and have the agent keep serving
            # Merchant A's data under Merchant B's rate-limit budget and audit
            # trail. `get_session` is already bound to this tenant's database,
            # so a foreign session normally isn't found at all; the explicit
            # comparison is the second line of defence.
            session = tenant_db.get_session(request.session_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            if session["user_id"] != actual_user_id:
                raise HTTPException(status_code=403, detail="Session does not belong to user")
            session_tenant = session.get("tenant_id")
            if session_tenant is not None and session_tenant != request.tenant_id:
                # 404, not 403: confirming the session exists under a
                # different tenant would itself be a cross-tenant disclosure.
                logger.warning(
                    "Session/tenant mismatch on chat continuation",
                    session_id=request.session_id,
                    claimed_tenant=request.tenant_id,
                )
                raise HTTPException(status_code=404, detail="Session not found")

            internal_session_id = request.session_id
            agent_engine_session_id = session["agent_engine_session_id"]

            logger.info("Using existing session", session_id=internal_session_id)
        else:
            # Create new session
            internal_session_id = None
            agent_engine_session_id = None
            logger.info("Creating new session")

        # Model Armor safety check — screen user prompt before sending to agent
        if _model_armor_client and _MODEL_ARMOR_TEMPLATE_ID:
            try:
                ma_response = _model_armor_client.sanitize_user_prompt(
                    request=_modelarmor_v1.SanitizeUserPromptRequest(
                        name=_MODEL_ARMOR_TEMPLATE_ID,
                        user_prompt_data=_modelarmor_v1.DataItem(text=request.message),
                    )
                )
                violations = _parse_ma_response(ma_response)
                if violations:
                    if _MODEL_ARMOR_MODE == "INSPECT_AND_BLOCK":
                        logger.warning("Model Armor blocked user prompt", violations=str(violations))
                        raise HTTPException(
                            status_code=400,
                            detail="I'm sorry, I can't process this request as it violates our safety policy. Please contact support if you need assistance.",
                        )
                    else:
                        logger.info(
                            "Model Armor flagged user prompt (INSPECT_ONLY — not blocked)", violations=str(violations)
                        )
            except HTTPException:
                raise
            except Exception as ma_err:
                logger.error("Model Armor check error (failing open)", error=str(ma_err))

        # Query the agent
        response_text, agent_engine_session_id, reason_codes = await agent_client.query_agent(
            user_id=actual_user_id,
            agent_engine_session_id=agent_engine_session_id,
            message=request.message,
            tenant_id=request.tenant_id,
        )

        # If new session, create it in database
        if not internal_session_id:
            internal_session_id = tenant_db.create_session(
                user_id=actual_user_id, agent_engine_session_id=agent_engine_session_id
            )
            logger.info("Created new session", session_id=internal_session_id)
        else:
            # Update existing session
            tenant_db.update_session(internal_session_id)

        # Save messages to database for UI display
        tenant_db.save_message(internal_session_id, "user", request.message)
        tenant_db.save_message(internal_session_id, "assistant", response_text)

        return ChatResponse(
            response=response_text,
            user_id=actual_user_id,
            session_id=internal_session_id,
            reason_codes=reason_codes,
        )

    except HTTPException:
        increment_chat_errors()
        raise
    except TimeoutError as e:
        increment_chat_errors()
        logger.warning("Chat request timed out", error=str(e))
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        increment_chat_errors()
        logger.error("Error processing chat request", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")


# =============================================================================
# SESSION MANAGEMENT ENDPOINTS
#
# All four take `tenant_id` as a REQUIRED query parameter. They have no
# request body to carry it in, and it cannot be optional: sessions live in
# their tenant's own database, and `get_current_user` needs it to know which
# token store to verify the caller's bearer token against. A missing
# tenant_id is a 422, never an implicit default.
#
# Cross-tenant access therefore fails twice over: the token doesn't verify
# against the wrong tenant's store (401), and even an anonymous X-User-Id
# caller finds no session there (404).
# =============================================================================


@app.get("/api/sessions", response_model=SessionListResponse)
async def list_sessions(
    tenant_id: str,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Get the current user's sessions with one merchant."""
    try:
        tenant_db = resolve_tenant_database(tenant_id)
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        sessions = tenant_db.get_user_sessions(actual_user_id)

        from .models import SessionInfo

        session_list = [SessionInfo(**session) for session in sessions]

        return SessionListResponse(user_id=actual_user_id, sessions=session_list)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error listing sessions", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to list sessions")


@app.put("/api/sessions/{session_id}/rename")
async def rename_session(
    session_id: str,
    request: RenameSessionRequest,
    tenant_id: str,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Rename a session."""
    try:
        tenant_db = resolve_tenant_database(tenant_id)
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Verify session belongs to user (and, via tenant_db, to this tenant)
        session = tenant_db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["user_id"] != actual_user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to user")

        tenant_db.rename_session(session_id, request.session_name)

        return {"status": "success", "session_id": session_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error renaming session", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to rename session")


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str,
    tenant_id: str,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Delete a session."""
    try:
        tenant_db = resolve_tenant_database(tenant_id)
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Verify session belongs to user (and, via tenant_db, to this tenant)
        session = tenant_db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["user_id"] != actual_user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to user")

        tenant_db.delete_session(session_id)

        return {"status": "deleted", "session_id": session_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting session", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to delete session")


@app.get("/api/sessions/{session_id}/messages", response_model=MessageHistoryResponse)
async def get_session_messages(
    session_id: str,
    tenant_id: str,
    user_id: Optional[str] = Depends(get_current_user),
    x_user_id: Optional[str] = Header(None),
    _rate_check: bool = Depends(RateLimitDependency("sessions")),
):
    """Get message history for a session."""
    try:
        tenant_db = resolve_tenant_database(tenant_id)
        actual_user_id = user_id or x_user_id

        if not actual_user_id:
            raise HTTPException(status_code=401, detail="Authentication required")

        # Verify session belongs to user (and, via tenant_db, to this tenant)
        session = tenant_db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["user_id"] != actual_user_id:
            raise HTTPException(status_code=403, detail="Session does not belong to user")

        # Get messages
        messages = tenant_db.get_session_messages(session_id)

        message_list = [MessageInfo(**msg) for msg in messages]

        return MessageHistoryResponse(session_id=session_id, messages=message_list)

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching messages", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to fetch messages")


# =============================================================================
# REFUND APPROVAL ENDPOINTS (HITL step 3 — approver API)
# =============================================================================


def resolve_refund_request_store(tenant_id: str):
    """Return the Firestore handle holding `tenant_id`'s refund_requests.

    This must be the *tenant's own* database — `process_refund` stages
    PENDING_APPROVAL documents into `get_provider(tenant_id)._db`, so an
    approver API reading from a single hardcoded `database_id` (as this
    module used to) can only ever see the one tenant whose configured
    `database_id` happens to match. For every other tenant the approval
    queue looked empty and approving raised "not found" — the HITL refund
    workflow was silently broken for all of them.

    Raises 404 for an unknown tenant and 501 for a provider with no
    refund-staging store of its own (a Shopify-backed tenant: refund staging
    is this product's workflow layer, not something Shopify hosts — see
    customer_support_mas/agents/refund/tools.py's matching guard).
    """
    try:
        provider = get_provider(tenant_id)
    except TenantNotFoundError:
        raise HTTPException(status_code=404, detail=f"Unknown tenant_id: {tenant_id}")
    except TenantConfigConflictError as exc:
        # Same reasoning as /api/chat's handler: the conflict message names
        # both colliding tenant ids and the shared database, so it stays in
        # the logs and the caller gets a generic 503.
        logger.error("Tenant config conflict resolving refund store", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(status_code=503, detail="Service temporarily unavailable for this tenant")

    store = getattr(provider, "_db", None)
    if store is None:
        raise HTTPException(
            status_code=501,
            detail=f"Tenant {tenant_id} uses a provider with no refund-request store; refund approval is unavailable",
        )
    return store


def require_approver_for_tenant(tenant_id: str, approver_id: str = Depends(require_approver)) -> str:
    """`require_approver` plus an explicit tenant check, for the
    tenant-scoped admin endpoints.

    The primary guard is now structural rather than this comparison: users
    live in their tenant's own database, so `require_approver`'s own lookup
    already fails (403) for an approver of a different merchant, and the
    bearer token that named them fails earlier still (401), having been
    issued into a different tenant's token store. This function's remaining
    job is to enforce the `tenant_id` field on the user document itself, for
    a store that has not been re-pointed or a document that predates the
    split — a user doc with no `tenant_id` at all is accepted, since it can
    only have been written by the tenant owning the database it sits in.
    """
    user = resolve_tenant_database(tenant_id).get_user(approver_id) or {}
    user_tenant = user.get("tenant_id")
    if user_tenant is not None and user_tenant != tenant_id:
        raise HTTPException(status_code=403, detail="Approver is not authorized for this tenant")
    return approver_id


@app.get("/api/admin/refunds/pending")
async def pending_refunds(tenant_id: str, approver_id: str = Depends(require_approver_for_tenant)):
    """List a tenant's PENDING_APPROVAL refund requests. Approver-only.

    `tenant_id` is a required query parameter — there is no default tenant,
    and enumerating every tenant's queue from one endpoint would both cost a
    Firestore connection per tenant and hand one merchant's approver another
    merchant's customer data.
    """
    try:
        store = resolve_refund_request_store(tenant_id)
        return {"requests": refund_approvals.list_pending(store, tenant_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error listing pending refunds", tenant_id=tenant_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list pending refunds")


@app.post("/api/admin/refunds/{request_id}/approve")
async def approve_refund_endpoint(
    request_id: str, tenant_id: str, approver_id: str = Depends(require_approver_for_tenant)
):
    """Approve a pending refund request and execute the refund. Approver-only.

    `tenant_id` is required rather than read off the staged document,
    because the document has to be located before it can be read — and it
    lives in that tenant's own database. It is then matched against the
    document's own `tenant_id` inside `refund_approvals._get_request`, so a
    mismatch is a 404, never a cross-tenant write.
    """
    try:
        store = resolve_refund_request_store(tenant_id)
        return refund_approvals.approve_refund(store, tenant_id, request_id, approver_id)
    except HTTPException:
        raise
    except refund_approvals.ApprovalError as exc:
        status_code = {"not_found": 404, "not_pending": 409, "self_approval": 403}.get(exc.code, 400)
        raise HTTPException(status_code=status_code, detail=str(exc))
    except Exception as e:
        logger.error(
            "Unexpected error approving refund",
            request_id=request_id,
            tenant_id=tenant_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to approve refund")


@app.post("/api/admin/refunds/{request_id}/reject")
async def reject_refund_endpoint(
    request_id: str,
    tenant_id: str,
    body: dict = {},
    approver_id: str = Depends(require_approver_for_tenant),
):
    """Reject a pending refund request. Never writes to the refunds collection. Approver-only."""
    try:
        store = resolve_refund_request_store(tenant_id)
        return refund_approvals.reject_refund(store, tenant_id, request_id, approver_id, note=body.get("note", ""))
    except HTTPException:
        raise
    except refund_approvals.ApprovalError as exc:
        status_code = {"not_found": 404, "not_pending": 409}.get(exc.code, 400)
        raise HTTPException(status_code=status_code, detail=str(exc))
    except Exception as e:
        logger.error(
            "Unexpected error rejecting refund",
            request_id=request_id,
            tenant_id=tenant_id,
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Failed to reject refund")


@app.get("/api")
async def api_root():
    """API root endpoint"""
    return {
        "message": "Customer Support AI Backend v2.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "auth": {
                "register": "POST /api/auth/register (body: tenant_id)",
                "login": "POST /api/auth/login (body: tenant_id)",
                "anonymous": "POST /api/auth/anonymous (body: tenant_id)",
                "logout": "POST /api/auth/logout?tenant_id=",
            },
            "chat": "POST /api/chat (body: tenant_id)",
            "sessions": {
                "list": "GET /api/sessions?tenant_id=",
                "rename": "PUT /api/sessions/{id}/rename?tenant_id=",
                "delete": "DELETE /api/sessions/{id}?tenant_id=",
                "messages": "GET /api/sessions/{id}/messages?tenant_id=",
            },
        },
        "tenancy": "Every endpoint above requires a tenant_id. There is no default tenant.",
    }


# Serve static frontend files
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/")
    async def serve_frontend():
        """Serve the React frontend"""
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"message": "Frontend not found. Build the frontend first."}

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve SPA - return index.html for all non-API routes"""
        if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("openapi.json"):
            raise HTTPException(status_code=404, detail="Not found")

        file_path = static_dir / full_path
        if file_path.is_file():
            return FileResponse(file_path)

        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        raise HTTPException(status_code=404, detail="Not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=settings.port)
