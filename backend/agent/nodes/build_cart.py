import logging

from langgraph.types import RunnableConfig

from agent.state import CircleState
from agent.nodes.cart_mcp import (
    apply_cart_coupon,
    call_structured,
    resolve_cart_item,
    update_error,
)
from db import crud

logger = logging.getLogger(__name__)


async def _build_with_retry(tools: dict, sub: dict, address_id: str, retries: int = 2) -> dict:
    """Flush → update → get_food_cart, retrying if the cart update is rejected."""
    cart_items = [
        await resolve_cart_item(tools, i, sub["restaurant_id"], address_id)
        for i in sub.get("items", [])
    ]

    last_err = None
    for attempt in range(retries):
        try:
            await tools["flush_food_cart"].ainvoke({"addressId": address_id})
        except Exception as exc:
            logger.debug("flush_food_cart (attempt %d): %s", attempt, exc)

        update_sc = await call_structured(tools["update_food_cart"], {
            "restaurantId": sub["restaurant_id"],
            "addressId": address_id,
            "cartItems": cart_items,
            "restaurantName": sub.get("restaurant_name", ""),
        })
        last_err = update_error(update_sc)
        if last_err:
            logger.warning("update_food_cart failed (attempt %d): %s", attempt, last_err)
            continue

        cart_sc = await call_structured(tools["get_food_cart"], {"addressId": address_id})
        return await apply_cart_coupon(tools, cart_sc, address_id)

    raise RuntimeError(f"Cart build failed after {retries} retries: {last_err}")


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools = config["configurable"]["mcp_tools"]
    db = config["configurable"]["db"]

    if not tools or "update_food_cart" not in tools:
        logger.info("build_cart: cart tools unavailable — skipping")
        return {}

    plan_dict = next(
        (p for p in state.plans if p["plan_id"] == state.chosen_plan_id), None
    )
    if not plan_dict:
        logger.warning("build_cart: no chosen plan in state for room %s", state.room_id)
        return {}

    sub_orders = plan_dict["sub_orders"]
    idx = state.current_sub_order_idx
    if idx >= len(sub_orders):
        return {}

    sub = sub_orders[idx]

    try:
        parsed = await _build_with_retry(tools, sub, state.address_id)
    except RuntimeError as exc:
        logger.error("build_cart: %s", exc)
        return {}

    subtotal = parsed.get("subtotal") if parsed.get("subtotal") is not None else sub.get("subtotal", 0)
    fees = parsed.get("fees") or 0
    discount = parsed.get("discount") or 0
    coupon_code = parsed.get("coupon_code") or sub.get("coupon_code")
    total = parsed.get("total")
    if total is None:
        total = (subtotal or 0) + fees - discount

    if total >= 1000:
        logger.warning(
            "build_cart: sub_order total %d >= 1000 for room %s (idx=%d) — host will see warning",
            total, state.room_id, idx,
        )

    updated_sub = {**sub, "subtotal": subtotal, "fees": fees,
                   "discount": discount, "coupon_code": coupon_code, "total": total}
    new_subs = list(sub_orders)
    new_subs[idx] = updated_sub

    plan_total = sum(s.get("total", 0) for s in new_subs)
    new_plan = {**plan_dict, "sub_orders": new_subs, "total": plan_total}
    new_plans = [
        new_plan if p["plan_id"] == state.chosen_plan_id else p
        for p in state.plans
    ]

    crud.update_plan_pricing(db, plan_dict["plan_id"], plan_total, new_subs)
    logger.info(
        "build_cart: cart ready for sub_order %d in room %s (total=%d)",
        idx, state.room_id, total,
    )
    return {"plans": new_plans}
