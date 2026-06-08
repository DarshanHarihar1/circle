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

    # A participant can have items in more than one sub-order of a multi-restaurant
    # plan. Each sub-order's split is computed independently, then summed per
    # participant so the persisted split reflects everything they owe across the
    # whole plan — not just the most recently built cart. Accumulating across all
    # priced sub-orders [0..idx] (rather than adding to the stored row) keeps this
    # idempotent under retries: re-running any idx recomputes the same totals.
    per_participant: dict[str, int] = {}
    for j in range(idx + 1):
        for s in compute_split(sub_orders[j]):
            per_participant[s["participant_id"]] = (
                per_participant.get(s["participant_id"], 0) + s["amount"]
            )

    for pid, amount in per_participant.items():
        upi = make_upi_link(host_vpa, amount, state.room_id) if host_vpa else None
        crud.upsert_split(db, state.room_id, pid, amount, upi)

    splits = [{"participant_id": pid, "amount": amt} for pid, amt in per_participant.items()]
    logger.info(
        "split_bill: %d splits written for room %s through sub_order %d (total=%d)",
        len(splits), state.room_id, idx, sum(per_participant.values()),
    )
    return {"split": splits}
