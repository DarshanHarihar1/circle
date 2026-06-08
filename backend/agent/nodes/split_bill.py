import logging
import urllib.parse

from langgraph.types import RunnableConfig

from agent.state import CircleState
from db import crud

logger = logging.getLogger(__name__)


def compute_split(sub: dict) -> list[dict]:
    """
    Each participant pays their proportional share of the cart total.
    Formula: amount_i = round(item_cost_i / items_total * cart_total)
    Last participant absorbs any rounding remainder so amounts sum exactly to cart_total.
    """
    totals: dict[str, int] = {}
    for item in sub.get("items", []):
        pid = item["participant_id"]
        totals[pid] = totals.get(pid, 0) + item["price"]

    if not totals:
        return []

    items_total = sum(totals.values())
    cart_total = sub.get("total") or items_total

    splits = []
    remaining = cart_total
    pids = list(totals.keys())
    for i, pid in enumerate(pids):
        if i == len(pids) - 1:
            splits.append({"participant_id": pid, "amount": remaining})
        else:
            amount = round((totals[pid] / items_total) * cart_total) if items_total else 0
            splits.append({"participant_id": pid, "amount": amount})
            remaining -= amount

    return splits


def make_upi_link(host_vpa: str, amount: int, room_id: str) -> str:
    note = urllib.parse.quote(f"Circle {room_id[:6]}", safe="")
    return f"upi://pay?pa={host_vpa}&am={amount}&tn={note}&cu=INR"


async def run(state: CircleState, config: RunnableConfig) -> dict:
    db = config["configurable"]["db"]
    host_vpa: str = config["configurable"].get("host_vpa") or ""

    plan_dict = next(
        (p for p in state.plans if p["plan_id"] == state.chosen_plan_id), None
    )
    if not plan_dict:
        return {}

    sub_orders = plan_dict["sub_orders"]
    idx = state.current_sub_order_idx
    if idx >= len(sub_orders):
        return {}

    sub = sub_orders[idx]
    splits = compute_split(sub)

    for s in splits:
        upi = make_upi_link(host_vpa, s["amount"], state.room_id) if host_vpa else None
        crud.upsert_split(db, state.room_id, s["participant_id"], s["amount"], upi)

    logger.info(
        "split_bill: %d splits written for room %s sub_order %d (total=%d)",
        len(splits), state.room_id, idx, sum(s["amount"] for s in splits),
    )
    return {"split": splits}
