import logging
import time

from langgraph.types import RunnableConfig

from agent.state import CircleState
from agent import resolver
from db import crud

logger = logging.getLogger(__name__)


def _rationale(plan: dict, runner_up: dict | None) -> tuple[str, str]:
    """Deterministic, always-accurate 'why this plan' + 'why not the runner-up'.

    Built straight from the plan numbers — no LLM. The rationale is a factual
    summary, so a template is faster, never wrong, and has no rate limits.
    """
    title = " + ".join(s["restaurant_name"] for s in plan["sub_orders"])
    n_people = len(plan.get("per_person_fit") or {})
    sat = round(plan.get("satisfaction", 0) * 100)
    deliveries = "one delivery" if plan["n_deliveries"] == 1 else f"{plan['n_deliveries']} deliveries"
    who = f"all {n_people} of you" if n_people else "everyone"

    rationale = (
        f"{title} covers {who} in {deliveries} for ₹{plan['total']} "
        f"— {sat}% match to everyone's cards."
    )

    why_not = ""
    if runner_up:
        edges = []
        diff = runner_up["total"] - plan["total"]
        if diff > 0:
            edges.append(f"₹{diff} cheaper")
        elif diff < 0:
            edges.append(f"₹{-diff} pricier")
        ru_sat = round(runner_up.get("satisfaction", 0) * 100)
        if sat > ru_sat:
            edges.append(f"a higher {sat}% vs {ru_sat}% match")
        elif sat < ru_sat:
            edges.append(f"a lower {sat}% vs {ru_sat}% match")
        if plan["n_deliveries"] < runner_up["n_deliveries"]:
            edges.append("fewer deliveries")
        elif plan["n_deliveries"] > runner_up["n_deliveries"]:
            edges.append("more deliveries")
        if edges:
            why_not = "Versus the runner-up: " + ", ".join(edges) + "."

    return rationale, why_not


async def run(state: CircleState, config: RunnableConfig) -> dict:
    db = config["configurable"]["db"]

    logger.info("build_plans: START room %s", state.room_id)
    t_start = time.perf_counter()

    pref_specs = state.pref_specs
    name_by_pid = {s["participant_id"]: s["display_name"] for s in pref_specs}

    singles = resolver.build_single_plans(state.candidates, state.feasibility_map,
                                          pref_specs, name_by_pid)
    multi = resolver.build_multi_plan(state.candidates, state.feasibility_map,
                                      pref_specs, name_by_pid)

    # Assemble a diverse shortlist (≤3): the best single, the multi alternative
    # (so the group can weigh satisfaction vs. an extra delivery), then fill the
    # remaining slots with the next-best distinct singles. Without this, several
    # near-identical full-coverage singles would crowd the multi out of the top 3.
    plans: list[dict] = []
    conflict_message = None

    if singles:
        plans.append(singles[0])

    if multi:
        uncovered = multi.pop("_uncovered", [])
        if uncovered:
            # Even the multi plan can't cover everyone → surface a conflict and
            # present the partial-coverage plan with a note.
            names = ", ".join(name_by_pid.get(pid, pid) for pid in uncovered)
            conflict_message = (
                f"{names}'s constraints couldn't be fully met nearby — "
                f"their item is flagged for confirmation."
            )
            multi["notes_uncovered"] = uncovered
        plans.append(multi)

    # Fill remaining shortlist slots with the next-best distinct singles.
    for s in singles[1:]:
        if len(plans) >= 3:
            break
        plans.append(s)

    # If nothing covers everyone and there's no multi plan, flag a conflict and
    # still present the best partial single plans by coverage.
    if not plans:
        partials = []
        for r in state.candidates:
            sc = resolver.score_restaurant(r["id"], state.feasibility_map, pref_specs)
            if sc["coverage"]:
                sub = resolver._sub_order_from(r, sc["best_items"])
                partials.append({
                    "plan_id": __import__("uuid").uuid4().hex,
                    "kind": "single",
                    "sub_orders": [sub],
                    "total": sub["total"],
                    "satisfaction": sc["satisfaction"],
                    "n_deliveries": 1,
                    "rationale": "",
                    "why_not_runner_up": "",
                    "per_person_fit": resolver._per_person_fit(sc["best_items"], name_by_pid),
                    "notes_uncovered": sc["uncovered"],
                })
        partials.sort(key=lambda p: (-len(p["per_person_fit"]), -p["satisfaction"]))
        plans = partials[:3]
        if plans:
            uncovered = plans[0].get("notes_uncovered", [])
            names = ", ".join(name_by_pid.get(pid, pid) for pid in uncovered)
            conflict_message = (
                f"No single nearby place covers everyone. {names} may need a "
                f"separate order — flagged for confirmation."
            )

    ranked = resolver.rank_plans(plans)

    # Rationale per plan (runner-up = next plan in the ranking). Deterministic —
    # no LLM, so it's instant and always factually correct.
    for i, plan in enumerate(ranked):
        runner_up = ranked[i + 1] if i + 1 < len(ranked) else None
        plan["rationale"], plan["why_not_runner_up"] = _rationale(plan, runner_up)
        plan["rank"] = i + 1

    # Persist
    crud.delete_plans(db, state.room_id)
    for plan in ranked:
        crud.create_plan(db, state.room_id, plan)

    logger.info("build_plans: DONE — %d plans for room %s (conflict=%s) in %.2fs",
                len(ranked), state.room_id, bool(conflict_message),
                time.perf_counter() - t_start)

    out = {"plans": ranked}
    if conflict_message:
        out["conflict_message"] = conflict_message
    return out
