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
    """
    Pull {total, subtotal, fees, discount} out of a get_food_cart response.
    Defensive across JSON and text shapes.
    """
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # text fallback — find "total: 836" style numbers
            out = {}
            for key in ("total", "subtotal", "discount"):
                m = re.search(rf"{key}[\"']?\s*[:=]\s*₹?\s*(\d+)", text, re.I)
                if m:
                    out[key] = int(m.group(1))
            fee = re.search(r"(?:delivery\s*fee|fees)[\"']?\s*[:=]\s*₹?\s*(\d+)", text, re.I)
            if fee:
                out["fees"] = int(fee.group(1))
            return out
        if "CART_EXPIRED" in text:
            return {"expired": True}

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


def _parse_coupons(raw) -> str | None:
    """Return the first COD-eligible coupon code, if any."""
    try:
        data = raw if isinstance(raw, (list, dict)) else json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return None
    coupons = data.get("coupons", data) if isinstance(data, dict) else data
    if not isinstance(coupons, list):
        return None
    for c in coupons:
        if not isinstance(c, dict):
            continue
        cod_ok = c.get("codEligible", c.get("cod_eligible", True))
        code = c.get("code") or c.get("couponCode")
        if code and cod_ok:
            return code
    return None


async def _price_sub_order(tools: dict, sub: dict, address_id: str) -> dict:
    """Build the cart for one sub-order, read its real total, then flush."""
    rid = sub["restaurant_id"]
    cart_items = [_cart_item_format(i) for i in sub["items"]]

    await tools["update_food_cart"].ainvoke({
        "restaurantId": rid,
        "addressId": address_id,
        "cartItems": cart_items,
        "restaurantName": sub.get("restaurant_name", ""),
    })
    cart_raw = await tools["get_food_cart"].ainvoke({"addressId": address_id})
    parsed = _parse_cart(cart_raw)

    coupon = None
    try:
        coup_raw = await tools["fetch_food_coupons"].ainvoke({"addressId": address_id})
        coupon = _parse_coupons(coup_raw)
    except Exception as exc:
        logger.debug("fetch_food_coupons failed (non-fatal): %s", exc)

    try:
        await tools["flush_food_cart"].ainvoke({"addressId": address_id})
    except Exception as exc:
        logger.debug("flush_food_cart failed (non-fatal): %s", exc)

    subtotal = parsed.get("subtotal") if parsed.get("subtotal") is not None else sub["subtotal"]
    fees = parsed.get("fees", 0) or 0
    discount = parsed.get("discount", 0) or 0
    total = parsed.get("total")
    if total is None:
        total = subtotal + fees - discount

    return {**sub, "subtotal": subtotal, "fees": fees,
            "discount": discount, "total": total, "coupon_code": coupon}


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools = config["configurable"]["mcp_tools"]
    db = config["configurable"]["db"]

    if not tools or "update_food_cart" not in tools:
        logger.info("price_plans: cart tools unavailable — keeping estimated totals")
        return {}

    priced_plans = []
    for plan in state.plans:
        new_subs = []
        plan_total = 0
        for sub in plan["sub_orders"]:
            try:
                psub = await _price_sub_order(tools, sub, state.address_id)
            except Exception as exc:
                logger.warning("pricing failed for %s: %s — keeping estimate",
                               sub.get("restaurant_name"), exc)
                psub = sub
            new_subs.append(psub)
            plan_total += psub["total"]
        priced = {**plan, "sub_orders": new_subs, "total": plan_total}
        priced_plans.append(priced)
        crud.update_plan_pricing(db, priced["plan_id"], plan_total, new_subs)

    logger.info("price_plans: priced %d plans for room %s", len(priced_plans), state.room_id)
    return {"plans": priced_plans}
