import json
import logging
from langgraph.types import RunnableConfig
from agent.state import CircleState
from db import crud

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 12


def _parse_restaurants(raw: str | list) -> list[dict]:
    """Parse search_restaurants tool response into a list of restaurant dicts."""
    if isinstance(raw, list):
        # Already a list of dicts
        return raw

    text = str(raw).strip()

    # Try JSON array first
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "restaurants" in data:
            return data["restaurants"]
        if isinstance(data, dict) and "data" in data:
            items = data["data"]
            if isinstance(items, list):
                return items
    except (json.JSONDecodeError, KeyError):
        pass

    # Fallback: try to extract restaurant data from text
    restaurants = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        if not line:
            continue
        r: dict = {}
        # Extract restaurant_id / id
        for pat in [r"restaurantId[:\s]+([A-Za-z0-9_-]+)",
                    r"\"id\"[:\s]+\"([^\"]+)\"",
                    r"\(ID:\s*([^)]+)\)"]:
            import re
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                r["id"] = m.group(1).strip()
                break
        if not r.get("id"):
            continue

        import re
        name_m = re.search(r"\"name\"[:\s]+\"([^\"]+)\"", line)
        if name_m:
            r["name"] = name_m.group(1)
        avail_m = re.search(r"(OPEN|CLOSED)", line)
        r["availabilityStatus"] = avail_m.group(1) if avail_m else "OPEN"
        restaurants.append(r)

    return restaurants


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools = config["configurable"]["mcp_tools"]
    db = config["configurable"]["db"]

    # Build union of cuisine queries from all participants' soft prefs.
    # pref_specs are plain dicts in state (PrefSpec.model_dump()).
    seen_queries: set[str] = set()
    queries: list[str] = []
    for spec in state.pref_specs:
        for term in spec.get("soft", []):
            t = term.lower().strip()
            if t and t not in seen_queries:
                seen_queries.add(t)
                queries.append(term)

    # Ensure at least one search query
    if not queries:
        queries = ["restaurant"]

    # Cap number of search calls to avoid rate limits
    queries = queries[:MAX_CANDIDATES]

    seen_ids: set[str] = set()
    candidates: list[dict] = []

    for query in queries:
        if len(candidates) >= MAX_CANDIDATES:
            break
        try:
            result = await tools["search_restaurants"].ainvoke({
                "addressId": state.address_id,
                "query": query,
            })
            restaurants = _parse_restaurants(result)
            for r in restaurants:
                rid = r.get("id") or r.get("restaurantId") or r.get("restaurant_id", "")
                if not rid or rid in seen_ids:
                    continue
                avail = (
                    r.get("availabilityStatus")
                    or r.get("availability", "OPEN")
                ).upper()
                if avail != "OPEN":
                    continue
                seen_ids.add(rid)
                candidates.append({
                    "id": rid,
                    "name": r.get("name") or r.get("restaurant_name") or r.get("restaurantName", "Unknown"),
                    "cuisines": r.get("cuisines") or r.get("cuisine") or [],
                    "rating": r.get("avgRating") or r.get("rating"),
                    "cost_for_two": r.get("costForTwo") or r.get("cost_for_two"),
                    "distance_km": r.get("distance") or r.get("distance_km"),
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

    logger.info("discover: found %d candidates for room %s", len(candidates), state.room_id)
    return {"candidates": candidates}
