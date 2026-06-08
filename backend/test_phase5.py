"""
Phase 5 end-to-end test harness — Cart Build + Order Placement.

Tests build_cart (CART_EXPIRED retry, variantGroups, total < 1000), split_bill
(per-person amounts sum to cart total, UPI link format), the confirm gate
(placed_orders empty before host confirms), and place_order_and_advance (writes
placed_orders row with non-null swiggy_order_id). Never calls the real Swiggy
place_food_order endpoint — mocks it to return a fake order ID.

    DATABASE_URL=postgresql://postgres@127.0.0.1:5599/circle_test python test_phase5.py
"""
import asyncio
import json
import os
import sys
import uuid

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@127.0.0.1:5599/circle_test")

from sqlalchemy import text  # noqa: E402

from config import settings  # noqa: E402
from db.session import SessionLocal, engine  # noqa: E402
from db import crud  # noqa: E402
from db.models import Base, PlacedOrder  # noqa: E402
from agent.state import CircleState  # noqa: E402
from agent.graph import build_order_graph  # noqa: E402
from agent.nodes.split_bill import compute_split, make_upi_link  # noqa: E402
from agent.nodes.build_cart import _build_with_retry  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── Mock Swiggy tools ─────────────────────────────────────────────────────────

class MockTool:
    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._fn(args)


def _mk_tools(cart_total=836, cart_expired_first=False, order_id="SW1001"):
    call_counts = {"upd_cart": 0, "get_cart": 0, "flush": 0, "place": 0, "coupon": 0}
    placed_ids: list[str] = []

    def flush(args):
        call_counts["flush"] += 1
        return "Cart flushed."

    def upd_cart(args):
        call_counts["upd_cart"] += 1
        return "Cart updated."

    def get_cart(args):
        call_counts["get_cart"] += 1
        if cart_expired_first and call_counts["get_cart"] == 1:
            return "CART_EXPIRED"
        return json.dumps({
            "total": cart_total,
            "subtotal": cart_total - 40,
            "deliveryFee": 40,
            "discount": 0,
        })

    def apply_coupon(args):
        call_counts["coupon"] += 1
        return "Coupon applied."

    def place_order(args):
        call_counts["place"] += 1
        oid = f"{order_id}_{call_counts['place']}"
        placed_ids.append(oid)
        return json.dumps({"orderId": oid, "status": "placed"})

    return {
        "flush_food_cart": MockTool(flush),
        "update_food_cart": MockTool(upd_cart),
        "get_food_cart": MockTool(get_cart),
        "apply_food_coupon": MockTool(apply_coupon),
        "place_food_order": MockTool(place_order),
    }, call_counts, placed_ids


def _cfg(room_id, db, tools, host_vpa="host@upi"):
    return {
        "configurable": {
            "thread_id": room_id,
            "mcp_tools": tools,
            "db": db,
            "supabase_url": "",
            "supabase_key": "",
            "host_vpa": host_vpa,
        }
    }


def _reset_db():
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    Base.metadata.create_all(engine)


def _seed_plan(db, kind="single"):
    room, host = crud.create_room(db, host_user_id="host-uid", display_name="Darshan")
    crud.set_room_address(db, room.id, "ADDR123")

    pids = [host.id]
    for name in ["Alice", "Bob", "Carol"]:
        g = crud.create_participant(db, room.id, name)
        pids.append(g.id)

    items_r1 = [
        {"participant_id": pids[0], "item_id": "R1-nonveg", "item_name": "Chicken Biryani",
         "price": 279, "variant": None, "addons": []},
        {"participant_id": pids[1], "item_id": "R1-veg", "item_name": "Veg Makhani Wrap",
         "price": 189, "variant": None, "addons": []},
    ]

    if kind == "single":
        items_r1 += [
            {"participant_id": pids[2], "item_id": "R1-paneer", "item_name": "Paneer Tikka",
             "price": 220, "variant": None, "addons": []},
            {"participant_id": pids[3], "item_id": "R1-egg", "item_name": "Egg Roll",
             "price": 99, "variant": None, "addons": []},
        ]
        sub_orders = [{
            "restaurant_id": "R1",
            "restaurant_name": "Spice Junction",
            "items": items_r1,
            "coupon_code": "SWIGGY50",
            "subtotal": 787, "fees": 40, "discount": 0, "total": 827,
        }]
        plan_dict = {
            "plan_id": str(uuid.uuid4()),
            "kind": "single",
            "sub_orders": sub_orders,
            "total": 827,
            "satisfaction": 0.85,
            "n_deliveries": 1,
            "rationale": "Good plan",
            "why_not_runner_up": "Runner up costs more",
            "per_person_fit": {pid: "ok" for pid in pids},
            "notes_uncovered": [],
        }
    else:  # multi
        items_r2 = [
            {"participant_id": pids[2], "item_id": "R2-noodles", "item_name": "Veg Hakka Noodles",
             "price": 199, "variant": None, "addons": []},
            {"participant_id": pids[3], "item_id": "R2-veg", "item_name": "Veg Wrap",
             "price": 189, "variant": None, "addons": []},
        ]
        sub_orders = [
            {
                "restaurant_id": "R1",
                "restaurant_name": "Spice Junction",
                "items": items_r1,
                "coupon_code": None,
                "subtotal": 468, "fees": 40, "discount": 0, "total": 508,
            },
            {
                "restaurant_id": "R2",
                "restaurant_name": "Green Bowl",
                "items": items_r2,
                "coupon_code": None,
                "subtotal": 388, "fees": 40, "discount": 0, "total": 428,
            },
        ]
        plan_dict = {
            "plan_id": str(uuid.uuid4()),
            "kind": "multi",
            "sub_orders": sub_orders,
            "total": 936,
            "satisfaction": 0.9,
            "n_deliveries": 2,
            "rationale": "Multi plan",
            "why_not_runner_up": "Single doesn't cover all",
            "per_person_fit": {pid: "ok" for pid in pids},
            "notes_uncovered": [],
        }

    crud.create_plan(db, room.id, plan_dict)
    crud.set_plan_chosen(db, room.id, plan_dict["plan_id"])
    crud.set_room_status(db, room.id, "ordering")
    return room.id, host.id, pids, plan_dict


async def _run_order_segment(db, room_id, host_id, tools, host_vpa="host@upi"):
    chosen = crud.get_chosen_plan(db, room_id)
    idx = len(crud.get_placed_orders(db, room_id))
    plans_list = crud.plans_as_dicts(db, room_id)
    room = crud.get_room(db, room_id)

    state = CircleState(
        room_id=room_id,
        host_user_id=host_id,
        address_id=room.address_id,
        plans=plans_list,
        chosen_plan_id=chosen.id,
        current_sub_order_idx=idx,
    )
    graph = build_order_graph()
    return await graph.ainvoke(state, _cfg(room_id, db, tools, host_vpa))


async def _simulate_place(db, room_id, host_id, tools, host_vpa="host@upi"):
    """Directly simulates place_order_and_advance logic (no real MCP session)."""
    from agent.nodes.build_cart import _build_with_retry
    from agent.nodes.split_bill import make_upi_link

    chosen = crud.get_chosen_plan(db, room_id)
    idx = len(crud.get_placed_orders(db, room_id))
    sub_orders = chosen.sub_orders
    if idx >= len(sub_orders):
        return
    sub = sub_orders[idx]

    room = crud.get_room(db, room_id)

    # Rebuild cart (may have expired)
    await _build_with_retry(tools, sub, room.address_id)

    # Apply coupon
    coupon = sub.get("coupon_code")
    if coupon and "apply_food_coupon" in tools:
        try:
            await tools["apply_food_coupon"].ainvoke({
                "couponCode": coupon,
                "addressId": room.address_id,
            })
        except Exception:
            pass

    # Place order
    result_raw = await tools["place_food_order"].ainvoke({
        "restaurantId": sub["restaurant_id"],
        "addressId": room.address_id,
    })

    from agent.runner import _parse_swiggy_order_id
    swiggy_order_id = _parse_swiggy_order_id(result_raw)
    crud.create_placed_order(
        db, room_id, chosen.id, swiggy_order_id,
        sub["restaurant_id"], sub["restaurant_name"], sub,
    )

    if idx + 1 < len(sub_orders):
        crud.set_room_status(db, room_id, "ordering")
        # Simulate next build_cart
        await _run_order_segment(db, room_id, host_id, tools, host_vpa)
        crud.set_room_status(db, room_id, "confirming")
    else:
        crud.set_room_status(db, room_id, "tracking")


async def main():
    print(f"\nPhase 5 E2E — DB: {settings.DATABASE_URL}\n")

    # ───────────────────────────────────────────────────────────────────────
    # Unit tests: split math and UPI link utilities
    # ───────────────────────────────────────────────────────────────────────
    print("Unit tests — split_bill utilities")

    sub_sample = {
        "restaurant_id": "R1",
        "items": [
            {"participant_id": "p1", "item_id": "i1", "item_name": "A", "price": 300},
            {"participant_id": "p2", "item_id": "i2", "item_name": "B", "price": 200},
            {"participant_id": "p3", "item_id": "i3", "item_name": "C", "price": 300},
        ],
        "fees": 40,
        "discount": 0,
        "total": 840,
    }
    splits = compute_split(sub_sample)
    total_split = sum(s["amount"] for s in splits)
    check("compute_split: amounts sum exactly to cart total",
          total_split == 840, f"sum={total_split}")

    link = make_upi_link("darshan@upi", 280, "abc123xyz")
    check("UPI link starts with upi://pay?", link.startswith("upi://pay?"))
    check("UPI link has pa= field", "pa=darshan%40upi" in link or "pa=darshan@upi" in link,
          f"link={link}")
    check("UPI link has am= field", "am=280" in link, f"link={link}")
    check("UPI link has cu=INR", "cu=INR" in link, f"link={link}")
    check("UPI link has tn= note", "tn=" in link, f"link={link}")

    # ───────────────────────────────────────────────────────────────────────
    # Scenario A — single-restaurant plan: cart build + split + place
    # ───────────────────────────────────────────────────────────────────────
    print("\nScenario A — single-restaurant plan: cart build + split + place")
    _reset_db()
    db = SessionLocal()
    room_id, host_id, pids, plan_dict = _seed_plan(db, kind="single")
    tools, counts, placed_ids = _mk_tools(cart_total=836)
    await _run_order_segment(db, room_id, host_id, tools)

    check("update_food_cart was called (cart built)", counts["upd_cart"] >= 1)
    check("get_food_cart was called (cart verified)", counts["get_cart"] >= 1)

    # Verify plan total updated via get_food_cart
    chosen = crud.get_chosen_plan(db, room_id)
    sub0 = chosen.sub_orders[0]
    check("Sub-order total updated from get_food_cart (836)", sub0["total"] == 836,
          f"total={sub0['total']}")

    # Verify splits
    splits_rows = crud.get_splits(db, room_id)
    check("Splits written to DB (one per participant)", len(splits_rows) == 4,
          f"count={len(splits_rows)}")
    total_split = sum(s.amount for s in splits_rows)
    check("Per-person amounts sum to cart total", total_split == 836,
          f"sum={total_split}, cart=836")

    # Verify UPI links
    links_with_vpa = [s.upi_link for s in splits_rows if s.upi_link]
    check("UPI links generated for all splits", len(links_with_vpa) == 4,
          f"links={len(links_with_vpa)}")
    for lnk in links_with_vpa:
        check("UPI link format valid", "upi://pay?" in lnk and "cu=INR" in lnk, f"link={lnk}")

    # Coupon was applied (coupon_code = SWIGGY50 on sub_order)
    check("apply_food_coupon called for plan coupon", counts["coupon"] >= 1)

    # Confirm gate: no orders placed yet
    placed = db.query(PlacedOrder).filter_by(room_id=room_id).all()
    check("Confirm gate: placed_orders empty before host confirms", len(placed) == 0)

    # Place order (mock)
    await _simulate_place(db, room_id, host_id, tools)
    placed = db.query(PlacedOrder).filter_by(room_id=room_id).all()
    check("placed_orders row created after confirm", len(placed) == 1)
    check("placed_orders.swiggy_order_id is non-null",
          placed[0].swiggy_order_id is not None, f"id={placed[0].swiggy_order_id}")
    check("room.status = 'tracking' after last order placed",
          crud.get_room(db, room_id).status == "tracking")
    db.close()

    # ───────────────────────────────────────────────────────────────────────
    # Scenario B — CART_EXPIRED retry
    # ───────────────────────────────────────────────────────────────────────
    print("\nScenario B — CART_EXPIRED retry")
    _reset_db()
    db = SessionLocal()
    room_id, host_id, pids, _ = _seed_plan(db, kind="single")
    tools_exp, counts_exp, _ = _mk_tools(cart_total=836, cart_expired_first=True)
    await _run_order_segment(db, room_id, host_id, tools_exp)

    check("CART_EXPIRED: update_food_cart called twice (once per attempt)",
          counts_exp["upd_cart"] == 2, f"upd_cart_calls={counts_exp['upd_cart']}")
    check("CART_EXPIRED: get_food_cart called twice (expired + success)",
          counts_exp["get_cart"] == 2, f"get_cart_calls={counts_exp['get_cart']}")
    sub0_exp = crud.get_chosen_plan(db, room_id).sub_orders[0]
    check("CART_EXPIRED: cart total correct after retry",
          sub0_exp["total"] == 836, f"total={sub0_exp['total']}")
    db.close()

    # ───────────────────────────────────────────────────────────────────────
    # Scenario C — multi-restaurant: second sub-order built only after first placed
    # ───────────────────────────────────────────────────────────────────────
    print("\nScenario C — multi-restaurant: sequential ordering")
    _reset_db()
    db = SessionLocal()
    room_id, host_id, pids, plan_m = _seed_plan(db, kind="multi")
    tools_m, counts_m, placed_m = _mk_tools(cart_total=508, order_id="SW2000")

    # Step 1: build cart for sub_order 0
    await _run_order_segment(db, room_id, host_id, tools_m)
    placed_before = db.query(PlacedOrder).filter_by(room_id=room_id).count()
    check("Multi: placed_orders empty after first cart built (before confirm)",
          placed_before == 0)

    # Step 2: place order 0 — this triggers build cart for sub_order 1
    upd_before_second = counts_m["upd_cart"]
    await _simulate_place(db, room_id, host_id, tools_m)
    placed_after_first = db.query(PlacedOrder).filter_by(room_id=room_id).count()
    check("Multi: first placed_order created after confirm",
          placed_after_first == 1, f"count={placed_after_first}")
    check("Multi: second sub-order cart built only after first order placed",
          counts_m["upd_cart"] > upd_before_second,
          f"upd_before={upd_before_second}, upd_after={counts_m['upd_cart']}")

    # Step 3: place order 1
    await _simulate_place(db, room_id, host_id, tools_m)
    placed_final = db.query(PlacedOrder).filter_by(room_id=room_id).count()
    check("Multi: two placed_orders after both orders confirmed",
          placed_final == 2, f"count={placed_final}")
    check("Multi: room.status = 'tracking' after both orders placed",
          crud.get_room(db, room_id).status == "tracking")
    db.close()

    # ───────────────────────────────────────────────────────────────────────
    # Scenario D — total >= 1000: cart built with warning (not aborted)
    # ───────────────────────────────────────────────────────────────────────
    print("\nScenario D — cart total >= 1000: warning surfaced, cart still built")
    _reset_db()
    db = SessionLocal()
    room_id, host_id, pids, _ = _seed_plan(db, kind="single")
    tools_big, counts_big, _ = _mk_tools(cart_total=1200)
    await _run_order_segment(db, room_id, host_id, tools_big)
    sub0_big = crud.get_chosen_plan(db, room_id).sub_orders[0]
    check("Total >= 1000: cart still built (not aborted)",
          sub0_big["total"] == 1200, f"total={sub0_big['total']}")
    # A warning should have been logged; here we just verify no crash
    check("Total >= 1000: splits still computed", len(crud.get_splits(db, room_id)) == 4)
    db.close()

    print(f"\n{'='*60}\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
        sys.exit(1)
    print("All Phase 5 testing criteria passed.")


if __name__ == "__main__":
    asyncio.run(main())
