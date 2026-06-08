"""
Tracking node — polls Swiggy for order status every POLL_INTERVAL_S seconds,
updates placed_orders in the DB (triggers Supabase Realtime to all tabs), and
broadcasts a supplementary order:tracking event with ETA data.

Runs until all placed orders reach a terminal state (Delivered / Cancelled).
On 401 from MCP: broadcasts auth:expired and stops cleanly.
On unexpected errors: calls report_error (if the tool is available) and logs.
"""
import asyncio
import json
import logging
import re

import httpx
from langgraph.types import RunnableConfig

from agent.state import CircleState
from db import crud

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 10
TERMINAL_STATUSES = frozenset({"delivered", "cancelled"})


def _parse_tracking(raw) -> dict:
    """
    Extract {status, eta_mins, order_id} from a track_food_order /
    get_food_orders response.  Defensive across JSON and plain-text shapes.
    """
    text = str(raw)
    try:
        data = json.loads(text)
    except Exception:
        data = {}

    # get_food_orders returns a list; take the first item
    if isinstance(data, list):
        data = data[0] if data else {}

    status = (
        data.get("orderStatus")
        or data.get("order_status")
        or data.get("status")
        or data.get("tracking_status")
        or ""
    )

    eta_raw = (
        data.get("etaMins")
        or data.get("eta_mins")
        or data.get("eta")
        or data.get("estimatedDeliveryTime")
    )
    eta_mins: int | None = None
    if eta_raw is not None:
        try:
            eta_mins = int(re.sub(r"[^\d]", "", str(eta_raw)))
        except (ValueError, TypeError):
            pass

    order_id = (
        data.get("orderId")
        or data.get("order_id")
        or data.get("swiggyOrderId")
    )

    return {
        "status": str(status).strip(),
        "eta_mins": eta_mins,
        "order_id": str(order_id) if order_id else None,
    }


async def _broadcast(
    supabase_url: str,
    supabase_key: str,
    room_id: str,
    payload: dict,
) -> None:
    """
    Send a supplementary Supabase channel broadcast.
    Non-fatal if the endpoint is unreachable — the DB update is the primary
    mechanism for Realtime delivery.
    """
    if not supabase_url or not supabase_key:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{supabase_url}/realtime/v1/api/broadcast",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "messages": [
                        {
                            "topic": f"realtime:room:{room_id}",
                            "event": payload.get("event", "order:tracking"),
                            "payload": payload,
                        }
                    ]
                },
            )
    except Exception as exc:
        logger.debug("_broadcast failed (non-fatal): %s", exc)


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools: dict = config["configurable"]["mcp_tools"]
    db = config["configurable"]["db"]
    supabase_url: str = config["configurable"].get("supabase_url", "")
    supabase_key: str = config["configurable"].get("supabase_key", "")

    if not tools or "track_food_order" not in tools:
        logger.info("track: tracking tools unavailable — setting room to done")
        crud.set_room_status(db, state.room_id, "done")
        return {}

    placed = crud.get_placed_orders(db, state.room_id)
    active = [o for o in placed if o.swiggy_order_id]

    if not active:
        crud.set_room_status(db, state.room_id, "done")
        return {}

    terminal_ids: set[str] = set()

    while len(terminal_ids) < len(active):
        for order in active:
            if order.id in terminal_ids:
                continue

            try:
                raw = await tools["track_food_order"].ainvoke(
                    {"orderId": order.swiggy_order_id}
                )
                info = _parse_tracking(raw)
                status = info["status"]
                eta_mins = info["eta_mins"]

                # Persist status so Supabase Realtime notifies all tabs
                crud.update_placed_order_status(db, order.id, status, eta_mins)

                # Supplementary broadcast (ETA + consolidated payload)
                await _broadcast(supabase_url, supabase_key, state.room_id, {
                    "event": "order:tracking",
                    "swiggy_order_id": order.swiggy_order_id,
                    "restaurant_name": order.restaurant_name,
                    "status": status,
                    "eta_mins": eta_mins,
                    "order_id": order.id,
                })

                logger.info(
                    "track room=%s order=%s status=%s eta=%s",
                    state.room_id, order.swiggy_order_id, status, eta_mins,
                )

                if status.lower() in TERMINAL_STATUSES:
                    terminal_ids.add(order.id)
                    logger.info(
                        "track: order %s reached terminal status %s",
                        order.swiggy_order_id, status,
                    )

            except Exception as exc:
                exc_str = str(exc).lower()

                # 401 → the host's token expired; signal re-auth and stop
                if "401" in exc_str or "unauthorized" in exc_str:
                    logger.warning(
                        "track: 401 from Swiggy for room %s — broadcasting auth:expired",
                        state.room_id,
                    )
                    await _broadcast(supabase_url, supabase_key, state.room_id, {
                        "event": "auth:expired",
                        "message": "Host must reconnect Swiggy",
                    })
                    crud.set_room_status(db, state.room_id, "done")
                    return {}

                # Unexpected error — report and continue (don't crash the loop)
                if "report_error" in tools:
                    try:
                        await tools["report_error"].ainvoke({
                            "error": str(exc),
                            "context": (
                                f"track room={state.room_id} "
                                f"order={order.swiggy_order_id}"
                            ),
                        })
                    except Exception:
                        pass

                logger.error(
                    "track: poll error for order %s: %s",
                    order.swiggy_order_id, exc,
                )

        if len(terminal_ids) < len(active):
            await asyncio.sleep(POLL_INTERVAL_S)

    crud.set_room_status(db, state.room_id, "done")
    logger.info("track: all orders terminal for room %s", state.room_id)
    return {}
