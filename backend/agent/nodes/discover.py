import logging
import re
import time
from langgraph.types import RunnableConfig
from agent.state import CircleState
from agent.nodes.cart_mcp import call_structured
from db import crud

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 12


def _cost_int(val) -> int | None:
    """costForTwo arrives as a string like '₹400 for two' — extract the number."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    m = re.search(r"\d+", str(val))
    return int(m.group()) if m else None


def _clean_name(name: str | None) -> str:
    """Swiggy appends a sponsored tag like ' (Ad)' to restaurant names — drop it."""
    if not name:
        return "Unknown"
    return re.sub(r"\s*\(Ad\)\s*$", "", name).strip() or "Unknown"


_QUERY_STOPWORDS = {"with", "some", "something", "food", "style"}


def _search_queries(pref_specs: list[dict]) -> list[str]:
    """Turn soft-preference tags into Swiggy restaurant-search queries.

    A free-text cuisine vibe ("pizza bakery classic stuffed garlic bread") is a
    single soft tag, but Swiggy's search returns *dish* entries (which have no
    menu) for long, dish-specific phrases — only short cuisine-ish queries
    return real restaurants. So we query each tag as the user phrased it AND its
    individual words; discover's restaurant filter drops the dish-only
    responses, leaving whichever queries surfaced real restaurants.
    """
    seen: set[str] = set()
    queries: list[str] = []

    def add(q: str) -> None:
        q = q.strip()
        key = q.lower()
        if q and key not in seen:
            seen.add(key)
            queries.append(q)

    for spec in pref_specs:
        for term in spec.get("soft", []):
            term = (term or "").strip()
            if not term:
                continue
            add(term)
            words = term.split()
            if len(words) > 1:
                for w in words:
                    if len(w) >= 4 and w.lower() not in _QUERY_STOPWORDS:
                        add(w)

    return queries or ["restaurant"]


def _is_real_restaurant(r: dict) -> bool:
    """Distinguish a real restaurant from a dish-search entry.

    Dish entries come back as bare {id, name, cuisines:[]} and get_restaurant_menu
    returns no items for their id. Real restaurants carry rating/cost/area/status
    metadata. Requiring one of those keys filters the dish entries out.
    """
    return bool(
        r.get("avgRating")
        or r.get("costForTwo")
        or r.get("areaName")
        or r.get("availabilityStatus")
    )


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools = config["configurable"]["mcp_tools"]
    db = config["configurable"]["db"]

    # Build search queries from all participants' soft prefs (expanding free-text
    # phrases into cuisine words). pref_specs are plain dicts (PrefSpec.model_dump()).
    queries = _search_queries(state.pref_specs)

    # Cap number of search calls to avoid rate limits
    queries = queries[:MAX_CANDIDATES]
    logger.info("discover: START room %s — %d search quer(ies): %s",
                state.room_id, len(queries), queries)
    t_start = time.perf_counter()

    seen_ids: set[str] = set()
    candidates: list[dict] = []

    for query in queries:
        if len(candidates) >= MAX_CANDIDATES:
            break
        try:
            sc = await call_structured(tools["search_restaurants"], {
                "addressId": state.address_id,
                "query": query,
            })
            restaurants = sc.get("restaurants") or []
            for r in restaurants:
                rid = r.get("id") or r.get("restaurantId") or r.get("restaurant_id", "")
                if not rid or rid in seen_ids:
                    continue
                if not _is_real_restaurant(r):
                    continue  # dish-search entry — has no menu, skip
                avail = (
                    r.get("availabilityStatus")
                    or r.get("availability", "OPEN")
                ).upper()
                if avail != "OPEN":
                    continue
                seen_ids.add(rid)
                candidates.append({
                    "id": rid,
                    "name": _clean_name(r.get("name") or r.get("restaurant_name") or r.get("restaurantName")),
                    "cuisines": r.get("cuisines") or r.get("cuisine") or [],
                    "rating": r.get("avgRating") or r.get("rating"),
                    "cost_for_two": _cost_int(r.get("costForTwo") or r.get("cost_for_two")),
                    "distance_km": r.get("distanceKm") or r.get("distance") or r.get("distance_km"),
                    "availability": "OPEN",
                    "metadata": r,
                })
                if len(candidates) >= MAX_CANDIDATES:
                    break
        except Exception as exc:
            logger.warning("search_restaurants failed for query '%s': %s", query, exc)
            continue

    # Persist candidates
    crud.delete_candidates(db, state.room_id)
    for c in candidates:
        crud.create_candidate(db, state.room_id, c)

    logger.info("discover: DONE — found %d candidates for room %s in %.2fs",
                len(candidates), state.room_id, time.perf_counter() - t_start)
    return {"candidates": candidates}
