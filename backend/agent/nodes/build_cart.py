import json
import logging
import re

from langgraph.types import RunnableConfig

from agent.state import CircleState
from db import crud

logger = logging.getLogger(__name__)


def _cart_item_format(item: dict) -> dict:
    """Items for update_food_cart — variant items MUST include variantGroups."""
    d = {"itemId": item["item_id"], "quantity": 1}
    if item.get("variant"):
        d["variantGroups"] = [item["variant"]]
    if item.get("addons"):
        d["addons"] = item["addons"]
    return d


def _parse_cart(raw) -> dict:
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw)
        if "CART_EXPIRED" in text:
            return {"expired": True}
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            out: dict = {}
            for key in ("total", "subtotal", "discount"):
                m = re.search(rf"{key}[\"']?\s*[:=]\s*₹?\s*(\d+)", text, re.I)
                if m:
                    out[key] = int(m.group(1))
            fee = re.search(r"(?:delivery\s*fee|fees)[\"']?\s*[:=]\s*₹?\s*(\d+)", text, re.I)
            if fee:
                out["fees"] = int(fee.group(1))
            return out

    def _g(*keys):
        for k in keys:
            if k in data and data[k] is not None:
                try:
                    return int(data[k])
                except (TypeError, ValueError):
                    pass
        return None

    return {
        "total": _g("total", "grandTotal", "finalAmount", "payableAmount"),
        "subtotal": _g("subtotal", "itemTotal"),
        "fees": _g("fees", "deliveryFee", "deliveryCharge") or 0,
        "discount": _g("discount", "couponDiscount") or 0,
    }


async def _build_with_retry(tools: dict, sub: dict, address_id: str, retries: int = 2) -> dict:
    """Flush → update → get_food_cart, retrying on CART_EXPIRED."""
    cart_items = [_cart_item_format(i) for i in sub.get("items", [])]

    for attempt in range(retries):
        try:
            await tools["flush_food_cart"].ainvoke({"addressId": address_id})
        except Exception as exc:
            logger.debug("flush_food_cart (attempt %d): %s", attempt, exc)

        await tools["update_food_cart"].ainvoke({
            "restaurantId": sub["restaurant_id"],
            "addressId": address_id,
            "cartItems": cart_items,
            "restaurantName": sub.get("restaurant_name", ""),
        })

        cart_raw = await tools["get_food_cart"].ainvoke({"addressId": address_id})
        parsed = _parse_cart(cart_raw)

        if parsed.get("expired"):
            logger.warning("CART_EXPIRED on attempt %d — retrying", attempt)
            continue

        return parsed

    raise RuntimeError(f"Cart build failed after {retries} retries (CART_EXPIRED)")


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
    total = parsed.get("total")
    if total is None:
        total = (subtotal or 0) + fees - discount

    if total >= 1000:
        logger.warning(
            "build_cart: sub_order total %d >= 1000 for room %s (idx=%d) — host will see warning",
            total, state.room_id, idx,
        )

    # Apply coupon if available (non-fatal)
    coupon = sub.get("coupon_code")
    if coupon and "apply_food_coupon" in tools:
        try:
            await tools["apply_food_coupon"].ainvoke({
                "couponCode": coupon,
                "addressId": state.address_id,
            })
            logger.info("build_cart: applied coupon %s", coupon)
        except Exception as exc:
            logger.debug("apply_food_coupon failed (non-fatal): %s", exc)

    updated_sub = {**sub, "subtotal": subtotal, "fees": fees,
                   "discount": discount, "total": total}
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
