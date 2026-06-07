"""
Phase 4 end-to-end test harness — Resolver: Build Plans + Voting.

Runs the real discover→feasibility→build_plans→price_plans segment against a
real Postgres DB, with the network-blocked OpenRouter LLM and Swiggy MCP
(search / menu / cart) mocked with realistic payloads. Never calls
place_food_order. Then exercises voting + plan selection via the CRUD layer
(same calls the API routes make).

    DATABASE_URL=postgresql://postgres@127.0.0.1:5599/circle_test python test_phase4.py
"""
import asyncio
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@127.0.0.1:5599/circle_test")

from sqlalchemy import text  # noqa: E402

from config import settings  # noqa: E402
from db.session import SessionLocal, engine  # noqa: E402
from db import crud  # noqa: E402
from db.models import Base, PlacedOrder  # noqa: E402
import agent.nodes.parse_prefs as parse_prefs_mod  # noqa: E402
import agent.nodes.build_plans as build_plans_mod  # noqa: E402
from agent.state import CircleState  # noqa: E402
from agent.graph import build_parse_graph, build_discover_graph  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── Mock Swiggy MCP ───────────────────────────────────────────────────────────

def _restaurants(veg_only=False):
    base = [
        ("R1", "Spice Junction", ["North Indian", "Biryani"]),
        ("R2", "Biryani Blues", ["Biryani", "Mughlai"]),
        ("R3", "Green Bowl", ["Healthy", "Salads"]),
        ("R4", "Dragon Wok", ["Chinese"]),
        ("R5", "The Biryani Life", ["Biryani"]),
        ("R6", "Noodle House", ["Chinese"]),
        ("R7", "Light Bites", ["Healthy"]),
        ("R8", "Tandoori Nights", ["North Indian"]),
    ]
    return [{"id": i, "name": n, "cuisines": c, "availabilityStatus": "OPEN"} for i, n, c in base]


# Menu generator with knobs so we can craft single-coverage, multi-coverage,
# all-veg and conflict scenarios.
class MenuPlan:
    """Controls which restaurants can cover which participants."""
    def __init__(self, mode="mixed"):
        self.mode = mode

    def menu(self, rid):
        # default items present at every restaurant
        items = [
            {"id": f"{rid}-veg", "name": "Veg Makhani Wrap", "price": 189, "isVeg": True,
             "description": "light veg wrap biryani spices"},
            {"id": f"{rid}-paneer", "name": "Paneer Tikka", "price": 220, "isVeg": True,
             "description": "spicy paneer"},
        ]
        if self.mode == "all_veg":
            return items  # no non-veg anywhere
        # mixed: add non-veg
        items += [
            {"id": f"{rid}-nonveg", "name": "Chicken Biryani", "price": 279, "isVeg": False,
             "description": "spicy chicken biryani"},
            {"id": f"{rid}-egg", "name": "Egg Roll", "price": 99, "isVeg": False,
             "description": "quick egg roll"},
        ]
        if self.mode == "multi":
            # Only R4/R6 (Chinese) carry noodles that the 'chinese' guest wants;
            # only R1/R2/R5 carry biryani for the non-veg biryani guest.
            if rid in ("R4", "R6"):
                items.append({"id": f"{rid}-noodles", "name": "Veg Hakka Noodles", "price": 199,
                              "isVeg": True, "description": "chinese veg noodles light"})
                # remove biryani so these don't single-cover everyone
                items = [i for i in items if "biryani" not in i["name"].lower()]
            else:
                items = [i for i in items if "noodles" not in i["name"].lower()]
        if self.mode == "conflict":
            # Carol wants something nobody has, and is allergic to everything cheap
            pass
        return items


class MockTool:
    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._fn(args)


def _mk_tools(menuplan, veg_only=False, cart_total=836):
    def search(args):
        return json.dumps(_restaurants(veg_only))

    def menu(args):
        return json.dumps({"items": menuplan.menu(args["restaurantId"])})

    placed = {"order": False}

    def upd_cart(args):
        return "Cart updated."

    def get_cart(args):
        return json.dumps({"total": cart_total, "subtotal": cart_total - 40,
                           "deliveryFee": 40, "discount": 0})

    def coupons(args):
        return json.dumps({"coupons": [{"code": "SWIGGY50", "codEligible": True}]})

    def flush(args):
        return "Cart flushed."

    def place_order(args):
        placed["order"] = True
        raise AssertionError("place_food_order must NOT be called in Phase 4 tests")

    return {
        "search_restaurants": MockTool(search),
        "get_restaurant_menu": MockTool(menu),
        "update_food_cart": MockTool(upd_cart),
        "get_food_cart": MockTool(get_cart),
        "fetch_food_coupons": MockTool(coupons),
        "flush_food_cart": MockTool(flush),
        "place_food_order": MockTool(place_order),
    }, placed


async def _mock_chat_parse(model, system, user, response_format=None):
    u = user.lower()
    veg = "veg" if "veg/nonveg/either: veg" in u else ("non_veg" if "non_veg" in u else "either")
    allergies = ["peanut"] if "peanut" in u else []
    soft = [kw for kw in ("biryani", "spicy", "light", "chinese", "noodles") if kw in u]
    import re
    m = re.search(r"budget:\s*(\d+)", u)
    return json.dumps({"veg": veg, "budget_max": int(m.group(1)) if m else None,
                       "allergies": allergies, "excludes": [], "soft": soft})


async def _mock_chat_rationale(model, system, user, response_format=None):
    return json.dumps({"rationale": "Covers everyone's picks in one go.",
                       "why_not_runner_up": "The runner-up adds a second delivery."})


def _cfg(room_id, db, tools):
    return {"configurable": {"thread_id": room_id, "mcp_tools": tools, "db": db,
                             "supabase_url": "", "supabase_key": ""}}


def _reset_db():
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)


def seed(db, cards):
    host_card = dict(cards[0])
    host_card.pop("_name", None)
    room, host = crud.create_room(db, host_user_id="host-uid", display_name="Darshan")
    crud.set_room_address(db, room.id, "ADDR123")
    pids = [host.id]
    crud.upsert_craving_card(db, host.id, room.id, **host_card)
    for i, c in enumerate(cards[1:], start=1):
        c = dict(c)
        g = crud.create_participant(db, room.id, c.pop("_name", f"G{i}"))
        pids.append(g.id)
        crud.upsert_craving_card(db, g.id, room.id, **c)
    return room.id, host.id, pids


async def _run_segment(db, room_id, host_id, tools):
    pg = build_parse_graph()
    await pg.ainvoke(CircleState(room_id=room_id, host_user_id=host_id, address_id="ADDR123"),
                     _cfg(room_id, db, tools))
    crud.approve_pref_specs(db, room_id)
    state = CircleState(room_id=room_id, host_user_id=host_id, address_id="ADDR123",
                        pref_specs=crud.pref_specs_as_dicts(db, room_id))
    dg = build_discover_graph()
    result = await dg.ainvoke(state, _cfg(room_id, db, tools))
    crud.set_room_status(db, room_id, "choosing")  # mirrors runner.run_discover
    return result


def _card(name, veg, budget, vibe, allergies=None, deal=None):
    return {"_name": name, "veg": veg, "budget_max": budget, "cuisine_vibe": vibe,
            "must_have": "", "allergies": allergies or [], "deal_breakers": deal or []}


async def main():
    print(f"\nPhase 4 E2E — DB: {settings.DATABASE_URL}\n")
    parse_prefs_mod.chat = _mock_chat_parse
    build_plans_mod.chat = _mock_chat_rationale

    # ───────────────────────────────────────────────────────────────────────
    # Scenario A — mixed 4-person group → expect single + multi plans
    # ───────────────────────────────────────────────────────────────────────
    print("Scenario A — mixed group (single + multi expected)")
    _reset_db()
    db = SessionLocal()
    cards = [
        _card("Darshan", "either", None, "biryani spicy"),
        _card("Alice", "veg", 250, "light chinese noodles", allergies=["peanut"]),
        _card("Bob", "non_veg", 300, "biryani"),
        _card("Carol", "either", None, "spicy biryani"),
    ]
    room_id, host_id, pids = seed(db, cards)
    tools, placed = _mk_tools(MenuPlan("multi"))
    result = await _run_segment(db, room_id, host_id, tools)

    plans = crud.get_plans(db, room_id)
    kinds = {p.kind for p in plans}
    check("Plans were generated and persisted", len(plans) >= 1, f"{len(plans)} plans")
    check("At least one single-restaurant plan exists", "single" in kinds, f"kinds={kinds}")
    check("At least one multi-restaurant plan exists", "multi" in kinds, f"kinds={kinds}")
    check("Each plan has non-empty rationale and why_not_runner_up",
          all(p.rationale and p.why_not_runner_up for p in plans))
    check("per_person_fit has an entry for every participant",
          all(len(p.per_person_fit or {}) == len(pids) for p in plans if p.kind == "single"))
    check("Plans are ranked (rank set, ≤3, unique)",
          all(p.rank for p in plans) and len(plans) <= 3
          and len({p.rank for p in plans}) == len(plans))
    check("place_food_order was NEVER called", placed["order"] is False)

    # Pricing: plan totals come from get_food_cart (mock returns 836/sub-order)
    single = next(p for p in plans if p.kind == "single")
    check("Single-plan total matches get_food_cart total (not estimated)",
          single.total == 836, f"total={single.total}")
    multi = next(p for p in plans if p.kind == "multi")
    check("Multi-plan total = sum of its sub-order cart totals",
          multi.total == 836 * multi.n_deliveries, f"total={multi.total} n={multi.n_deliveries}")
    check("Coupon code captured from fetch_food_coupons",
          any((so.get("coupon_code") == "SWIGGY50") for p in plans for so in p.sub_orders))

    # Gate: present_options — no orders placed before the host chooses
    check("present_options gate: placed_orders empty before any vote/choose",
          len(db.query(PlacedOrder).filter_by(room_id=room_id).all()) == 0)
    check("room.status advanced to 'choosing' after planning segment",
          crud.get_room(db, room_id).status == "choosing")

    # Voting: 2 votes on plan A, 1 on plan B
    plan_a, plan_b = plans[0].id, plans[1].id if len(plans) > 1 else plans[0].id
    crud.add_plan_vote(db, pids[0], plan_a)
    crud.add_plan_vote(db, pids[1], plan_a)
    crud.add_plan_vote(db, pids[2], plan_b)
    counts = crud.get_vote_counts(db, room_id)
    check("Voting tallies correctly (2 on A, 1 on B)",
          counts.get(plan_a) == 2 and counts.get(plan_b) == 1, f"counts={counts}")
    # Re-vote replaces, not duplicates
    crud.add_plan_vote(db, pids[0], plan_b)
    counts2 = crud.get_vote_counts(db, room_id)
    check("Re-voting moves the vote (no double counting)",
          counts2.get(plan_a) == 1 and counts2.get(plan_b) == 2, f"counts={counts2}")

    # Choose plan → chosen=true, status ordering, graph proceeds
    crud.set_plan_chosen(db, room_id, plan_a)
    crud.set_room_status(db, room_id, "ordering")
    chosen = [p for p in crud.get_plans(db, room_id) if p.chosen]
    check("After choose-plan: exactly one plan has chosen=true",
          len(chosen) == 1 and chosen[0].id == plan_a)
    check("After choose-plan: room.status = 'ordering'",
          crud.get_room(db, room_id).status == "ordering")
    db.close()

    # ───────────────────────────────────────────────────────────────────────
    # Scenario B — everyone veg → no non-veg item in any plan
    # ───────────────────────────────────────────────────────────────────────
    print("\nScenario B — all-veg group (no non-veg items in any plan)")
    _reset_db()
    db = SessionLocal()
    cards = [
        _card("Darshan", "veg", None, "paneer"),
        _card("Alice", "veg", None, "light veg"),
        _card("Bob", "veg", None, "spicy veg"),
    ]
    room_id, host_id, pids = seed(db, cards)
    tools, _ = _mk_tools(MenuPlan("mixed"))   # menus DO contain non-veg
    await _run_segment(db, room_id, host_id, tools)
    plans = crud.get_plans(db, room_id)
    nonveg_terms = ("chicken", "egg", "mutton", "fish")
    has_nonveg = any(
        any(t in so_item["item_name"].lower() for t in nonveg_terms)
        for p in plans for so in p.sub_orders for so_item in so["items"]
    )
    check("All-veg group: no non-veg item appears in any plan", not has_nonveg)
    check("All-veg group still produced at least one plan", len(plans) >= 1, f"{len(plans)} plans")
    db.close()

    # ───────────────────────────────────────────────────────────────────────
    # Scenario C — conflict: ₹1 budget + common-ingredient allergy
    # ───────────────────────────────────────────────────────────────────────
    print("\nScenario C — conflict (impossible constraints → conflict surfaced)")
    _reset_db()
    db = SessionLocal()
    cards = [
        _card("Darshan", "either", None, "biryani"),
        _card("Alice", "veg", None, "light"),
        # Meera: ₹1 budget AND allergic to 'veg' and 'paneer' and 'chicken' etc.
        _card("Meera", "veg", 1, "anything", allergies=["veg", "paneer", "wrap", "tikka"]),
    ]
    room_id, host_id, pids = seed(db, cards)
    tools, _ = _mk_tools(MenuPlan("mixed"))
    result = await _run_segment(db, room_id, host_id, tools)
    conflict = result.get("conflict_message") if isinstance(result, dict) else result.conflict_message
    check("Conflict scenario sets a conflict_message", bool(conflict), f"msg={conflict!r}")
    plans = crud.get_plans(db, room_id)
    check("Conflict scenario still returns partial-coverage plan(s) to present",
          len(plans) >= 1, f"{len(plans)} plans")
    db.close()

    print(f"\n{'='*60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        sys.exit(1)
    print("All Phase 4 testing criteria passed.")


if __name__ == "__main__":
    asyncio.run(main())
