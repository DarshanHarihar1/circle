# Circle — Design Docs

**Circle** turns a group's conflicting food cravings into one or two
tap-to-vote plans and places the **real Swiggy order** for everyone — every
restaurant, menu, price, coupon, and order is live via Swiggy's **MCP server**.

## Start here

| Doc | What it covers | Status |
|---|---|---|
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | The system **as built** — pipeline, data model, matching engine, MCP, realtime | ✅ Current — source of truth |
| [`DESIGN.md`](./DESIGN.md) | UI design language (editorial ink-on-canvas system, tokens, components) | ✅ Current |
| [`circle-system-design.md`](./circle-system-design.md) | Original **HLD** — concept, principles, architecture | 📜 Original design (reference) |
| [`circle-lld.md`](./circle-lld.md) | Original **LLD** — schema, routes, agent, MCP, vault | 📜 Original design (reference) |

> The two original design docs predate implementation. Where they differ from
> [`ARCHITECTURE.md`](./ARCHITECTURE.md), **ARCHITECTURE.md is authoritative** —
> see its [§9 Divergences](./ARCHITECTURE.md#9-divergences-from-the-original-hldlld).
> (The phase-wise implementation plan has been removed — it was a build artifact,
> not a design doc.)

## TL;DR architecture

```
Next.js UI ──HTTP──▶ FastAPI (routers + LangGraph agent) ──MCP──▶ Swiggy food server
    ▲                                  │
    └──── Supabase Realtime ───── Supabase Postgres  ◀── source of truth / state machine
```

- **Deterministic planning** — no LLM in the hot path; rule-based feasibility,
  scoring, ranking, and rationale.
- **DB-backed segments** — the LangGraph agent runs as four idempotent,
  restart-safe segments; `rooms.status` is the FSM cursor.
- **Three guarantees** — hard constraints (diet/budget/allergies) → must-have
  dish present → cuisine craving present in every shown plan.
- **Real Swiggy** — live search, menus, cart pricing (`to_pay`), auto-coupons,
  order placement, and tracking via MCP.

## Where the code lives

| Concern | Path |
|---|---|
| Agent graph & segments | `backend/agent/graph.py`, `backend/agent/runner.py` |
| Matching engine | `backend/agent/nodes/feasibility.py`, `backend/agent/resolver.py` |
| MCP helpers | `backend/agent/nodes/cart_mcp.py` |
| API | `backend/routers/rooms.py`, `backend/routers/auth.py` |
| Schema / migrations | `backend/db/models.py`, `backend/db/migrations/` |
| Frontend | `frontend/app/`, `frontend/components/` |
