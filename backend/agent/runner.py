"""
Runner — drives the Circle agent as discrete, DB-backed segments.

See agent/graph.py for why we run segments (reconstructing CircleState from the
durable app tables) instead of relying on cross-request checkpoint resume.

Each segment:
  * opens its own resources (LLM is reached inside nodes; MCP session is opened
    here when the segment needs Swiggy tools),
  * reconstructs the input CircleState from the DB,
  * runs a single graph invocation to END,
  * persists its results to the app tables (nodes do this).
"""
import logging

from langchain_mcp_adapters.client import MultiServerMCPClient

import json
import re

from agent.graph import build_parse_graph, build_discover_graph, build_order_graph, build_track_graph
from agent.state import CircleState
from config import settings
from db import crud
from db.session import SessionLocal

logger = logging.getLogger(__name__)

FOOD_URL = f"{settings.SWIGGY_MCP_BASE_URL}/food"


def _config(room_id: str, db, tools_by_name: dict | None = None) -> dict:
    return {
        "configurable": {
            "thread_id": room_id,
            "mcp_tools": tools_by_name or {},
            "db": db,
            "supabase_url": settings.SUPABASE_URL,
            "supabase_key": settings.SUPABASE_SERVICE_ROLE_KEY,
        }
    }


async def run_parse(room_id: str, host_user_id: str, address_id: str) -> None:
    """
    Segment 1 (/activate): snapshot craving cards → parse preferences.
    Only needs the LLM (reached inside parse_prefs) — no MCP session required.
    Writes pref_specs and sets room.status = 'planning'.
    """
    db = SessionLocal()
    try:
        graph = build_parse_graph()
        state = CircleState(room_id=room_id, host_user_id=host_user_id, address_id=address_id)
        await graph.ainvoke(state, _config(room_id, db))
        logger.info("run_parse complete for room %s", room_id)
    except Exception as exc:
        logger.error("run_parse failed for room %s: %s", room_id, exc, exc_info=True)
        crud.set_room_status(db, room_id, "collecting")  # let the host retry
    finally:
        db.close()


async def run_discover(room_id: str, access_token: str) -> None:
    """
    Segment 2 (/approve-prefs): discover restaurants → score feasibility.
    Reconstructs CircleState (address + approved pref_specs) from the DB and
    opens an MCP session for the Swiggy food tools.
    """
    db = SessionLocal()
    try:
        room = crud.get_room(db, room_id)
        if not room or not room.address_id:
            logger.error("run_discover: room %s missing or no address", room_id)
            return

        state = CircleState(
            room_id=room_id,
            host_user_id=room.host_user_id,
            address_id=room.address_id,
            pref_specs=crud.pref_specs_as_dicts(db, room_id),
        )

        async with MultiServerMCPClient({
            "food": {
                "url": FOOD_URL,
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {access_token}"},
            }
        }) as mcp:
            tools_by_name = {t.name: t for t in await mcp.get_tools()}
            graph = build_discover_graph()
            await graph.ainvoke(state, _config(room_id, db, tools_by_name))

        crud.set_room_status(db, room_id, "choosing")
        logger.info("run_discover complete for room %s", room_id)
    except Exception as exc:
        logger.error("run_discover failed for room %s: %s", room_id, exc, exc_info=True)
        crud.set_room_status(db, room_id, "planning")  # let the host retry approve
    finally:
        db.close()


async def run_build_cart(
    room_id: str,
    access_token: str,
    host_vpa: str | None = None,
) -> None:
    """
    Segment 3 (/choose-plan or next sub-order):
    Build cart for current_sub_order_idx → compute split → write splits to DB.
    current_sub_order_idx is derived from the number of already-placed orders.
    Sets room.status = 'confirming' on success.
    """
    db = SessionLocal()
    try:
        room = crud.get_room(db, room_id)
        if not room or not room.address_id:
            logger.error("run_build_cart: room %s missing or no address", room_id)
            return

        chosen_plan = crud.get_chosen_plan(db, room_id)
        if not chosen_plan:
            logger.error("run_build_cart: no chosen plan for room %s", room_id)
            return

        idx = len(crud.get_placed_orders(db, room_id))
        if idx >= len(chosen_plan.sub_orders):
            crud.set_room_status(db, room_id, "tracking")
            return

        plans_list = crud.plans_as_dicts(db, room_id)
        state = CircleState(
            room_id=room_id,
            host_user_id=room.host_user_id,
            address_id=room.address_id,
            plans=plans_list,
            chosen_plan_id=chosen_plan.id,
            current_sub_order_idx=idx,
        )

        async with MultiServerMCPClient({
            "food": {
                "url": FOOD_URL,
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {access_token}"},
            }
        }) as mcp:
            tools_by_name = {t.name: t for t in await mcp.get_tools()}
            cfg = _config(room_id, db, tools_by_name)
            cfg["configurable"]["host_vpa"] = host_vpa or ""
            graph = build_order_graph()
            await graph.ainvoke(state, cfg)

        crud.set_room_status(db, room_id, "confirming")
        logger.info("run_build_cart complete for room %s (sub_order_idx=%d)", room_id, idx)
    except Exception as exc:
        logger.error("run_build_cart failed for room %s: %s", room_id, exc, exc_info=True)
        crud.set_room_status(db, room_id, "ordering")
    finally:
        db.close()


def _parse_swiggy_order_id(raw) -> str | None:
    text = str(raw)
    try:
        data = json.loads(text)
        for key in ("orderId", "order_id", "id", "swiggyOrderId", "orderID"):
            if key in data and data[key]:
                return str(data[key])
    except Exception:
        pass
    m = re.search(r"(?:order[._-]?id)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_-]+)", text, re.I)
    if m:
        return m.group(1)
    return None


async def place_order_and_advance(
    room_id: str,
    access_token: str,
    host_vpa: str | None = None,
) -> None:
    """
    Places the Swiggy order for the current sub_order_idx, writes a placed_orders row,
    then either advances to the next sub_order (multi-restaurant) or sets tracking.
    """
    db = SessionLocal()
    try:
        room = crud.get_room(db, room_id)
        if not room:
            return

        chosen_plan = crud.get_chosen_plan(db, room_id)
        if not chosen_plan:
            return

        idx = len(crud.get_placed_orders(db, room_id))
        sub_orders = chosen_plan.sub_orders
        if idx >= len(sub_orders):
            crud.set_room_status(db, room_id, "tracking")
            return

        sub = sub_orders[idx]

        async with MultiServerMCPClient({
            "food": {
                "url": FOOD_URL,
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {access_token}"},
            }
        }) as mcp:
            tools_by_name = {t.name: t for t in await mcp.get_tools()}

            # Rebuild cart — it may have expired since the host confirmed
            from agent.nodes.build_cart import _build_with_retry
            await _build_with_retry(tools_by_name, sub, room.address_id)

            # Apply coupon (non-fatal)
            coupon = sub.get("coupon_code")
            if coupon and "apply_food_coupon" in tools_by_name:
                try:
                    await tools_by_name["apply_food_coupon"].ainvoke({
                        "couponCode": coupon,
                        "addressId": room.address_id,
                    })
                except Exception as exc:
                    logger.debug("apply_food_coupon (pre-place) failed: %s", exc)

            result_raw = await tools_by_name["place_food_order"].ainvoke({
                "restaurantId": sub["restaurant_id"],
                "addressId": room.address_id,
            })

        swiggy_order_id = _parse_swiggy_order_id(result_raw)
        crud.create_placed_order(
            db, room_id, chosen_plan.id, swiggy_order_id,
            sub["restaurant_id"], sub["restaurant_name"], sub,
        )
        logger.info(
            "placed order for room %s sub_order %d: swiggy_order_id=%s",
            room_id, idx, swiggy_order_id,
        )

        if idx + 1 < len(sub_orders):
            crud.set_room_status(db, room_id, "ordering")
            await run_build_cart(room_id, access_token, host_vpa)
        else:
            crud.set_room_status(db, room_id, "tracking")
            await run_track(room_id, access_token)

    except Exception as exc:
        logger.error(
            "place_order_and_advance failed for room %s: %s", room_id, exc, exc_info=True
        )
    finally:
        db.close()


async def run_track(room_id: str, access_token: str) -> None:
    """
    Segment 4: poll Swiggy for order status → update placed_orders → broadcast.
    Runs until all orders are terminal (Delivered / Cancelled).
    """
    db = SessionLocal()
    try:
        room = crud.get_room(db, room_id)
        if not room:
            return

        placed = crud.get_placed_orders(db, room_id)
        state = CircleState(
            room_id=room_id,
            host_user_id=room.host_user_id,
            address_id=room.address_id or "",
            placed_orders=[
                {"id": o.id, "swiggy_order_id": o.swiggy_order_id,
                 "restaurant_name": o.restaurant_name}
                for o in placed
            ],
        )

        async with MultiServerMCPClient({
            "food": {
                "url": FOOD_URL,
                "transport": "streamable_http",
                "headers": {"Authorization": f"Bearer {access_token}"},
            }
        }) as mcp:
            tools_by_name = {t.name: t for t in await mcp.get_tools()}
            graph = build_track_graph()
            await graph.ainvoke(state, _config(room_id, db, tools_by_name))

        logger.info("run_track complete for room %s", room_id)
    except Exception as exc:
        logger.error("run_track failed for room %s: %s", room_id, exc, exc_info=True)
        crud.set_room_status(db, room_id, "done")
    finally:
        db.close()
