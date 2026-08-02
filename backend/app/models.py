from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field

# =============================================================================
# AUTHENTICATION
# =============================================================================


class RegisterRequest(BaseModel):
    """Register a new user account."""

    email: EmailStr = Field(..., description="User email address")
    name: str = Field(..., min_length=1, max_length=100, description="User display name")
    password: str = Field(..., min_length=8, description="Password (min 8 characters)")


class LoginRequest(BaseModel):
    """Login with email and password."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., description="Password")


class AuthResponse(BaseModel):
    """Response after successful login/register."""

    user_id: str = Field(..., description="User ID")
    token: str = Field(..., description="Authentication token")
    name: str = Field(..., description="User display name")
    email: str = Field(..., description="User email")


class AnonymousUserResponse(BaseModel):
    """Response for anonymous user creation."""

    user_id: str = Field(..., description="Anonymous user ID")
    is_anonymous: bool = Field(default=True, description="Flag indicating anonymous user")


# =============================================================================
# CHAT / MESSAGING
# =============================================================================


class ChatRequest(BaseModel):
    """
    Request for sending a message to the agent.

    Architecture:
    - Authenticated users: Pass auth token in Authorization header
    - Anonymous users: Pass user_id from AnonymousUserResponse
    - session_id: Identifies a specific conversation thread
    - tenant_id: Which merchant/tenant this chat belongs to (required, no
      default — see CommerceProvider's "no implicit tenant" constraint)
    """

    message: str = Field(..., min_length=1, description="User message")
    session_id: Optional[str] = Field(None, description="Optional session ID for specific conversation thread")
    tenant_id: str = Field(
        ...,
        description=(
            "Which merchant/tenant this chat is for — the embedding widget's "
            "own store identifier, required on every request"
        ),
    )


class RefundReasonCode(BaseModel):
    """One selectable refund reason from the active refund policy."""

    code: str
    label: str


class ChatResponse(BaseModel):
    """Response from the agent."""

    response: str = Field(..., description="Agent response")
    user_id: str = Field(..., description="User identifier")
    session_id: str = Field(..., description="Session ID for this conversation")
    reason_codes: Optional[List[RefundReasonCode]] = Field(
        None,
        description=(
            "Present only when check_if_refundable ran this turn: the "
            "active policy's eligible refund reasons, for the frontend to "
            "render as clickable options instead of a free-text prompt."
        ),
    )


# =============================================================================
# SESSION MANAGEMENT
# =============================================================================


class SessionInfo(BaseModel):
    """Information about a conversation session."""

    session_id: str
    session_name: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    is_active: bool


class SessionListResponse(BaseModel):
    """List of user's sessions."""

    user_id: str
    sessions: List[SessionInfo]


class RenameSessionRequest(BaseModel):
    """Request to rename a session."""

    session_name: str = Field(..., min_length=1, max_length=100, description="New session name")


class MessageInfo(BaseModel):
    """Information about a message in a conversation."""

    message_id: str
    session_id: str
    role: str  # 'user' or 'assistant'
    content: str
    timestamp: datetime


class MessageHistoryResponse(BaseModel):
    """Message history for a session."""

    session_id: str
    messages: List[MessageInfo]


# =============================================================================
# HEALTH CHECK
# =============================================================================


class HealthResponse(BaseModel):
    status: str
    agent_engine: str
    project: str
    location: str
