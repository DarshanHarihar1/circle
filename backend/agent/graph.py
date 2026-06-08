"""
Graph definition for the Circle agent.

Design note — segmented execution
---------------------------------
The agent is driven by three human-in-the-loop gates (approve prefs, choose
plan, confirm order). The canonical LangGraph pattern for this is one graph
compiled with `interrupt_before=[...]` plus a durable checkpointer, resumed
across HTTP requests with `ainvoke(None)`.

However, langgraph 1.2.x's Postgres checkpointer does **not** persist any
checkpoint rows on the resume path (`ainvoke(None)` after a static interrupt) —
verified against both 3.0.5 and 3.1.0; `MemorySaver` works, Postgres writes
nothing — and `langchain-mcp-adapters` pins the whole stack to langgraph 1.x,
so downgrading isn't an option.

So instead of relying on cross-request checkpoint resume, each gate boundary is
a **segment**: a small graph that is invoked once, reconstructs its input
`CircleState` from the durable app tables (rooms / pref_specs / candidates /
plans), runs to its END, and writes its results back to those tables. The app
DB — not an opaque checkpoint blob — is the source of truth, which is both
robust to restarts (re-running a segment is idempotent) and directly queryable
by the frontend.

`build_full_graph()` keeps the complete interrupt-based graph from the LLD for
reference and within-single-invocation tests.
"""
from langgraph.graph import StateGraph, END

from agent.state import CircleState
from agent.nodes import (
    snapshot,
    parse_prefs,
    discover,
    feasibility,
    build_plans,
    price_plans,
    build_cart,
    split_bill,
    confirm_order,
    track,
)


# ── Segment graphs (used by the runner) ───────────────────────────────────────

def build_parse_graph(checkpointer=None):
    """Segment 1 — /activate: snapshot craving cards → parse preferences."""
    b = StateGraph(CircleState)
    b.add_node("snapshot_inputs", snapshot.run)
    b.add_node("parse_preferences", parse_prefs.run)
    b.set_entry_point("snapshot_inputs")
    b.add_edge("snapshot_inputs", "parse_preferences")
    b.add_edge("parse_preferences", END)
    return b.compile(checkpointer=checkpointer)


def build_discover_graph(checkpointer=None):
    """Segment 2 — /approve-prefs: discover → feasibility → build plans → price.

    Produces the ranked, priced plan shortlist and ends at the present_options
    gate (the host then votes / chooses).
    """
    b = StateGraph(CircleState)
    b.add_node("discover", discover.run)
    b.add_node("feasibility", feasibility.run)
    b.add_node("build_plans", build_plans.run)
    b.add_node("price_plans", price_plans.run)
    b.set_entry_point("discover")
    b.add_edge("discover", "feasibility")
    b.add_edge("feasibility", "build_plans")
    b.add_edge("build_plans", "price_plans")
    b.add_edge("price_plans", END)
    return b.compile(checkpointer=checkpointer)


def build_order_graph(checkpointer=None):
    """Segment 3 — /choose-plan: build_cart for current sub_order_idx → split_bill.

    Leaves the cart populated (for place_food_order) and writes per-person split
    amounts to the splits table. Status is set to 'confirming' by the runner once
    this graph returns.
    """
    b = StateGraph(CircleState)
    b.add_node("build_cart", build_cart.run)
    b.add_node("split_bill", split_bill.run)
    b.set_entry_point("build_cart")
    b.add_edge("build_cart", "split_bill")
    b.add_edge("split_bill", END)
    return b.compile(checkpointer=checkpointer)


# ── Full graph (reference / single-invocation tests) ──────────────────────────

def _feasibility_router(state: CircleState) -> str:
    return "build_plans"


def _order_router(state: CircleState) -> str:
    if not state.plans or not state.chosen_plan_id:
        return "track"
    plan = next((p for p in state.plans if p.get("plan_id") == state.chosen_plan_id), None)
    if plan and state.current_sub_order_idx < len(plan.get("sub_orders", [])):
        return "build_cart"
    return "track"


def build_full_graph(checkpointer=None):
    builder = StateGraph(CircleState)

    builder.add_node("snapshot_inputs",    snapshot.run)
    builder.add_node("parse_preferences",  parse_prefs.run)
    builder.add_node("host_confirm_prefs", lambda s, c=None: {})   # interrupt point
    builder.add_node("discover",           discover.run)
    builder.add_node("feasibility",        feasibility.run)
    builder.add_node("build_plans",        build_plans.run)
    builder.add_node("price_plans",        price_plans.run)
    builder.add_node("present_options",    lambda s, c=None: {})   # interrupt point
    builder.add_node("build_cart",         build_cart.run)
    builder.add_node("split_bill",         split_bill.run)
    builder.add_node("confirm_and_order",  lambda s, c=None: {})   # interrupt point
    builder.add_node("track",              track.run)

    builder.set_entry_point("snapshot_inputs")
    builder.add_edge("snapshot_inputs",    "parse_preferences")
    builder.add_edge("parse_preferences",  "host_confirm_prefs")
    builder.add_edge("host_confirm_prefs", "discover")
    builder.add_edge("discover",           "feasibility")
    builder.add_conditional_edges("feasibility", _feasibility_router,
                                  {"build_plans": "build_plans"})
    builder.add_edge("build_plans",        "price_plans")
    builder.add_edge("price_plans",        "present_options")
    builder.add_edge("present_options",    "build_cart")
    builder.add_edge("build_cart",         "split_bill")
    builder.add_edge("split_bill",         "confirm_and_order")
    builder.add_conditional_edges("confirm_and_order", _order_router,
                                  {"build_cart": "build_cart", "track": "track"})
    builder.add_edge("track", END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["host_confirm_prefs", "present_options", "confirm_and_order"],
    )


# Backwards-compatible alias
build_graph = build_full_graph
