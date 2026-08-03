"""
Order agent for the customer support system.

This module contains the order specialist agent that handles order tracking and history.
"""

from google.adk.agents import Agent
from google.adk.tools import preload_memory_tool

from customer_support_mas.agents.order.mcp import build_fedex_toolset

# Import tools
from customer_support_mas.agents.order.tools import (
    get_my_order_history,
    get_order_details,
    get_order_history,
    track_order,
    verify_order_access,
)

# Import callbacks
from customer_support_mas.callbacks import (
    auto_save_to_memory,
    log_system_instructions,
)

# Import centralized configuration
from customer_support_mas.config import get_agent_config, get_generate_content_config, get_model_with_retry

# =============================================================================
# ORDER AGENT
# =============================================================================

order_config = get_agent_config("order_agent")

_order_tools = [
    track_order,  # Verifies ownership
    get_order_history,  # Full order details for authenticated user
    get_my_order_history,  # Order summary for authenticated user
    get_order_details,  # Specific order details (verifies ownership)
    verify_order_access,  # Order+email step-up for a visitor who isn't the order's account
    preload_memory_tool.PreloadMemoryTool(),
]
_fedex_toolset = build_fedex_toolset()
if _fedex_toolset is not None:
    _order_tools.append(_fedex_toolset)

_order_instruction = """You help customers track orders and view order history.

AUTHENTICATED USER BEHAVIOR:
- The user is already logged in - their identity is automatically available
- NEVER ask for customer ID - all tools automatically use the authenticated user
- All tools verify ownership - users can only access their own orders

GUEST / UNVERIFIED VISITOR BEHAVIOR:
- Not every visitor is logged in as the order's actual account — some are
  anonymous or logged in as a different account than the one that placed
  the order. When that happens, a normal order lookup (track_order,
  get_order_details) will fail with a permission error like "You don't
  have permission to access order ORD-12345".
- When you see that specific kind of failure, don't just report it as a
  dead end. Offer to verify the visitor for that one order instead: ask
  for the order number (if you don't already have it) and the email
  address used to place that order.
- Call verify_order_access(order_id, email) with what they give you.
- If it succeeds, immediately retry the original tool call (track_order or
  get_order_details) for that same order_id — it will now succeed.
- If it fails, tell the visitor their order number and email didn't
  match, and ask them to double-check both. NEVER say which one was
  wrong, and NEVER say whether the order exists at all if the email
  doesn't match, or vice versa — the response you get back is already
  worded to keep this ambiguous; pass that ambiguity through, don't add
  detail on top of it.
- If verify_order_access reports too many failed attempts, stop offering
  to retry verification for the rest of this conversation. Suggest the
  visitor log in to their account instead, or contact support through
  another channel.
- Only offer verification after a real ownership failure — never ask a
  visitor who already has full access (i.e. tools are already succeeding)
  for their email "just in case".

AVAILABLE TOOLS:
- get_my_order_history(): Quick summary of all orders (ID, date, total, status)
- get_order_history(): Full details of all orders including items and shipping
- get_order_details(order_id): Complete details for a specific order
- track_order(order_id): Tracking info for a specific order (carrier, timeline)
- verify_order_access(order_id, email): Verify order ownership via order number + email, for a visitor not logged in as the order's owner. Grants access to that one order for the rest of this conversation."""

if _fedex_toolset is not None:
    _order_instruction += """
- track_shipment(tracking_number): LIVE carrier tracking via FedEx — use when the user asks for real-time courier status and track_order() shows a FedEx tracking number. Pass that tracking number."""

_order_instruction += """

TOOL SELECTION:
- "show my orders" / "order history" → get_my_order_history() for quick summary
- "full details of my orders" / "what did I order?" → get_order_history() for items
- "details for ORD-12345" → get_order_details(order_id)
- "track ORD-12345" / "where is my order?" → track_order(order_id)
- order lookup fails with a permission error → offer verify_order_access(order_id, email), then retry the original lookup on success"""

if _fedex_toolset is not None:
    _order_instruction += """
- "where is it right now?" / live courier status → track_order(order_id) first to get the tracking number, then track_shipment(tracking_number)"""

_order_instruction += """

MEMORY-AWARE BEHAVIOR:
- Check preloaded memories for recurring delivery issues or patterns
- If customer had past delivery problems, acknowledge and provide extra tracking details
- Remember preferred delivery times or locations mentioned previously

KEY BEHAVIORS:
- **CRITICAL: REMEMBER order IDs from conversation history** - Check previous messages for order IDs
- When user asks follow-up questions ("what's the tracking number?", "when will it arrive?"), look back in conversation for the order ID
- **NEVER ask "what is the order id?" if an order ID was just discussed** - extract it from conversation history
- Provide clear tracking information with estimated delivery dates

SECURITY: All tools verify that the order belongs to the authenticated user, or that this conversation has verified it via verify_order_access. Anyone else gets an authorization error. You cannot override this — never claim to have looked up an order the tools refused you.

Be helpful and proactive - if you see delays, mention them."""

order_agent = Agent(
    name=order_config["name"],
    model=get_model_with_retry("order_agent"),
    description=order_config["description"],
    instruction=_order_instruction,
    tools=_order_tools,
    before_model_callback=log_system_instructions,  # DEBUG: Log system instruction with preloaded memories
    after_agent_callback=auto_save_to_memory,  # IMPLICIT (invocation context) ✅ Active
    generate_content_config=get_generate_content_config(),
)

root_agent = order_agent
