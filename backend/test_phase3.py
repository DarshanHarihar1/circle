"""
Phase 3 end-to-end test harness.

Exercises the real agent against a real Postgres database. The two
network-only dependencies — OpenRouter (llm.chat) and the Swiggy food MCP —
are replaced with deterministic mocks returning realistic Swiggy-shaped
payloads, so the graph, nodes, DB writes, segment boundaries and recovery can
be verified offline.

    DATABASE_URL=postgresql://postgres@127.0.0.1:5599/circle_test python test_phase3.py
"""
import asyncio
import json
import os
import sys
import time

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@127.0.0.1:5599/circle_test")

from sqlalchemy import text  # noqa: E402

from config import settings  # noqa: E402
from db.session import SessionLocal, engine  # noqa: E402
from db import crud  # noqa: E402
from db.models import Base, PlacedOrder  # noqa: E402
import agent.nodes.parse_prefs as parse_prefs_mod  # noqa: E402
from agent.state import CircleState  # noqa: E402
from agent.graph import build_parse_graph, build_discover_graph, build_full_graph  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── Mock Swiggy MCP tools ─────────────────────────────────────────────────────

_RESTAURANTS = [
    {"id": f"R{i}", "name": n, "cuisines": cz, "availabilityStatus": "OPEN", "avgRating": 4.2}
    for i, (n, cz) in enumerate([
        ("Spice Junction", ["North Indian", "Biryani"]),
        ("Biryani Blues", ["Biryani", "Mughlai"]),
        ("Green Bowl", ["Healthy", "Salads"]),
        ("Dragon Wok", ["Chinese"]),
        ("The Biryani Life", ["Biryani"]),
        ("Paneer Palace", ["North Indian"]),
        ("Noodle House", ["Chinese", "Asian"]),
        ("Light Bites", ["Healthy", "Continental"]),
        ("Curry Leaf", ["South Indian"]),
        ("Tandoori Nights", ["North Indian", "Tandoori"]),
        ("Wok This Way", ["Chinese"]),
        ("Salad Days", ["Healthy"]),
        ("Biryani Bhavan", ["Biryani"]),
        ("Spicy Affair", ["North Indian"]),
        ("Asian Fusion", ["Chinese", "Thai"]),
    ], start=1)
]
_RESTAURANTS.append({"id": "RCLOSED", "name": "Closed Kitchen", "cuisines": ["X"],
                     "availabilityStatus": "CLOSED"})


def _menu_for(rid):
    return [
        {"id": f"{rid}-veg", "name": "Veg Makhani Wrap", "price": 189, "isVeg": True,
         "description": "light veg wrap, biryani-style spices"},
        {"id": f"{rid}-nonveg", "name": "Chicken Biryani", "price": 279, "isVeg": False,
         "description": "spicy chicken biryani serves 1"},
        {"id": f"{rid}-cheap", "name": "Egg Roll", "price": 99, "isVeg": False,
         "description": "quick egg roll"},
        {"id": f"{rid}-peanut", "name": "Peanut Chaat", "price": 120, "isVeg": True,
         "description": "crunchy peanut snack"},
        {"id": f"{rid}-noodles", "name": "Veg Hakka Noodles", "price": 199, "isVeg": True,
         "description": "chinese veg noodles, light"},
    ]


class MockTool:
    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._fn(args)


def _search_restaurants(args):
    return json.dumps(_RESTAURANTS)


def _get_restaurant_menu(args):
    assert "addressId" in args, "get_restaurant_menu MUST receive addressId"
    assert "restaurantId" in args, "get_restaurant_menu MUST receive restaurantId"
    return json.dumps({"items": _menu_for(args["restaurantId"])})


async def _mock_chat(model, system, user, response_format=None):
    u = user.lower()
    veg = "veg" if "veg/nonveg/either: veg" in u else ("non_veg" if "non_veg" in u else "either")
    allergies = ["peanut"] if "peanut" in u else []
    soft = [kw for kw in ("biryani", "spicy", "light", "chinese", "noodles") if kw in u]
    import re
    m = re.search(r"budget:\s*(\d+)", u)
    return json.dumps({"veg": veg, "budget_max": int(m.group(1)) if m else None,
                       "allergies": allergies, "excludes": [], "soft": soft})


def _tools():
    return {"search_restaurants": MockTool(_search_restaurants),
            "get_restaurant_menu": MockTool(_get_restaurant_menu)}


def _cfg(room_id, db, tools):
    return {"configurable": {"thread_id": room_id, "mcp_tools": tools, "db": db,
                             "supabase_url": "", "supabase_key": ""}}


def seed_room(db):
    room, host = crud.create_room(db, host_user_id="host-uid-test", display_name="Darshan")
    crud.set_room_address(db, room.id, "ADDR123")
    g1 = crud.create_participant(db, room.id, "Alice")
    g2 = crud.create_participant(db, room.id, "Bob")
    g3 = crud.create_participant(db, room.id, "Carol")
    crud.upsert_craving_card(db, host.id, room.id, "either", None, "biryani, something spicy", "", [], [])
    crud.upsert_craving_card(db, g1.id, room.id, "veg", 250, "light, chinese noodles", "", ["peanut"], [])
    crud.upsert_craving_card(db, g2.id, room.id, "non_veg", 300, "biryani", "chicken", [], [])
    crud.upsert_craving_card(db, g3.id, room.id, "either", None, "spicy biryani", "", [], [])
    return room.id, host.id, [host.id, g1.id, g2.id, g3.id]


async def main():
    print(f"\nPhase 3 E2E — DB: {settings.DATABASE_URL}\n")
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)

    db = SessionLocal()
    room_id, host_id, pids = seed_room(db)
    print(f"Seeded room {room_id} with {len(pids)} participants\n")
    parse_prefs_mod.chat = _mock_chat
    tools = _tools()

    # ── Segment 1: parse (the /activate path) ──
    print("Segment 1 — /activate: snapshot → parse_preferences")
    t0 = time.time()
    pg = build_parse_graph()
    await pg.ainvoke(CircleState(room_id=room_id, host_user_id=host_id, address_id="ADDR123"),
                     _cfg(room_id, db, tools))
    parse_elapsed = time.time() - t0

    specs = crud.get_pref_specs(db, room_id)
    check("parse_preferences produced a PrefSpec for every participant",
          len(specs) == len(pids), f"{len(specs)}/{len(pids)}")
    alice = next((s for s in specs if "peanut" in (s.allergies or [])), None)
    check("Allergy 'peanut' preserved exactly in parsed spec",
          alice is not None and list(alice.allergies) == ["peanut"],
          f"alice.allergies={alice.allergies if alice else None}")
    check("All parsed specs have valid veg enum",
          all(s.veg in ("veg", "non_veg", "either") for s in specs))
    check("Soft prefs extracted (non-empty for at least one participant)",
          any(list(s.soft) for s in specs))
    check("OpenRouter parse latency budget (<5s/participant, run in parallel)",
          parse_elapsed < 5.0, f"{parse_elapsed:.2f}s total for {len(pids)}")

    # Gate: before host approval, discover has NOT run
    cands_before = crud.get_candidates(db, room_id)
    orders_before = db.query(PlacedOrder).filter_by(room_id=room_id).all()
    check("Before approval: candidates table is empty (discover gated)",
          len(cands_before) == 0, f"candidates={len(cands_before)}")
    check("Before approval: placed_orders is empty",
          len(orders_before) == 0)
    check("room.status advanced to 'planning' after parse",
          crud.get_room(db, room_id).status == "planning")

    # The full graph's static interrupt also stops at host_confirm_prefs
    # (within a single invocation) — verify the interrupt wiring directly.
    from langgraph.checkpoint.memory import MemorySaver
    fg = build_full_graph(MemorySaver())
    fcfg = {"configurable": {"thread_id": f"{room_id}-full", "mcp_tools": tools, "db": db,
                             "supabase_url": "", "supabase_key": ""}}
    await fg.ainvoke(CircleState(room_id=room_id, host_user_id=host_id, address_id="ADDR123"), fcfg)
    fsnap = await fg.aget_state(fcfg)
    check("Full graph's host_confirm_prefs interrupt stops the graph",
          fsnap.next == ("host_confirm_prefs",), f"next={fsnap.next}")

    # ── Host approves ──
    crud.approve_pref_specs(db, room_id)
    check("Pref specs flip to approved=true on host approval",
          all(s.approved for s in crud.get_pref_specs(db, room_id)))

    # ── Recovery test: reconstruct state purely from the DB (simulates a
    #    process restart between segments — durability via app tables) ──
    print("\nSegment 2 — /approve-prefs: [state reconstructed from DB] discover → feasibility")
    rebuilt = CircleState(
        room_id=room_id,
        host_user_id=crud.get_room(db, room_id).host_user_id,
        address_id=crud.get_room(db, room_id).address_id,
        pref_specs=crud.pref_specs_as_dicts(db, room_id),
    )
    check("CircleState fully reconstructable from DB after a restart",
          len(rebuilt.pref_specs) == len(pids) and rebuilt.address_id == "ADDR123")

    dg = build_discover_graph()
    result = await dg.ainvoke(rebuilt, _cfg(room_id, db, tools))

    cands = crud.get_candidates(db, room_id)
    check("After approve: candidates table has 8–12 rows",
          8 <= len(cands) <= 12, f"{len(cands)} candidates")
    check("All candidates are availability=OPEN (CLOSED filtered out)",
          all(c.availability == "OPEN" for c in cands)
          and all(c.restaurant_id != "RCLOSED" for c in cands))

    menu_tool = tools["get_restaurant_menu"]
    check("get_restaurant_menu always called with addressId",
          len(menu_tool.calls) > 0 and all("addressId" in a for a in menu_tool.calls),
          f"{len(menu_tool.calls)} menu calls")

    fmap = result.get("feasibility_map", {}) if isinstance(result, dict) else result.feasibility_map
    n_specs = len(crud.get_pref_specs(db, room_id))
    check("feasibility_map has an entry for every candidate × participant",
          len(fmap) == len(cands) and all(len(v) == n_specs for v in fmap.values()),
          f"map={len(fmap)} cands={len(cands)} specs={n_specs}")

    # Allergy + veg safety across the whole feasibility map
    alice_pid = next(s.participant_id for s in crud.get_pref_specs(db, room_id)
                     if "peanut" in (s.allergies or []))
    peanut_leaks, veg_violations, covered = [], [], 0
    for rid, per in fmap.items():
        e = per.get(alice_pid, {})
        bi = e.get("best_item") or {}
        if e.get("feasible"):
            covered += 1
        if bi and "peanut" in bi.get("name", "").lower():
            peanut_leaks.append(rid)
        if bi and bi.get("is_veg") is False:
            veg_violations.append(rid)
    check("Allergy-safe: peanut item never chosen for the peanut-allergic guest",
          not peanut_leaks, f"leaks={peanut_leaks}")
    check("Veg constraint honoured for the veg participant's best items",
          not veg_violations, f"violations={veg_violations}")
    check("Feasibility actually found items for the constrained guest (map populated)",
          covered > 0, f"{covered} candidates cover Alice")

    # Idempotent re-run (recovery after a mid-segment crash)
    before_ids = {c.restaurant_id for c in crud.get_candidates(db, room_id)}
    await build_discover_graph().ainvoke(rebuilt, _cfg(room_id, db, _tools()))
    after = crud.get_candidates(db, room_id)
    check("Re-running discover is idempotent (no duplicate candidate rows)",
          len(after) == len(before_ids), f"{len(after)} rows after re-run")

    db.close()
    print(f"\n{'='*60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        sys.exit(1)
    print("All Phase 3 testing criteria passed.")


if __name__ == "__main__":
    asyncio.run(main())
