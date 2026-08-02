"""
Product-related tools for the customer support system.

This module contains all tools for product search, details, inventory, and reviews.
"""

import logging

from google.adk.tools.tool_context import ToolContext

from customer_support_mas.error_handling import tool_error_handler
from customer_support_mas.providers.registry import get_provider
from customer_support_mas.tenancy.context import get_tenant_id
from customer_support_mas.validation import (
    validate_product_id,
    validate_search_query,
    validation_error_response,
)

logger = logging.getLogger(__name__)


@tool_error_handler
def search_products(query: str, tool_context: ToolContext) -> dict:
    """Search for products using RAG (semantic) or keyword fallback.

    Automatically saves the first result to session state for follow-up questions.

    Args:
        query: Search query string
        tool_context: ADK ToolContext (automatically injected)
    """
    is_valid, error_msg = validate_search_query(query)
    if not is_valid:
        return validation_error_response(error_msg)

    tenant_id = get_tenant_id(tool_context)
    provider = get_provider(tenant_id)
    products = provider.search_products(tenant_id, query, limit=5)

    if not products:
        return {"status": "no_results", "message": f"No products found matching '{query}'"}

    results = [p.as_response_dict() for p in products]

    tool_context.state["last_product_id"] = results[0]["id"]
    tool_context.state["last_product_name"] = results[0]["name"]
    tool_context.state["last_search_query"] = query
    logger.debug("Session state saved: %s - %s", results[0]["id"], results[0]["name"])

    product_ids = [p["id"] for p in results]
    tool_context.state["products_to_detail"] = product_ids
    tool_context.state["detailed_product_ids"] = []
    logger.debug("Saved %d product IDs for multi-detail: %s", len(product_ids), product_ids)

    return {"status": "success", "products": results, "count": len(results)}


@tool_error_handler
def get_product_details(product_id: str, tool_context: ToolContext) -> dict:
    """Get detailed information about a specific product by its ID.

    Args:
        product_id: The product ID (e.g., "PROD-001")
        tool_context: ADK ToolContext (automatically injected)
    """
    is_valid, error_msg = validate_product_id(product_id)
    if not is_valid:
        return validation_error_response(error_msg)

    tenant_id = get_tenant_id(tool_context)
    provider = get_provider(tenant_id)
    product = provider.get_product(tenant_id, product_id)

    if product is None:
        return {"status": "not_found", "message": f"Product {product_id} not found"}

    return {"status": "success", "product": product.as_response_dict()}


@tool_error_handler
def get_last_mentioned_product(tool_context: ToolContext) -> dict:
    """IMPORTANT: Use this tool when customer asks for details about a product you just showed them.

    Triggers: "yes", "yes please", "sure", "ok", "tell me more", "details", "get details",
              "more info", "this one", "that one", "show me details", "I want details"

    This tool requires NO parameters - it automatically retrieves the last product from session state.
    DO NOT ask "which product?" - just call this tool directly!

    Args:
        tool_context: ADK ToolContext (automatically injected)
    """
    # Read from persistent session state (safe with default)
    last_product_id = tool_context.state.get("last_product_id")
    last_product_name = tool_context.state.get("last_product_name", "Unknown")

    logger.debug("get_last_mentioned_product: product_id=%s, product_name=%s", last_product_id, last_product_name)

    if not last_product_id:
        return {"status": "error", "message": "No product was recently discussed. Please search for a product first."}

    tenant_id = get_tenant_id(tool_context)
    provider = get_provider(tenant_id)
    product = provider.get_product(tenant_id, last_product_id)

    if product is None:
        return {"status": "not_found", "message": f"Product {last_product_id} not found"}

    return {
        "status": "success",
        "product": product.as_response_dict(),
        "context_note": f"This is the {last_product_name} you asked about.",
    }


@tool_error_handler
def check_inventory(product_id: str, tool_context: ToolContext) -> dict:
    """Check inventory levels.

    Args:
        product_id: The product ID to check inventory for
        tool_context: ADK ToolContext (automatically injected)
    """
    is_valid, error_msg = validate_product_id(product_id)
    if not is_valid:
        return validation_error_response(error_msg)

    tenant_id = get_tenant_id(tool_context)
    provider = get_provider(tenant_id)
    inventory = provider.get_inventory(tenant_id, product_id)

    if inventory is None:
        return {"status": "not_found", "message": f"No inventory record for product {product_id}"}

    # as_response_dict() keeps the per-warehouse breakdown, which the agent
    # quotes to customers ("45 units across our warehouses: ...") — an
    # earlier version returned only a bare quantity and silently dropped it.
    return {"status": "success", "inventory": inventory.as_response_dict()}


@tool_error_handler
def get_product_reviews(product_id: str, tool_context: ToolContext) -> dict:
    """Get customer reviews for a product.

    Args:
        product_id: The product ID to get reviews for
        tool_context: ADK ToolContext (automatically injected)
    """
    is_valid, error_msg = validate_product_id(product_id)
    if not is_valid:
        return validation_error_response(error_msg)

    tenant_id = get_tenant_id(tool_context)
    provider = get_provider(tenant_id)
    reviews = provider.get_reviews_for_product(tenant_id, product_id)

    if not reviews:
        return {"status": "not_found"}

    return {"status": "success", "reviews": {"product_id": product_id, **reviews[0]}}


@tool_error_handler
def get_all_saved_products_info(tool_context: ToolContext) -> dict:
    """
    Get comprehensive information for ALL products from the last search.

    This tool retrieves all product IDs saved in session state and fetches
    comprehensive information (details + inventory + reviews) for each.

    **Use this tool when:**
    - User asks for "details on all of them", "all three", "both", "show me all"
    - User wants information about multiple products from the previous search

    This is MORE EFFICIENT than using LoopAgent because it fetches directly
    without iteration overhead and timeout issues.

    Args:
        tool_context: ADK ToolContext (automatically injected)

    Returns:
        Dictionary with comprehensive info for all saved products
    """
    products_to_detail = tool_context.state.get("products_to_detail", [])

    if not products_to_detail:
        return {"status": "error", "message": "No products were recently searched. Please search for products first."}

    logger.debug("Fetching info for %d products: %s", len(products_to_detail), products_to_detail)

    results = {"status": "success", "count": len(products_to_detail), "products": []}

    # Fetch comprehensive info for each product
    for product_id in products_to_detail:
        product_info = get_product_info(product_id, tool_context)
        if product_info.get("status") == "success":
            results["products"].append(product_info)
        else:
            results["products"].append(
                {"product_id": product_id, "status": "not_found", "message": f"Product {product_id} not found"}
            )

    logger.debug("Successfully fetched %d products", len(results["products"]))

    return results


@tool_error_handler
def get_product_info(
    product_id: str,
    tool_context: ToolContext,
    include_details: bool = True,
    include_inventory: bool = True,
    include_reviews: bool = True,
) -> dict:
    """
    Smart unified product information fetcher with automatic comprehensive data retrieval.

    **DEFAULT BEHAVIOR**: Fetches ALL information (details + inventory + reviews) for complete product info.
    This is the RECOMMENDED tool for most product queries as it provides comprehensive information efficiently.

    **Use this tool when:**
    - User asks for product information (any details about a product)
    - User mentions "full details", "everything", "complete info"
    - User explicitly asks for inventory, reviews, or stock levels
    - User wants comprehensive product data

    **Only use individual tools (get_product_details, check_inventory, get_product_reviews) when:**
    - User explicitly says "ONLY details" or "JUST the basic info"
    - User specifically requests a single piece of information

    Args:
        product_id: The product ID (e.g., "PROD-001")
        tool_context: ADK ToolContext (automatically injected)
        include_details: Whether to fetch product details (default: True)
        include_inventory: Whether to fetch inventory levels (default: True)
        include_reviews: Whether to fetch customer reviews (default: True)

    Returns:
        Comprehensive product information with all requested data
    """
    logger.debug(
        "get_product_info called for %s (details=%s, inventory=%s, reviews=%s)",
        product_id,
        include_details,
        include_inventory,
        include_reviews,
    )

    result = {"status": "success", "product_id": product_id, "data_fetched": [], "fetch_method": "comprehensive"}

    # Fetch details
    if include_details:
        details = get_product_details(product_id, tool_context)
        if details.get("status") == "success":
            result["details"] = details.get("product", {})
            result["data_fetched"].append("details")
        else:
            result["details_error"] = "Product not found"

    # Fetch inventory
    if include_inventory:
        inventory = check_inventory(product_id, tool_context)
        if inventory.get("status") == "success":
            result["inventory"] = inventory.get("inventory", {})
            result["data_fetched"].append("inventory")
        else:
            result["inventory_error"] = "Inventory not found"

    # Fetch reviews
    if include_reviews:
        reviews = get_product_reviews(product_id, tool_context)
        if reviews.get("status") == "success":
            result["reviews"] = reviews.get("reviews", {})
            result["data_fetched"].append("reviews")
        else:
            result["reviews_error"] = "Reviews not found"

    # Update status if nothing was found
    if not result["data_fetched"]:
        result["status"] = "not_found"
        result["message"] = f"No information found for product {product_id}"

    logger.debug("Successfully fetched: %s", ", ".join(result["data_fetched"]))

    return result
