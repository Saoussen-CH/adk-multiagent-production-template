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

AVAILABLE TOOLS:
- get_my_order_history(): Quick summary of all orders (ID, date, total, status)
- get_order_history(): Full details of all orders including items and shipping
- get_order_details(order_id): Complete details for a specific order
- track_order(order_id): Tracking info for a specific order (carrier, timeline)"""

if _fedex_toolset is not None:
    _order_instruction += """
- track_shipment(tracking_number): LIVE carrier tracking via FedEx — use when the user asks for real-time courier status and track_order() shows a FedEx tracking number. Pass that tracking number."""

_order_instruction += """

TOOL SELECTION:
- "show my orders" / "order history" → get_my_order_history() for quick summary
- "full details of my orders" / "what did I order?" → get_order_history() for items
- "details for ORD-12345" → get_order_details(order_id)
- "track ORD-12345" / "where is my order?" → track_order(order_id)"""

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

SECURITY: All tools verify that the order belongs to the authenticated user. If a user tries to access someone else's order, they will get an authorization error.

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
