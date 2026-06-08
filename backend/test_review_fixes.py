"""
Regression tests for the codebase-review fixes.

Covers:
  * runner._find_order_id / _parse_swiggy_order_id — nested & list JSON shapes
  * runner._is_auth_error — 401 detection
  * split_bill — cumulative per-participant split across a multi-restaurant plan
    (the prior code overwrote earlier sub-orders), and idempotency under re-run
  * crud.mark_split_paid — explicit set is idempotent; None still toggles
  * crud.add_plan_vote — switching and re-casting votes is idempotent (no 500)

    DATABASE_URL=postgresql://postgres@127.0.0.1:5599/circle_test python test_review_fixes.py
"""
import asyncio
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@127.0.0.1:5599/circle_test")

from sqlalchemy import text  # noqa: E402

from db.session import SessionLocal, engine  # noqa: E402
from db import crud  # noqa: E402
from db.models import Base  # noqa: E402
from agent.state import CircleState  # noqa: E402
from agent.nodes.split_bill import run as split_run, compute_split  # noqa: E402
from agent.runner import _find_order_id, _parse_swiggy_order_id, _is_auth_error  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── runner: order-id parsing ──────────────────────────────────────────────────

def test_find_order_id():
    print("\n[runner: order-id extraction]")
    check("top-level orderId", _parse_swiggy_order_id('{"orderId": "SW1"}') == "SW1")
    check("nested data.orderId",
          _parse_swiggy_order_id('{"data": {"orderId": "SW2"}}') == "SW2")
    check("deeply nested",
          _parse_swiggy_order_id('{"result": {"order": {"order_id": "SW3"}}}') == "SW3")
    check("list response",
          _parse_swiggy_order_id('[{"swiggyOrderId": "SW4"}]') == "SW4")
    check("regex fallback on plain text",
          _parse_swiggy_order_id("Your order_id: SW5 is confirmed") == "SW5")
    check("none when absent", _parse_swiggy_order_id('{"foo": "bar"}') is None)


def test_is_auth_error():
    print("\n[runner: auth-error detection]")
    check("401 detected", _is_auth_error(Exception("HTTP 401")) is True)
    check("unauthorized detected", _is_auth_error(Exception("Unauthorized")) is True)
    check("other errors not flagged", _is_auth_error(Exception("timeout")) is False)


# ── split_bill: cumulative split across a multi-restaurant plan ────────────────

def _setup_room(db, room_id):
    db.execute(text(
        "INSERT INTO rooms (id, host_user_id, address_id, status) "
        "VALUES (:id, 'h1', 'addr', 'confirming')"
    ), {"id": room_id})
    pids = {}
    for name in ("alice", "bob", "carol"):
        pid = f"{name}-{uuid.uuid4().hex[:6]}"
        db.execute(text(
            "INSERT INTO participants (id, room_id, display_name, is_host) "
            "VALUES (:id, :rid, :n, false)"
        ), {"id": pid, "rid": room_id, "n": name})
        pids[name] = pid
    db.commit()
    return pids


def _cleanup(db, room_id):
    db.execute(text("DELETE FROM splits WHERE room_id = :id"), {"id": room_id})
    db.execute(text("DELETE FROM participants WHERE room_id = :id"), {"id": room_id})
    db.execute(text("DELETE FROM rooms WHERE id = :id"), {"id": room_id})
    db.commit()


def _splits_map(db, room_id):
    return {s.participant_id: s.amount for s in crud.get_splits(db, room_id)}


async def test_multi_restaurant_cumulative_split():
    print("\n[split_bill: multi-restaurant cumulative split]")
    db = SessionLocal()
    room_id = f"rev-{uuid.uuid4().hex[:8]}"
    try:
        pids = _setup_room(db, room_id)
        a, b, c = pids["alice"], pids["bob"], pids["carol"]

        # A multi plan: alice+bob order from R1, bob+carol from R2.
        # bob appears in BOTH — the old code would overwrite bob's R1 share.
        sub0 = {
            "restaurant_id": "r1", "restaurant_name": "R1", "total": 300,
            "items": [
                {"participant_id": a, "item_name": "x", "price": 100},
                {"participant_id": b, "item_name": "y", "price": 200},
            ],
        }
        sub1 = {
            "restaurant_id": "r2", "restaurant_name": "R2", "total": 400,
            "items": [
                {"participant_id": b, "item_name": "z", "price": 150},
                {"participant_id": c, "item_name": "w", "price": 250},
            ],
        }
        plan = {
            "plan_id": "p1", "kind": "multi",
            "sub_orders": [sub0, sub1], "total": 700,
        }

        cfg = {"configurable": {"db": db, "host_vpa": ""}}

        # Build cart for sub-order 0 → split runs at idx 0
        state0 = CircleState(room_id=room_id, host_user_id="h1", address_id="addr",
                             plans=[plan], chosen_plan_id="p1", current_sub_order_idx=0)
        await split_run(state0, cfg)
        m0 = _splits_map(db, room_id)
        check("idx0: alice owes her R1 share", m0.get(a) == 100, f"got {m0.get(a)}")
        check("idx0: bob owes his R1 share", m0.get(b) == 200, f"got {m0.get(b)}")
        check("idx0: sub-order 0 sums to 300", sum(m0.values()) == 300, f"got {sum(m0.values())}")

        # Build cart for sub-order 1 → split runs at idx 1 (cumulative)
        state1 = CircleState(room_id=room_id, host_user_id="h1", address_id="addr",
                             plans=[plan], chosen_plan_id="p1", current_sub_order_idx=1)
        await split_run(state1, cfg)
        m1 = _splits_map(db, room_id)
        check("idx1: bob's share accumulates across both orders",
              m1.get(b) == 200 + 150, f"got {m1.get(b)}")
        check("idx1: carol owes her R2 share", m1.get(c) == 250, f"got {m1.get(c)}")
        check("idx1: alice unchanged", m1.get(a) == 100, f"got {m1.get(a)}")
        check("idx1: total across all participants == plan total (700)",
              sum(m1.values()) == 700, f"got {sum(m1.values())}")

        # Re-run idx1 (simulating a retry) — must be idempotent, not double-count
        await split_run(state1, cfg)
        m2 = _splits_map(db, room_id)
        check("re-run idx1 is idempotent (bob still 350)", m2.get(b) == 350, f"got {m2.get(b)}")
        check("re-run idx1 total still 700", sum(m2.values()) == 700, f"got {sum(m2.values())}")
    finally:
        _cleanup(db, room_id)
        db.close()


def test_single_restaurant_split_unchanged():
    print("\n[split_bill: single-restaurant exact sum preserved]")
    sub = {
        "restaurant_id": "r1", "total": 1000,
        "items": [
            {"participant_id": "a", "item_name": "x", "price": 333},
            {"participant_id": "b", "item_name": "y", "price": 333},
            {"participant_id": "c", "item_name": "z", "price": 334},
        ],
    }
    splits = compute_split(sub)
    total = sum(s["amount"] for s in splits)
    check("single-restaurant split sums exactly to cart total", total == 1000, f"got {total}")


# ── crud: mark_split_paid ─────────────────────────────────────────────────────

async def test_mark_split_paid_explicit():
    print("\n[crud: mark_split_paid explicit set is idempotent]")
    db = SessionLocal()
    room_id = f"rev-{uuid.uuid4().hex[:8]}"
    try:
        pids = _setup_room(db, room_id)
        sid = str(uuid.uuid4())
        db.execute(text(
            "INSERT INTO splits (id, room_id, participant_id, amount, paid) "
            "VALUES (:id, :rid, :pid, 100, false)"
        ), {"id": sid, "rid": room_id, "pid": pids["alice"]})
        db.commit()

        r1 = crud.mark_split_paid(db, sid, paid=True)
        check("explicit set True returns True", r1 is True)
        r2 = crud.mark_split_paid(db, sid, paid=True)  # retry
        check("repeated set True stays True (idempotent)", r2 is True)

        r3 = crud.mark_split_paid(db, sid)  # None → toggle
        check("None toggles True→False", r3 is False)

        r4 = crud.mark_split_paid(db, "no-such-id", paid=True)
        check("missing split returns None", r4 is None)
    finally:
        _cleanup(db, room_id)
        db.close()


# ── crud: add_plan_vote idempotency ───────────────────────────────────────────

def _setup_plans(db, room_id, n=2):
    ids = []
    for i in range(n):
        pid = f"plan-{uuid.uuid4().hex[:6]}"
        db.execute(text(
            "INSERT INTO plans (id, room_id, kind, sub_orders, total, n_deliveries, chosen) "
            "VALUES (:id, :rid, 'single', '[]'::jsonb, 100, 1, false)"
        ), {"id": pid, "rid": room_id})
        ids.append(pid)
    db.commit()
    return ids


def test_add_plan_vote_idempotent():
    print("\n[crud: add_plan_vote — switch & re-cast are safe]")
    db = SessionLocal()
    room_id = f"rev-{uuid.uuid4().hex[:8]}"
    try:
        pids = _setup_room(db, room_id)
        plans = _setup_plans(db, room_id, 2)
        voter = pids["alice"]

        crud.add_plan_vote(db, voter, plans[0])
        crud.add_plan_vote(db, voter, plans[0])  # re-cast same — should not error/dup
        votes = crud.get_votes(db, room_id)
        mine = [v for v in votes if v["participant_id"] == voter]
        check("re-casting same vote keeps exactly one", len(mine) == 1, f"got {len(mine)}")
        check("vote is on plan 0", mine and mine[0]["plan_id"] == plans[0])

        crud.add_plan_vote(db, voter, plans[1])  # switch
        votes = crud.get_votes(db, room_id)
        mine = [v for v in votes if v["participant_id"] == voter]
        check("switching vote still exactly one", len(mine) == 1, f"got {len(mine)}")
        check("vote moved to plan 1", mine and mine[0]["plan_id"] == plans[1])
    finally:
        db.execute(text("DELETE FROM plan_votes WHERE participant_id IN "
                        "(SELECT id FROM participants WHERE room_id = :id)"), {"id": room_id})
        db.execute(text("DELETE FROM plans WHERE room_id = :id"), {"id": room_id})
        _cleanup(db, room_id)
        db.close()


async def run_all():
    Base.metadata.create_all(engine)

    test_find_order_id()
    test_is_auth_error()
    await test_multi_restaurant_cumulative_split()
    test_single_restaurant_split_unchanged()
    await test_mark_split_paid_explicit()
    test_add_plan_vote_idempotent()

    print(f"\n{'='*60}")
    print(f"  PASSED: {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print(f"  FAILED: {len(FAIL)}")
        for f in FAIL:
            print(f"    ✗ {f}")
    else:
        print("  All checks passed.")
    return len(FAIL) == 0


if __name__ == "__main__":
    ok = asyncio.run(run_all())
    sys.exit(0 if ok else 1)
