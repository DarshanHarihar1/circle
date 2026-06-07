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

from agent.graph import build_parse_graph, build_discover_graph
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
