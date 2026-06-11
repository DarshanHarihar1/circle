import logging
import time

from langgraph.types import RunnableConfig

from agent.state import CircleState, PrefSpec
from agent.nodes.cart_mcp import call_structured

logger = logging.getLogger(__name__)


def _specs_from_state(state: CircleState) -> list[PrefSpec]:
    """pref_specs live in state as plain dicts — rehydrate to typed helpers."""
    return [PrefSpec(**s) for s in state.pref_specs]


# Page 1 of a Swiggy menu is the rotating offers/combos section ("Minimum 50%
# off", "Recommended", combo bundles); the real à-la-carte categories ("7"
# REGULAR PIZZAS | VEG", etc.) live on later pages. Fetch enough pages to reach
# them so scoring sees actual dishes, not just whatever combos are promoted today.
MENU_PAGE_SIZE = 8   # max categories per page Swiggy allows
MAX_MENU_PAGES = 3


def _menu_items_from_sc(sc: dict) -> list[dict]:
    """Flatten one get_restaurant_menu page's categories into item dicts.

    Each item has {id, name, description, price, isVeg, hasVariants, hasAddons}.
    """
    items: list[dict] = []
    for category in (sc.get("categories") or []):
        for item in (category.get("items") or []):
            if isinstance(item, dict) and item.get("id"):
                items.append(item)
    return items


async def _fetch_menu_items(tool, rest_id: str, address_id: str) -> list[dict]:
    """Fetch up to MAX_MENU_PAGES of a restaurant's menu, deduped by item id."""
    items: list[dict] = []
    seen: set = set()
    for page in range(1, MAX_MENU_PAGES + 1):
        sc = await call_structured(tool, {
            "restaurantId": rest_id,
            "addressId": address_id,   # required (undocumented)
            "page": page,
            "pageSize": MENU_PAGE_SIZE,
        })
        for item in _menu_items_from_sc(sc):
            iid = item.get("id")
            if iid not in seen:
                seen.add(iid)
                items.append(item)
        if not sc.get("hasMore"):
            break
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

    # Hard: must-have — the card promises "this exact dish lands in your order",
    # so an item that doesn't contain the must-have can't cover this participant.
    # If no item at a restaurant matches, the participant is left uncovered there
    # and that plan is dropped / flagged as a conflict downstream.
    if spec.must_have and spec.must_have.lower().strip() not in text:
        return {"feasible": False, "score": 0.0, "reason": "missing must-have"}

    # Soft preference scoring — keyword matching (cuisine "mood"). parse_prefs
    # also folds must_have into spec.soft; that's fine, the hard gate above is
    # what guarantees the dish.
    soft_hits = sum(1 for s in spec.soft if s.lower() in text)
    score = (soft_hits / len(spec.soft)) if spec.soft else 0.5

    # Price efficiency is a tie-breaker, NOT a score boost — folding it in would
    # let a cheap off-cuisine dish tie a perfect cuisine match. It only orders
    # items that already match the soft prefs equally well.
    budget_eff = bool(spec.budget_max and price <= spec.budget_max * 0.75)

    return {"feasible": True, "score": round(score, 3), "reason": "ok",
            "budget_eff": budget_eff}


def _item_text(item: dict) -> str:
    name = (item.get("name") or item.get("itemName") or "").lower()
    desc = (item.get("description") or item.get("itemDescription") or "").lower()
    return f"{name} {desc}"


def _cuisine_tags(spec: PrefSpec) -> list[str]:
    """The cuisine the participant actually craves, i.e. their soft tags minus
    the must-have term. parse_prefs folds must_have into soft, but the must-have
    is usually a generic protein ("chicken") that matches almost any restaurant —
    excluding it here means a chicken pizza can't masquerade as a "biryani"
    craving when we check whether a restaurant covers the cuisine."""
    mh = spec.must_have.lower().strip() if spec.must_have else None
    return [t for t in spec.soft if t and t.lower().strip() != mh]


def _cuisine_match(text: str, tags: list[str]) -> bool:
    """True if the dish satisfies any cuisine tag. A multi-word vibe ("spicy
    biryani") rarely appears verbatim, so a significant word of it (e.g.
    "biryani") also counts — matching how discover expands phrases for search."""
    for tag in tags:
        t = tag.lower().strip()
        if not t:
            continue
        if t in text:
            return True
        if any(len(w) >= 4 and w in text for w in t.split()):
            return True
    return False


async def run(state: CircleState, config: RunnableConfig) -> dict:
    tools = config["configurable"]["mcp_tools"]

    specs = _specs_from_state(state)
    feasibility_map: dict = {}

    logger.info("feasibility: START room %s — fetching menus for %d candidates",
                state.room_id, len(state.candidates))
    t_start = time.perf_counter()

    for candidate in state.candidates:
        rest_id = candidate["id"]
        rest_name = candidate.get("name", "Unknown")

        try:
            items = await _fetch_menu_items(
                tools["get_restaurant_menu"], rest_id, state.address_id
            )
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
                    feasible_items.append((item, result_score))

            best_item = best = None
            if feasible_items:
                best_item, best = max(
                    feasible_items,
                    key=lambda x: (x[1]["score"], x[1]["budget_eff"]),
                )
                # Cuisine-craving gate: the best dish must actually match the
                # cuisine they crave. The best dish already maximises soft score,
                # so it matches the cuisine whenever any feasible dish here does;
                # if it doesn't, no dish here serves their craving — leave them
                # uncovered so this restaurant's plan is dropped / flagged.
                cuisine = _cuisine_tags(spec)
                if cuisine and not _cuisine_match(_item_text(best_item), cuisine):
                    feasible_items = []

            if feasible_items:
                per_participant[spec.participant_id] = {
                    "feasible": True,
                    "score": best["score"],
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

    logger.info("feasibility: DONE — checked %d candidates for room %s in %.2fs",
                len(state.candidates), state.room_id, time.perf_counter() - t_start)
    return {"feasibility_map": feasibility_map}
