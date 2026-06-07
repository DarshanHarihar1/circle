"""
Resolver — turns the feasibility map into a ranked shortlist of plans.

Works off the structures produced by Phase 3:
  candidates       : list[dict]  — {id, name, cuisines, ...}
  feasibility_map  : {restaurant_id: {participant_id: {feasible, score, best_item}}}
  pref_specs       : list[dict]  — PrefSpec.model_dump()

A "plan" here is a plain dict matching Plan.model_dump() so it can live in
CircleState and be persisted directly.
"""
import uuid


def _sub_order_item(pid: str, best: dict) -> dict:
    return {
        "participant_id": pid,
        "item_id": best.get("id", ""),
        "item_name": best.get("name", ""),
        "variant": best.get("variant"),
        "addons": best.get("addons", []),
        "price": int(best.get("price", 0)),
    }


def score_restaurant(restaurant_id: str, feasibility_map: dict,
                     pref_specs: list[dict]) -> dict:
    """coverage / uncovered / satisfaction / best_items for one restaurant."""
    per = feasibility_map.get(restaurant_id, {})
    coverage, uncovered, best_items = [], [], {}
    score_sum = 0.0
    for spec in pref_specs:
        pid = spec["participant_id"]
        entry = per.get(pid, {})
        if entry.get("feasible") and entry.get("best_item"):
            coverage.append(pid)
            best_items[pid] = entry["best_item"]
            score_sum += float(entry.get("score", 0.0))
        else:
            uncovered.append(pid)
    satisfaction = score_sum / len(pref_specs) if pref_specs else 0.0
    return {"coverage": coverage, "uncovered": uncovered,
            "satisfaction": round(satisfaction, 3), "best_items": best_items}


def _sub_order_from(restaurant: dict, best_items: dict) -> dict:
    items = [_sub_order_item(pid, bi) for pid, bi in best_items.items()]
    subtotal = sum(i["price"] for i in items)
    return {
        "restaurant_id": restaurant["id"],
        "restaurant_name": restaurant.get("name", "Restaurant"),
        "items": items,
        "coupon_code": None,
        "subtotal": subtotal,
        "fees": 0,
        "discount": 0,
        "total": subtotal,
    }


def _per_person_fit(best_items: dict, name_by_pid: dict) -> dict:
    fit = {}
    for pid, bi in best_items.items():
        fit[pid] = f"{bi.get('name', 'Item')} ₹{int(bi.get('price', 0))}"
    return fit


def build_single_plans(candidates: list[dict], feasibility_map: dict,
                       pref_specs: list[dict], name_by_pid: dict) -> list[dict]:
    """Full-coverage single-restaurant plans, best satisfaction first."""
    plans = []
    for r in candidates:
        sc = score_restaurant(r["id"], feasibility_map, pref_specs)
        if sc["uncovered"]:
            continue
        sub = _sub_order_from(r, sc["best_items"])
        plans.append({
            "plan_id": str(uuid.uuid4()),
            "kind": "single",
            "sub_orders": [sub],
            "total": sub["total"],
            "satisfaction": sc["satisfaction"],
            "n_deliveries": 1,
            "rationale": "",
            "why_not_runner_up": "",
            "per_person_fit": _per_person_fit(sc["best_items"], name_by_pid),
        })
    plans.sort(key=lambda p: (-p["satisfaction"], p["total"]))
    return plans


def _best_placement(pid: str, candidates: list[dict], feasibility_map: dict):
    """The (restaurant, best_item, score) that maximises this participant's
    soft-pref score across all feasible candidates. None if uncovered."""
    best = None  # (score, restaurant, item)
    for r in candidates:
        entry = feasibility_map.get(r["id"], {}).get(pid, {})
        if entry.get("feasible") and entry.get("best_item"):
            score = float(entry.get("score", 0.0))
            if best is None or score > best[0]:
                best = (score, r, entry["best_item"])
    return best


def build_multi_plan(candidates: list[dict], feasibility_map: dict,
                     pref_specs: list[dict], name_by_pid: dict) -> dict | None:
    """
    Max-satisfaction multi-restaurant plan: give every participant their single
    best-matching dish across all restaurants, then group those picks by
    restaurant. If the picks span ≥2 restaurants this is a genuine multi plan
    that complements the single-restaurant options (higher satisfaction, more
    deliveries). Participants with no feasible item anywhere are recorded as
    uncovered (a conflict signal).
    """
    groups: dict[str, dict] = {}      # restaurant_id -> {restaurant, best_items}
    score_sum = 0.0
    uncovered = []

    for spec in pref_specs:
        pid = spec["participant_id"]
        placement = _best_placement(pid, candidates, feasibility_map)
        if not placement:
            uncovered.append(pid)
            continue
        score, restaurant, item = placement
        score_sum += score
        g = groups.setdefault(restaurant["id"], {"restaurant": restaurant, "best_items": {}})
        g["best_items"][pid] = item

    # Need at least two distinct restaurants for a multi plan to be meaningful.
    if len(groups) < 2:
        return None

    sub_orders = [_sub_order_from(g["restaurant"], g["best_items"]) for g in groups.values()]
    total = sum(s["total"] for s in sub_orders)
    fit = {}
    for g in groups.values():
        fit.update(_per_person_fit(g["best_items"], name_by_pid))
    satisfaction = score_sum / len(pref_specs) if pref_specs else 0.0

    return {
        "plan_id": str(uuid.uuid4()),
        "kind": "multi",
        "sub_orders": sub_orders,
        "total": total,
        "satisfaction": round(satisfaction, 3),
        "n_deliveries": len(sub_orders),
        "rationale": "",
        "why_not_runner_up": "",
        "per_person_fit": fit,
        "_uncovered": uncovered,   # internal: who is still uncovered (conflict)
    }


def rank_plans(plans: list[dict]) -> list[dict]:
    """Single-delivery first, then satisfaction desc, then cheapest. Top 3."""
    ranked = sorted(
        plans,
        key=lambda p: (-int(p["n_deliveries"] == 1), -p["satisfaction"], p["total"]),
    )
    return ranked[:3]
