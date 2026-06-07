import json
import logging
import re

from langgraph.types import RunnableConfig

from agent.state import CircleState, PrefSpec

logger = logging.getLogger(__name__)


def _specs_from_state(state: CircleState) -> list[PrefSpec]:
    """pref_specs live in state as plain dicts — rehydrate to typed helpers."""
    return [PrefSpec(**s) for s in state.pref_specs]


def _parse_menu(raw: str | list) -> list[dict]:
    """Parse get_restaurant_menu tool response into a list of item dicts."""
    if isinstance(raw, list):
        return raw

    text = str(raw).strip()

    # Try JSON
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        for key in ("items", "menuItems", "menu", "data", "categories"):
            if key in data:
                val = data[key]
                if isinstance(val, list):
                    # Might be categories with nested items
                    result = []
                    for entry in val:
                        if isinstance(entry, dict):
                            if "items" in entry and isinstance(entry["items"], list):
                                result.extend(entry["items"])
                            elif "itemId" in entry or "item_id" in entry or "id" in entry:
                                result.append(entry)
                    return result or val
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback text extraction: look for item-like lines
    items = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        item: dict = {}
        # item id
        m = re.search(r"itemId[:\s]+([A-Za-z0-9_-]+)", line, re.I)
        if not m:
            m = re.search(r"\"id\"[:\s]+\"([^\"]+)\"", line)
        if m:
            item["id"] = m.group(1)
        # name
        m = re.search(r"\"name\"[:\s]+\"([^\"]+)\"", line)
        if m:
            item["name"] = m.group(1)
        # price
        m = re.search(r"(?:price|cost)[:\s]+(\d+)", line, re.I)
        if m:
            item["price"] = int(m.group(1))
        # veg
        item["isVeg"] = bool(re.search(r"\bveg\b", line, re.I) and not re.search(r"non.veg", line, re.I))
        if item.get("id"):
            items.append(item)

    return items


def _check_feasibility(item: dict, spec: PrefSpec) -> dict:
    """Rule-based hard constraint check + soft preference scoring."""
    item_name = (item.get("name") or item.get("itemName") or "").lower()
    item_desc = (item.get("description") or item.get("itemDescription") or "").lower()
    text = f"{item_name} {item_desc}"

    price = int(item.get("price") or item.get("finalPrice") or item.get("cost") or 0)
    is_veg = item.get("isVeg") or item.get("is_veg") or False

    # Hard: veg constraint
    if spec.veg == "veg" and not is_veg:
        return {"feasible": False, "score": 0.0, "reason": "veg constraint"}
    if spec.veg == "non_veg" and is_veg:
        return {"feasible": False, "score": 0.0, "reason": "non-veg preference not met"}

    # Hard: budget
    if spec.budget_max and price > spec.budget_max:
        return {"feasible": False, "score": 0.0, "reason": "over budget"}

    # Hard: allergies — never serve if allergen in name/description
    for allergen in spec.allergies:
        if allergen.lower() in text:
            return {"feasible": False, "score": 0.0, "reason": f"allergen: {allergen}"}

    # Hard: excludes
    for excl in spec.excludes:
        if excl.lower() in text:
            return {"feasible": False, "score": 0.0, "reason": f"excluded: {excl}"}

    # Soft preference scoring — keyword matching
    soft_hits = sum(1 for s in spec.soft if s.lower() in text)
    score = (soft_hits / len(spec.soft)) if spec.soft else 0.5

    # Slight boost for price efficiency
    if spec.budget_max and price <= spec.budget_max * 0.75:
        score = min(1.0, score + 0.1)

    return {"feasible": True, "score": round(score, 3), "reason": "ok"}


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools = config["configurable"]["mcp_tools"]

    specs = _specs_from_state(state)
    feasibility_map: dict = {}

    for candidate in state.candidates:
        rest_id = candidate["id"]
        rest_name = candidate.get("name", "Unknown")

        try:
            result = await tools["get_restaurant_menu"].ainvoke({
                "restaurantId": rest_id,
                "addressId": state.address_id,   # required (undocumented)
            })
            items = _parse_menu(result)
        except Exception as exc:
            logger.warning("get_restaurant_menu failed for %s: %s", rest_name, exc)
            items = []

        if not items:
            logger.warning("No menu items for %s — skipping feasibility", rest_name)
            # Still include in map so every candidate has an entry
            feasibility_map[rest_id] = {
                spec.participant_id: {"feasible": False, "score": 0.0, "reason": "no menu data", "best_item": None}
                for spec in specs
            }
            continue

        per_participant: dict = {}
        for spec in specs:
            feasible_items = []
            for item in items:
                result_score = _check_feasibility(item, spec)
                if result_score["feasible"]:
                    feasible_items.append((item, result_score["score"]))

            if feasible_items:
                best_item, best_score = max(feasible_items, key=lambda x: x[1])
                per_participant[spec.participant_id] = {
                    "feasible": True,
                    "score": best_score,
                    "reason": "ok",
                    "best_item": {
                        "id": best_item.get("id") or best_item.get("itemId", ""),
                        "name": best_item.get("name") or best_item.get("itemName", ""),
                        "price": int(best_item.get("price") or best_item.get("finalPrice") or 0),
                        "is_veg": best_item.get("isVeg") or best_item.get("is_veg") or False,
                    },
                }
            else:
                per_participant[spec.participant_id] = {
                    "feasible": False,
                    "score": 0.0,
                    "reason": "no feasible items",
                    "best_item": None,
                }

        feasibility_map[rest_id] = per_participant
        logger.debug("feasibility %s: %d/%d participants covered",
                     rest_name,
                     sum(1 for v in per_participant.values() if v["feasible"]),
                     len(specs))

    logger.info("feasibility: checked %d candidates for room %s", len(state.candidates), state.room_id)
    return {"feasibility_map": feasibility_map}
