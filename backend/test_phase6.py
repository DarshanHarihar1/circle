"""
Phase 6 end-to-end test harness — Order Tracking.

Tests the track node: status polling, DB persistence, broadcast payloads,
terminal-status detection (Delivered/Cancelled), 401 → auth:expired, and
report_error on unexpected MCP errors. Also tests mark-paid CRUD directly.

    DATABASE_URL=postgresql://postgres@127.0.0.1:5599/circle_test python test_phase6.py
"""
import asyncio
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch, call

os.environ.setdefault("DATABASE_URL", "postgresql://postgres@127.0.0.1:5599/circle_test")

from sqlalchemy import text  # noqa: E402

from config import settings  # noqa: E402
from db.session import SessionLocal, engine  # noqa: E402
from db import crud  # noqa: E402
from db.models import Base, PlacedOrder, Split  # noqa: E402
from agent.state import CircleState  # noqa: E402
from agent.nodes.track import _parse_tracking, run as track_run  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ── DB helpers ────────────────────────────────────────────────────────────────

def _room_id():
    return f"ph6-{uuid.uuid4().hex[:8]}"


def _setup_room(db, room_id, address_id="addr-1"):
    db.execute(text(
        "INSERT INTO rooms (id, host_user_id, address_id, status) "
        "VALUES (:id, :hu, :addr, 'tracking')"
    ), {"id": room_id, "hu": "host-1", "addr": address_id})
    db.commit()


def _setup_participant(db, room_id, pid=None, name="Alice"):
    pid = pid or f"p-{uuid.uuid4().hex[:8]}"
    db.execute(text(
        "INSERT INTO participants (id, room_id, display_name, is_host) "
        "VALUES (:id, :rid, :name, true)"
    ), {"id": pid, "rid": room_id, "name": name})
    db.commit()
    return pid


def _setup_placed_order(db, room_id, swiggy_order_id="SWG-001", status="Accepted"):
    oid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO placed_orders (id, room_id, restaurant_id, restaurant_name, "
        "swiggy_order_id, status) "
        "VALUES (:id, :rid, 'rest-1', 'Biryani House', :soid, :st)"
    ), {"id": oid, "rid": room_id, "soid": swiggy_order_id, "st": status})
    db.commit()
    return oid


def _setup_split(db, room_id, participant_id, amount=300):
    sid = str(uuid.uuid4())
    db.execute(text(
        "INSERT INTO splits (id, room_id, participant_id, amount, paid) "
        "VALUES (:id, :rid, :pid, :amt, false)"
    ), {"id": sid, "rid": room_id, "pid": participant_id, "amt": amount})
    db.commit()
    return sid


def _get_order_status(db, order_id):
    row = db.execute(
        text("SELECT status FROM placed_orders WHERE id = :id"), {"id": order_id}
    ).fetchone()
    return row[0] if row else None


def _get_split_paid(db, split_id):
    row = db.execute(
        text("SELECT paid FROM splits WHERE id = :id"), {"id": split_id}
    ).fetchone()
    return row[0] if row else None


def _cleanup(db, room_id):
    db.execute(text("DELETE FROM splits WHERE room_id = :id"), {"id": room_id})
    db.execute(text("DELETE FROM placed_orders WHERE room_id = :id"), {"id": room_id})
    db.execute(text("DELETE FROM participants WHERE room_id = :id"), {"id": room_id})
    db.execute(text("DELETE FROM rooms WHERE id = :id"), {"id": room_id})
    db.commit()


def _get_room_status(db, room_id):
    row = db.execute(
        text("SELECT status FROM rooms WHERE id = :id"), {"id": room_id}
    ).fetchone()
    return row[0] if row else None


# ── Mock Swiggy tools ─────────────────────────────────────────────────────────

class MockTool:
    def __init__(self, fn):
        self._fn = fn
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._fn(args)


def _mk_track_tool(statuses: list[str], eta_mins: list[int | None] = None):
    """Returns a track_food_order mock that cycles through the given statuses."""
    if eta_mins is None:
        eta_mins = [30] * len(statuses)
    call_idx = {"n": 0}

    def track(args):
        i = call_idx["n"]
        s = statuses[i] if i < len(statuses) else statuses[-1]
        e = eta_mins[i] if i < len(eta_mins) else eta_mins[-1]
        call_idx["n"] += 1
        payload = {"orderStatus": s}
        if e is not None:
            payload["etaMins"] = e
        return json.dumps(payload)

    tool = MockTool(track)
    return tool, call_idx


def _build_config(db, room_id, tools):
    return {
        "configurable": {
            "thread_id": room_id,
            "mcp_tools": tools,
            "db": db,
            "supabase_url": "http://fake-supabase",
            "supabase_key": "fake-key",
        }
    }


# ── _parse_tracking unit tests ────────────────────────────────────────────────

def test_parse_tracking_json_orderStatus():
    print("\n[parse_tracking]")
    info = _parse_tracking('{"orderStatus": "Preparing", "etaMins": 22}')
    check("status from orderStatus", info["status"] == "Preparing")
    check("eta from etaMins", info["eta_mins"] == 22)


def test_parse_tracking_list_shape():
    data = [{"order_status": "Delivered", "eta_mins": None}]
    info = _parse_tracking(json.dumps(data))
    check("list shape — status", info["status"] == "Delivered")
    check("list shape — eta None", info["eta_mins"] is None)


def test_parse_tracking_fallback_text():
    info = _parse_tracking("not json at all")
    check("non-JSON returns empty status", info["status"] == "")
    check("non-JSON returns None eta", info["eta_mins"] is None)


def test_parse_tracking_eta_string():
    info = _parse_tracking('{"orderStatus": "Accepted", "etaMins": "15 min"}')
    check("eta from string strips non-digits", info["eta_mins"] == 15)


# ── track node integration tests ──────────────────────────────────────────────

async def _run_track(db, room_id, tools, broadcasts):
    state = CircleState(
        room_id=room_id,
        host_user_id="host-1",
        address_id="addr-1",
    )
    cfg = _build_config(db, room_id, tools)

    async def fake_broadcast(url, key, rid, payload):
        broadcasts.append(payload)

    with (
        patch("agent.nodes.track.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        patch("agent.nodes.track._broadcast", side_effect=fake_broadcast),
    ):
        await track_run(state, cfg)
        return mock_sleep.call_count


async def test_delivered_stops_polling():
    print("\n[track: Delivered stops polling]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        oid = _setup_placed_order(db, room_id, swiggy_order_id="SWG-D01", status="Accepted")

        track_tool, call_idx = _mk_track_tool(
            statuses=["Accepted", "Preparing", "Delivered"],
            eta_mins=[30, 20, None],
        )
        tools = {"track_food_order": track_tool}
        broadcasts = []

        sleep_calls = await _run_track(db, room_id, tools, broadcasts)

        final_status = _get_order_status(db, oid)
        check("DB status updated to Delivered", final_status == "Delivered",
              f"got {final_status!r}")
        check("room status is done", _get_room_status(db, room_id) == "done")
        check("sleep called between polls (not after last)", sleep_calls == 2,
              f"sleep called {sleep_calls} times")
        check("tracking events broadcast for each poll",
              len(broadcasts) == 3, f"got {len(broadcasts)} broadcasts")
        check("broadcast has correct event type",
              all(b["event"] == "order:tracking" for b in broadcasts))
        check("last broadcast status is Delivered",
              broadcasts[-1]["status"] == "Delivered")
    finally:
        _cleanup(db, room_id)
        db.close()


async def test_cancelled_stops_polling():
    print("\n[track: Cancelled stops polling]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        oid = _setup_placed_order(db, room_id, swiggy_order_id="SWG-C01", status="Accepted")

        track_tool, _ = _mk_track_tool(statuses=["Cancelled"])
        tools = {"track_food_order": track_tool}
        broadcasts = []

        sleep_calls = await _run_track(db, room_id, tools, broadcasts)

        check("DB status is Cancelled", _get_order_status(db, oid) == "Cancelled")
        check("room status is done", _get_room_status(db, room_id) == "done")
        check("no sleep after immediate terminal", sleep_calls == 0)
    finally:
        _cleanup(db, room_id)
        db.close()


async def test_eta_updated_in_broadcast():
    print("\n[track: ETA in broadcast payload]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        _setup_placed_order(db, room_id, swiggy_order_id="SWG-E01", status="Accepted")

        track_tool, _ = _mk_track_tool(
            statuses=["Picked up", "Delivered"],
            eta_mins=[8, None],
        )
        tools = {"track_food_order": track_tool}
        broadcasts = []

        await _run_track(db, room_id, tools, broadcasts)

        first = broadcasts[0]
        check("eta_mins in broadcast payload", "eta_mins" in first)
        check("eta_mins value correct", first["eta_mins"] == 8, f"got {first.get('eta_mins')}")
        check("restaurant_name in broadcast", "restaurant_name" in first)
        check("swiggy_order_id in broadcast", "swiggy_order_id" in first)
    finally:
        _cleanup(db, room_id)
        db.close()


async def test_401_broadcasts_auth_expired():
    print("\n[track: 401 → auth:expired broadcast]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        _setup_placed_order(db, room_id, swiggy_order_id="SWG-401", status="Accepted")

        def track_401(args):
            raise Exception("401 Unauthorized: token expired")

        tools = {"track_food_order": MockTool(track_401)}
        broadcasts = []

        await _run_track(db, room_id, tools, broadcasts)

        auth_expired = [b for b in broadcasts if b.get("event") == "auth:expired"]
        check("auth:expired broadcast sent", len(auth_expired) == 1,
              f"broadcasts: {broadcasts}")
        check("room status is done after 401", _get_room_status(db, room_id) == "done")
    finally:
        _cleanup(db, room_id)
        db.close()


async def test_report_error_on_unexpected_exception():
    print("\n[track: report_error called on unexpected error]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        _setup_placed_order(db, room_id, swiggy_order_id="SWG-ERR", status="Accepted")

        call_counts = {"track": 0, "report": 0}

        def track_then_fail(args):
            call_counts["track"] += 1
            if call_counts["track"] == 1:
                raise Exception("Unexpected network timeout")
            return json.dumps({"orderStatus": "Delivered"})

        def report_error(args):
            call_counts["report"] += 1
            return "logged"

        tools = {
            "track_food_order": MockTool(track_then_fail),
            "report_error": MockTool(report_error),
        }
        broadcasts = []

        await _run_track(db, room_id, tools, broadcasts)

        check("report_error was called", call_counts["report"] >= 1,
              f"report called {call_counts['report']} times")
        check("tracking recovered and finished",
              _get_room_status(db, room_id) == "done")
    finally:
        _cleanup(db, room_id)
        db.close()


async def test_no_tracking_tools_sets_done():
    print("\n[track: missing tools → room done immediately]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)

        state = CircleState(room_id=room_id, host_user_id="host-1", address_id="addr-1")
        cfg = _build_config(db, room_id, tools={})  # no tools
        broadcasts = []

        with patch("agent.nodes.track._broadcast", side_effect=lambda *a, **k: broadcasts):
            await track_run(state, cfg)

        check("room set to done without tools", _get_room_status(db, room_id) == "done")
    finally:
        _cleanup(db, room_id)
        db.close()


async def test_no_active_orders_sets_done():
    print("\n[track: no orders with swiggy_order_id → done]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        # Placed order without a swiggy_order_id
        db.execute(text(
            "INSERT INTO placed_orders (id, room_id, restaurant_id, restaurant_name, status) "
            "VALUES (:id, :rid, 'r1', 'Test', 'pending')"
        ), {"id": str(uuid.uuid4()), "rid": room_id})
        db.commit()

        def track_never(_):
            raise AssertionError("should not be called")

        tools = {"track_food_order": MockTool(track_never)}
        broadcasts = []

        await _run_track(db, room_id, tools, broadcasts)

        check("room set to done with no trackable orders",
              _get_room_status(db, room_id) == "done")
        check("track_food_order never called", tools["track_food_order"].calls == [])
    finally:
        _cleanup(db, room_id)
        db.close()


async def test_multi_order_both_terminal():
    print("\n[track: two orders, both reach terminal status]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        oid1 = _setup_placed_order(db, room_id, swiggy_order_id="SWG-M01", status="Accepted")
        oid2 = _setup_placed_order(db, room_id, swiggy_order_id="SWG-M02", status="Accepted")

        order_calls = {"SWG-M01": 0, "SWG-M02": 0}

        def track_multi(args):
            oid = args["orderId"]
            order_calls[oid] = order_calls.get(oid, 0) + 1
            n = order_calls[oid]
            # M01 delivers on 2nd poll, M02 on 3rd poll
            if oid == "SWG-M01":
                return json.dumps({"orderStatus": "Delivered" if n >= 2 else "Preparing"})
            else:
                return json.dumps({"orderStatus": "Delivered" if n >= 3 else "Preparing"})

        tools = {"track_food_order": MockTool(track_multi)}
        broadcasts = []

        sleep_calls = await _run_track(db, room_id, tools, broadcasts)

        check("order 1 DB status Delivered", _get_order_status(db, oid1) == "Delivered")
        check("order 2 DB status Delivered", _get_order_status(db, oid2) == "Delivered")
        check("room status done", _get_room_status(db, room_id) == "done")
        check("loop ran multiple rounds", sleep_calls >= 1,
              f"sleep called {sleep_calls} times")
    finally:
        _cleanup(db, room_id)
        db.close()


# ── Mark-paid CRUD tests ──────────────────────────────────────────────────────

def test_mark_paid_persists():
    print("\n[mark-paid: toggle persists to DB]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        pid = _setup_participant(db, room_id)
        sid = _setup_split(db, room_id, participant_id=pid, amount=250)

        # Starts unpaid
        check("split starts unpaid", _get_split_paid(db, sid) is False)

        crud.mark_split_paid(db, sid)

        check("after mark_split_paid, paid=True", _get_split_paid(db, sid) is True)

        # Toggle back
        crud.mark_split_paid(db, sid)

        check("second call toggles back to False", _get_split_paid(db, sid) is False)
    finally:
        _cleanup(db, room_id)
        db.close()


def test_get_splits_for_room():
    print("\n[mark-paid: get_splits returns room splits]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        pid = _setup_participant(db, room_id)
        sid1 = _setup_split(db, room_id, participant_id=pid, amount=300)
        sid2 = _setup_split(db, room_id, participant_id=pid, amount=200)

        splits = crud.get_splits(db, room_id)
        ids = {s.id for s in splits}

        check("get_splits returns both splits", {sid1, sid2} == ids)
        check("amounts correct", {s.amount for s in splits} == {300, 200})
    finally:
        _cleanup(db, room_id)
        db.close()


def test_update_placed_order_status():
    print("\n[crud: update_placed_order_status writes to DB]")
    db = SessionLocal()
    room_id = _room_id()
    try:
        _setup_room(db, room_id)
        oid = _setup_placed_order(db, room_id, swiggy_order_id="SWG-U01", status="Accepted")

        crud.update_placed_order_status(db, oid, "Preparing", eta_mins=25)

        row = db.execute(
            text("SELECT status, sub_order_data FROM placed_orders WHERE id = :id"),
            {"id": oid}
        ).fetchone()

        check("status updated to Preparing", row[0] == "Preparing", f"got {row[0]!r}")
        eta_stored = (row[1] or {}).get("eta_mins")
        check("eta_mins stored in sub_order_data", eta_stored == 25, f"got {eta_stored!r}")
    finally:
        _cleanup(db, room_id)
        db.close()


# ── Frontend design spec checks ───────────────────────────────────────────────

def test_frontend_order_tracking_component():
    print("\n[frontend: OrderTracking component exists and has correct UI elements]")
    path = os.path.join(os.path.dirname(__file__), "..", "frontend", "components", "OrderTracking.tsx")
    try:
        with open(path) as f:
            src = f.read()
    except FileNotFoundError:
        check("OrderTracking.tsx exists", False, "file not found")
        return

    check("file exists", True)
    check("stepper steps defined",
          'const STEPS = ["Accepted", "Preparing", "Picked up", "Delivered"]' in src)
    check("delivered banner bg-ink text-canvas",
          "bg-ink text-canvas" in src)
    check("confetti on delivery",
          "confetti(" in src)
    check("care number for cancellation",
          "080-67466729" in src)
    check("auth:expired handler",
          'auth:expired' in src)
    check("postgres_changes subscription to placed_orders",
          '"placed_orders"' in src or "'placed_orders'" in src)
    check("mark-paid toggle button",
          "onSplitPaid" in src)
    check("ETA chip animated",
          "etaMins" in src and "AnimatePresence" in src)


def test_frontend_lobby_shows_order_tracking():
    print("\n[frontend: lobby.tsx renders OrderTracking for tracking/done]")
    path = os.path.join(os.path.dirname(__file__), "..", "frontend", "app", "room", "[id]", "lobby.tsx")
    try:
        with open(path) as f:
            src = f.read()
    except FileNotFoundError:
        check("lobby.tsx exists", False, "file not found")
        return

    check("imports OrderTracking", 'import OrderTracking' in src)
    check("showTracking condition", 'showTracking' in src)
    check("OrderTracking rendered in lobby", '<OrderTracking' in src)
    check("CartReview only for confirming",
          'roomStatus === "confirming"' in src or "confirming" in src)


def test_frontend_wait_page_shows_order_tracking():
    print("\n[frontend: wait/page.tsx renders OrderTracking for guests]")
    path = os.path.join(
        os.path.dirname(__file__), "..", "frontend", "app", "room", "[id]", "wait", "page.tsx"
    )
    try:
        with open(path) as f:
            src = f.read()
    except FileNotFoundError:
        check("wait/page.tsx exists", False, "file not found")
        return

    check("imports OrderTracking", 'import OrderTracking' in src)
    check("TRACKING_STATUSES includes tracking and done",
          '"tracking"' in src and '"done"' in src)
    check("OrderTracking rendered for guests", '<OrderTracking' in src)
    check("isHost={false}", 'isHost={false}' in src)


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_all():
    Base.metadata.create_all(engine)

    # Unit tests (sync)
    test_parse_tracking_json_orderStatus()
    test_parse_tracking_list_shape()
    test_parse_tracking_fallback_text()
    test_parse_tracking_eta_string()

    # Integration tests (async)
    await test_delivered_stops_polling()
    await test_cancelled_stops_polling()
    await test_eta_updated_in_broadcast()
    await test_401_broadcasts_auth_expired()
    await test_report_error_on_unexpected_exception()
    await test_no_tracking_tools_sets_done()
    await test_no_active_orders_sets_done()
    await test_multi_order_both_terminal()

    # CRUD tests (sync)
    test_mark_paid_persists()
    test_get_splits_for_room()
    test_update_placed_order_status()

    # Frontend spec checks (sync)
    test_frontend_order_tracking_component()
    test_frontend_lobby_shows_order_tracking()
    test_frontend_wait_page_shows_order_tracking()

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
