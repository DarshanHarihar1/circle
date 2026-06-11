import logging
import time

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


async def _price_sub_order(tools: dict, sub: dict, address_id: str) -> dict:
    """Build the cart for one sub-order, read its real total, then flush."""
    rid = sub["restaurant_id"]
    cart_items = [
        await resolve_cart_item(tools, i, rid, address_id) for i in sub["items"]
    ]

    update_sc = await call_structured(tools["update_food_cart"], {
        "restaurantId": rid,
        "addressId": address_id,
        "cartItems": cart_items,
        "restaurantName": sub.get("restaurant_name", ""),
    })
    err = update_error(update_sc)
    if err:
        logger.warning("price_plans: update_food_cart failed for %s: %s",
                       sub.get("restaurant_name"), err)

    cart_sc = await call_structured(tools["get_food_cart"], {"addressId": address_id})
    parsed = await apply_cart_coupon(tools, cart_sc, address_id)

    try:
        await tools["flush_food_cart"].ainvoke({"addressId": address_id})
    except Exception as exc:
        logger.debug("flush_food_cart failed (non-fatal): %s", exc)

    subtotal = parsed.get("subtotal") if parsed.get("subtotal") is not None else sub["subtotal"]
    fees = parsed.get("fees", 0) or 0
    discount = parsed.get("discount") or 0
    total = parsed.get("total")
    if total is None:
        total = subtotal + fees - discount

    return {**sub, "subtotal": subtotal, "fees": fees,
            "discount": discount, "total": total, "coupon_code": parsed.get("coupon_code")}


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools = config["configurable"]["mcp_tools"]
    db = config["configurable"]["db"]

    if not tools or "update_food_cart" not in tools:
        logger.info("price_plans: cart tools unavailable — keeping estimated totals")
        return {}

    n_subs = sum(len(p["sub_orders"]) for p in state.plans)
    logger.info("price_plans: START room %s — pricing %d plans (%d sub-orders)",
                state.room_id, len(state.plans), n_subs)
    t_start = time.perf_counter()

    priced_plans = []
    for plan in state.plans:
        new_subs = []
        plan_total = 0
        for sub in plan["sub_orders"]:
            t_sub = time.perf_counter()
            try:
                psub = await _price_sub_order(tools, sub, state.address_id)
            except Exception as exc:
                logger.warning("pricing failed for %s: %s — keeping estimate",
                               sub.get("restaurant_name"), exc)
                psub = sub
            logger.info("  priced sub-order '%s' in %.2fs (total=%s)",
                        sub.get("restaurant_name"), time.perf_counter() - t_sub,
                        psub.get("total"))
            new_subs.append(psub)
            plan_total += psub["total"]
        priced = {**plan, "sub_orders": new_subs, "total": plan_total}
        priced_plans.append(priced)
        crud.update_plan_pricing(db, priced["plan_id"], plan_total, new_subs)

    logger.info("price_plans: DONE — priced %d plans for room %s in %.2fs",
                len(priced_plans), state.room_id, time.perf_counter() - t_start)
    return {"plans": priced_plans}
