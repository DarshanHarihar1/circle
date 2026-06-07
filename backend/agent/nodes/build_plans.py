import json
import logging

from langgraph.types import RunnableConfig

from agent.state import CircleState
from agent import resolver
from db import crud
from llm import chat, RATIONALE_MODEL

logger = logging.getLogger(__name__)

_RATIONALE_SYS = (
    "Write a 2-sentence 'why this plan works for everyone' using ONLY the data "
    "given. Then write a 1-sentence 'why not the runner-up' using ONLY the "
    "contrast data given. Never invent facts. Return JSON: "
    '{"rationale": "...", "why_not_runner_up": "..."}'
)


async def _rationale(plan: dict, runner_up: dict | None) -> tuple[str, str]:
    summary = {
        "kind": plan["kind"],
        "restaurants": [s["restaurant_name"] for s in plan["sub_orders"]],
        "total": plan["total"],
        "deliveries": plan["n_deliveries"],
        "satisfaction": plan["satisfaction"],
        "per_person": plan["per_person_fit"],
    }
    contrast = None
    if runner_up:
        contrast = {
            "kind": runner_up["kind"],
            "total": runner_up["total"],
            "deliveries": runner_up["n_deliveries"],
            "satisfaction": runner_up["satisfaction"],
        }
    try:
        raw = await chat(
            model=RATIONALE_MODEL,
            system=_RATIONALE_SYS,
            user=f"Plan: {json.dumps(summary)}\nRunner-up contrast: {json.dumps(contrast)}",
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
        return data.get("rationale", "").strip(), data.get("why_not_runner_up", "").strip()
    except Exception as exc:
        logger.warning("rationale generation failed: %s — using fallback", exc)
        names = ", ".join(summary["restaurants"])
        rationale = (
            f"{names} covers everyone's picks in {plan['n_deliveries']} "
            f"delivery(s) for ₹{plan['total']}."
        )
        why_not = ""
        if runner_up:
            diff = runner_up["total"] - plan["total"]
            why_not = (
                f"The runner-up costs ₹{abs(diff)} "
                f"{'more' if diff >= 0 else 'less'} with "
                f"{runner_up['n_deliveries']} delivery(s)."
            )
        return rationale, why_not


async def run(state: CircleState, config: RunnableConfig) -> dict:
    db = config["configurable"]["db"]

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

    # Rationale per plan (runner-up = next plan in the ranking)
    for i, plan in enumerate(ranked):
        runner_up = ranked[i + 1] if i + 1 < len(ranked) else None
        plan["rationale"], plan["why_not_runner_up"] = await _rationale(plan, runner_up)
        plan["rank"] = i + 1

    # Persist
    crud.delete_plans(db, state.room_id)
    for plan in ranked:
        crud.create_plan(db, state.room_id, plan)

    logger.info("build_plans: %d plans for room %s (conflict=%s)",
                len(ranked), state.room_id, bool(conflict_message))

    out = {"plans": ranked}
    if conflict_message:
        out["conflict_message"] = conflict_message
    return out
