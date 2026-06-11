"""Shared Swiggy-cart helpers built on the MCP structured-content response.

The MCP *text* response is a lossy human summary — it literally says
"Cart is empty" even when the cart is populated. The authoritative payload is in
the tool result's ``structuredContent``, which langchain-mcp-adapters (>=0.2)
exposes as ``msg.artifact["structured_content"]`` when the tool is invoked with a
ToolCall dict (the tool's ``response_format`` is ``"content_and_artifact"``).

Cart-item format (from update_food_cart's inputSchema):
    {"menu_item_id": "<id>", "quantity": N,
     "variantsV2": [{"group_id": "..", "variation_id": ".."}]}   # if item has variantsV2
     OR "variants": [...]                                         # if item has legacy variations
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4          # retry budget for 429 rate-limit backoff
_call_count = 0           # running tally of MCP calls (for diagnosing rate limits)


def reset_mcp_call_count() -> None:
    """Reset the running MCP call tally — call at the start of a segment."""
    global _call_count
    _call_count = 0


async def call_structured(tool, args: dict) -> dict:
    """Invoke an MCP tool and return its structuredContent dict (not the text).

    Retries with exponential backoff on 429 so a burst of calls doesn't surface
    a rate-limit error, and logs a running call tally so we can see exactly how
    many MCP calls a segment makes.
    """
    global _call_count
    tool_call = {"name": tool.name, "args": args, "id": "1", "type": "tool_call"}
    for attempt in range(_MAX_RETRIES):
        t0 = time.perf_counter()
        try:
            msg = await tool.ainvoke(tool_call)
            _call_count += 1
            logger.info("  mcp #%d %s took %.2fs", _call_count, tool.name,
                        time.perf_counter() - t0)
            artifact = getattr(msg, "artifact", None) or {}
            return artifact.get("structured_content") or {}
        except Exception as exc:
            if "429" in str(exc) and attempt < _MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning("  mcp %s hit 429 rate limit — backing off %ds (attempt %d/%d)",
                               tool.name, wait, attempt + 1, _MAX_RETRIES)
                await asyncio.sleep(wait)
                continue
            raise


def update_error(update_sc: dict) -> str | None:
    """Return a human error message if an update_food_cart result failed, else None."""
    sc = update_sc or {}
    codes = sc.get("errorCodes")
    if codes:
        return f"{sc.get('statusMessage') or 'cart update failed'} {codes}"
    return None


def _pick_variation(group: dict) -> dict | None:
    """Choose the default (or first) variation in a search_menu variant group."""
    variations = group.get("variations") or []
    if not variations:
        return None
    chosen = next((v for v in variations if v.get("default")), None) or variations[0]
    group_id = group.get("groupId") or group.get("group_id")
    variation_id = chosen.get("id") or chosen.get("variation_id")
    if not group_id or not variation_id:
        return None
    return {"group_id": str(group_id), "variation_id": str(variation_id)}


async def resolve_cart_item(tools: dict, item: dict, restaurant_id: str, address_id: str) -> dict:
    """Resolve a planned item to a cart-ready dict using search_menu structured data.

    Returns {menu_item_id, quantity, variantsV2?|variants?}. Falls back to the
    planned item_id (no variants) when search_menu can't resolve it.
    """
    quantity = item.get("quantity", 1)
    fallback = {"menu_item_id": str(item.get("item_id", "")), "quantity": quantity}
    if "search_menu" not in tools:
        return fallback

    try:
        sc = await call_structured(tools["search_menu"], {
            "query": item.get("item_name", ""),
            "restaurantIdOfAddedItem": restaurant_id,
            "addressId": address_id,
        })
    except Exception as exc:
        logger.debug("search_menu failed for %r: %s", item.get("item_name"), exc)
        return fallback

    candidates = sc.get("items") or []
    target_id = str(item.get("item_id", ""))
    target_name = (item.get("item_name") or "").strip().lower()
    match = None
    for c in candidates:
        if str(c.get("menu_item_id")) == target_id:
            match = c
            break
        if (c.get("name") or "").strip().lower() == target_name:
            match = match or c
    match = match or (candidates[0] if candidates else None)
    if not match:
        return fallback

    cart_item = {"menu_item_id": str(match["menu_item_id"]), "quantity": quantity}
    if match.get("variantsV2"):
        sel = [v for v in (_pick_variation(g) for g in match["variantsV2"]) if v]
        if sel:
            cart_item["variantsV2"] = sel
    elif match.get("variations"):
        sel = [v for v in (_pick_variation(g) for g in match["variations"]) if v]
        if sel:
            cart_item["variants"] = sel
    return cart_item


def parse_cart_pricing(cart_sc: dict) -> dict:
    """Extract totals from a get_food_cart structuredContent payload.

    Returns {subtotal, fees, taxes, total, discount, coupon_code, item_count, empty}.
    ``total`` is the real server-computed ``to_pay`` (item total + taxes - discounts + delivery).
    ``discount`` is only non-zero when a coupon is actually applied (coupon_discount > 0).
    """
    data = (cart_sc or {}).get("data") or {}
    pricing = data.get("pricing") or {}
    offers = data.get("offers") or {}

    def _i(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    discount = _i(offers.get("coupon_discount")) or 0
    coupon_code = offers.get("coupon_applied") if discount > 0 else None

    return {
        "subtotal": _i(pricing.get("item_total")),
        "fees": _i(pricing.get("delivery_charge")) or 0,
        "taxes": _i(pricing.get("taxes_and_charges")) or 0,
        "total": _i(pricing.get("to_pay")),
        "discount": discount,
        "coupon_code": coupon_code,
        "item_count": data.get("item_count"),
        "cart_id": data.get("cart_id"),
        "empty": not data.get("items"),
    }


async def apply_cart_coupon(tools: dict, cart_sc: dict, address_id: str) -> dict:
    """If the cart has an auto-suggested coupon, apply it and return updated pricing.

    Swiggy auto-suggests a coupon in ``data.offers.coupon_applied`` with
    ``coupon_discount=0``. We apply it and re-read the cart — if the discount
    becomes > 0 the total drops. If the coupon is rejected or does nothing we
    return the original pricing unchanged.
    """
    data = (cart_sc or {}).get("data") or {}
    suggested = (data.get("offers") or {}).get("coupon_applied")
    if not suggested or "apply_food_coupon" not in tools:
        return parse_cart_pricing(cart_sc)

    try:
        await tools["apply_food_coupon"].ainvoke({
            "couponCode": suggested,
            "addressId": address_id,
        })
        updated_sc = await call_structured(tools["get_food_cart"], {"addressId": address_id})
        updated_discount = (updated_sc.get("data") or {}).get("offers", {}).get("coupon_discount") or 0
        if updated_discount > 0:
            logger.info("apply_cart_coupon: %s saved ₹%d", suggested, updated_discount)
            return parse_cart_pricing(updated_sc)
    except Exception as exc:
        logger.debug("apply_cart_coupon failed (non-fatal): %s", exc)

    return parse_cart_pricing(cart_sc)
