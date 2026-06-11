# Circle — Architecture (as-built)

Circle turns a group's conflicting food cravings into one or two tap-to-vote
plans and places the **real Swiggy order** for everyone. Every restaurant,
menu, price, coupon, and order is live — sourced from Swiggy's **MCP server**.

This document describes the system **as it is actually built**. See
[§9 Key design decisions](#9-key-design-decisions) for the rationale behind the
non-obvious choices.

---

## 1. Overview

```
┌─────────────┐         ┌──────────────────────────────┐         ┌──────────────┐
│  Next.js UI │ ──HTTP─▶│        FastAPI backend        │ ──MCP──▶│  Swiggy MCP  │
│ (browser)   │◀─realtime         │  routers + LangGraph agent    │  (OAuth)│  food server │
└─────────────┘         └──────────────┬───────────────┘         └──────────────┘
       ▲                               │
       │  Supabase Realtime            ▼
       └──────────────────────  Supabase Postgres  ◀── source of truth / state machine
```

- **Frontend** — Next.js (App Router, Turbopack), React, Tailwind, framer-motion,
  sonner. Subscribes to **Supabase Realtime** channels for live room updates.
- **Backend** — FastAPI. Two routers (`auth`, `rooms`) plus a **LangGraph agent**
  run as discrete, DB-backed segments.
- **Swiggy MCP** — all food intelligence (search, menus, carts, coupons, order,
  tracking) over streaming HTTP with the host's OAuth token.
- **Supabase Postgres** — the durable **source of truth and state machine**, and
  the realtime transport back to clients.

The planning pipeline is **fully deterministic — no LLM in the hot path** — so it
is fast and can never invent a dish that isn't on the menu. (An OpenRouter LLM is
wired in via `backend/llm.py` for optional/auxiliary use, but parsing, scoring,
ranking, and rationale are all rule-based.)

---

## 2. Request lifecycle & the room FSM

A "room" (a Circle session) is a small finite-state machine. `rooms.status` is
the cursor; the human-in-the-loop gates are transitions on it.

```
collecting ──/activate──▶ planning ──/approve-prefs──▶ choosing
   (fill cards)        (parse prefs,      (discover→feasibility→
                        host reviews)      build_plans→price; vote)
                                                   │
                                          /choose-plan
                                                   ▼
                          ordering ⇄ confirming ──/confirm-order──▶ tracking ──▶ done
                          (build cart, split bill,  (place order(s);   (poll Swiggy
                           host confirms)            multi = sequential) until delivered)
```

| Status | Meaning | Advanced by |
|---|---|---|
| `collecting` | Members are filling craving cards | host `/activate` |
| `planning` | Prefs parsed; host reviews/edits, then approves | host `/approve-prefs` |
| `choosing` | Ranked plans ready; group votes, host chooses | host `/choose-plan` |
| `ordering`/`confirming` | Cart built + bill split; host confirms | host `/confirm-order` |
| `tracking` | Order(s) placed; polling live status | all orders terminal |
| `done` | Delivered / cancelled | — |

---

## 3. The agent: segmented LangGraph

LangGraph 1.2.x's Postgres checkpointer does not persist on the resume path
(`ainvoke(None)` after a static interrupt), and `langchain-mcp-adapters` pins the
stack to langgraph 1.x. So rather than rely on cross-request checkpoint resume,
the agent is split into **four DB-backed segments**. Each segment:

1. reconstructs its input `CircleState` **from the Postgres tables**,
2. runs one graph invocation to `END`,
3. writes results back to the tables (nodes persist as they go).

This makes every segment **idempotent and restart-safe** — re-running it just
recomputes from the same rows. The app DB (not an opaque checkpoint) is the
source of truth, and is directly queryable by the frontend.

> `build_full_graph()` keeps the complete interrupt-based graph from the LLD for
> reference and single-invocation tests; production uses the segment graphs.

### Segments (see `agent/graph.py`, driven by `agent/runner.py`)

| # | Trigger | Graph | Nodes | Writes |
|---|---|---|---|---|
| 1 | `/activate` | `build_parse_graph` | `snapshot_inputs → parse_preferences` | `pref_specs`, status→`planning` |
| 2 | `/approve-prefs` | `build_discover_graph` | `discover → feasibility → build_plans → price_plans` | `candidates`, `plans`, status→`choosing` |
| 3 | `/choose-plan` | `build_order_graph` | `build_cart → split_bill` | `splits`, status→`confirming` |
| 4 | `/confirm-order` | `build_track_graph` | `track` | `placed_orders`, status→`tracking`/`done` |

Segment 2 is the only LLM-free "brain"; segments 2–4 open a **single persistent
MCP session** for the whole segment (re-handshaking per call quadruples HTTP
traffic and trips Swiggy's 429 rate limit).

### Pipeline nodes (segment 2)

- **discover** (`nodes/discover.py`) — expands each member's cuisine vibe into
  Swiggy search queries (`search_restaurants`), filters dish-only results and
  closed restaurants, caps at 12 candidates.
- **feasibility** (`nodes/feasibility.py`) — fetches real paged menus
  (`get_restaurant_menu`), applies hard constraints + soft scoring + the
  craving-coverage gate (see [§5](#5-the-matching-engine)).
- **build_plans** (`nodes/build_plans.py` + `resolver.py`) — builds the
  single- and multi-restaurant options, ranks them, attaches a deterministic
  rationale.
- **price_plans** (`nodes/price_plans.py`) — builds real Swiggy carts
  (`search_menu` → `update_food_cart` → `get_food_cart`) for true `to_pay`
  totals and auto-applies suggested coupons.

---

## 4. Data model (Supabase Postgres)

Postgres is the durable state. The schema is normalized relational, with
**JSONB** where the agent's output is naturally nested (still fully queryable),
`Numeric` for exact money/score math, UUID text PKs (via `pgcrypto`), and
cascading foreign keys. Migrations run through **Alembic** (`db/migrations`).

| Table | Role | Notable columns |
|---|---|---|
| `rooms` | Group session + **FSM cursor** | `status`, `host_user_id`, `address_id` |
| `participants` | Members of a circle | `display_name`, `is_host` |
| `craving_cards` | Raw user input | `veg`, `budget_max`, `cuisine_vibe`, `must_have`, `allergies` (JSONB) |
| `pref_specs` | Parsed / approved prefs | `soft` (JSONB), `must_have`, `excludes`, `approved` |
| `candidates` | Discovered restaurants | `cuisines`/`metadata` (JSONB), `rating` (Numeric) |
| `plans` | Ranked output | `kind`, `sub_orders` (JSONB), `satisfaction` (Numeric), `n_deliveries`, `rank`, `chosen` |
| `plan_votes` | Group voting | composite PK `(participant_id, plan_id)` |
| `placed_orders` | Real Swiggy orders | `swiggy_order_id`, `status`, `sub_order_data` (JSONB) |
| `splits` | Per-person bill | `amount`, `upi_link`, `paid` |
| `host_tokens` | Swiggy OAuth | `encrypted_token` (**BYTEA**), `expires_at` |

Because state lives in normalized tables, the frontend (and analysts) can query
it directly — e.g. `jsonb_array_elements(plans.sub_orders)` to inspect a plan's
restaurants. This was used throughout development to debug ranking.

---

## 5. The matching engine

The core of Circle is `nodes/feasibility.py` + `resolver.py`. It enforces three
guarantees, in order of strength.

### 5.1 Hard constraints (`_check_feasibility`)
An item is **infeasible** (never served to that person) if it violates:
- **diet** — `veg` person never gets non-veg, and vice versa;
- **budget** — price over `budget_max`;
- **allergies** — allergen substring in name/description;
- **excludes** — deal-breaker substring;
- **must-have** — the card promises *"this exact dish lands in your order,"* so an
  item that doesn't contain the must-have can't satisfy that person.

### 5.2 Soft cuisine scoring
Among feasible items, score = fraction of the person's soft (cuisine) tags found
in the item text. Price-efficiency is a **tie-breaker only** — folding it into
the score would let a cheap off-cuisine dish tie a perfect cuisine match.

### 5.3 Craving-coverage gate (the "you get what you crave" rule)
A restaurant only **covers** a person if their best dish actually matches their
**cuisine craving**. The must-have/protein term is *excluded* from this check so
a generic must-have (e.g. `chicken`) can't let a chicken pizza masquerade as a
`biryani` craving. Multi-word vibes (`spicy biryani`) match on a significant word
(`biryani`), mirroring how discover expands phrases.

> Effect: plans that would hand someone the "best available compromise" instead of
> their actual craving are dropped or flagged as a conflict — so **every shown
> plan genuinely satisfies everyone**.

### 5.4 Plan construction & ranking (`resolver.py`)
- **Single-restaurant plans** — one restaurant that covers everyone; ranked by
  blended satisfaction (75% craving match + 25% restaurant rating).
- **Multi-restaurant plan** — give each person their single best placement across
  all restaurants, grouped by restaurant; a genuine multi plan when picks span ≥2
  restaurants.
- **Ranking** — best match first, minus a small **delivery nudge**:
  `effective = satisfaction − 0.05 × (n_deliveries − 1)`. A clearly-better split
  (e.g. 97% vs 79%) wins; a marginally-better one won't beat the convenience of a
  single delivery.
- **Rationale** — deterministic, generated straight from the plan numbers (no
  LLM): *"X + Y covers all N of you in 2 deliveries for ₹Z — 97% match."*

---

## 6. Swiggy MCP integration

All food access goes through `langchain-mcp-adapters` to the Swiggy food MCP over
`streamable_http`, authorized with the host's OAuth bearer token. Helpers live in
`nodes/cart_mcp.py` (structured-content extraction, 429 exponential back-off, one
session per segment).

| Tool | Used in | Purpose |
|---|---|---|
| `search_restaurants` | discover | find candidates per cuisine |
| `get_restaurant_menu` | feasibility | real paged menus for scoring |
| `search_menu` | price/cart | resolve a planned item to a cart-ready id + variants |
| `update_food_cart` / `get_food_cart` | price/cart | build cart, read true `to_pay` |
| `apply_food_coupon` | price/cart | apply Swiggy's auto-suggested coupon |
| `place_food_order` | order | place the real order (per sub-order) |
| `track_food_order` | track | poll live status / ETA |

The MCP **text** response is a lossy human summary; the authoritative payload is
`structuredContent`, which the adapter exposes as `msg.artifact["structured_content"]`.

---

## 7. Realtime & auth

- **Realtime** — the backend POSTs room events to Supabase
  `/realtime/v1/api/broadcast` (service-role key); the frontend
  (`lib/supabase.ts`) subscribes to per-room / per-participant channels. Events:
  member joined, card submitted, plans ready, `auth:expired`, `order:tracking`.
  The UI reacts instead of hammering the API.
- **Auth** — Swiggy OAuth (`routers/auth.py`: `/auth/start`, `/auth/callback`).
  The host's access token is encrypted at rest as `BYTEA` in `host_tokens`
  (`backend/vault.py`) with expiry tracked; a 401 mid-flow broadcasts
  `auth:expired` so the UI prompts a reconnect.

---

## 8. Tech stack

| Layer | Choice |
|---|---|
| Frontend | Next.js (App Router, Turbopack), React, TypeScript, Tailwind, framer-motion, sonner, `@supabase/supabase-js` |
| Backend | FastAPI, Pydantic, SQLAlchemy |
| Agent | LangGraph (segmented), `langchain-mcp-adapters` |
| Food platform | Swiggy MCP (streamable HTTP, OAuth) |
| Data / realtime | Supabase Postgres + Supabase Realtime; Alembic migrations |
| LLM (optional) | OpenRouter (`backend/llm.py`) — not in the planning hot path |

---

## 9. Key design decisions

1. **Segmented, DB-backed execution** — the LangGraph 1.2.x Postgres checkpointer
   doesn't persist on the resume path, so instead of interrupt/checkpoint resume
   the agent runs as four segments that reconstruct state from Postgres. The app
   DB is the source of truth; segments are idempotent and restart-safe.
2. **No LLM in the hot path** — preference parsing, scoring, ranking, and rationale
   are deterministic. This is fast, never hallucinates a dish, and has no
   rate-limit risk. The LLM integration is auxiliary, not required for planning.
3. **Three layered guarantees** — hard constraints (diet/budget/allergies), then a
   **must-have** hard gate, then a **cuisine-coverage** gate — so every shown plan
   gives each person their actual craving rather than a "best available" compromise.
4. **Best-match ranking with a delivery nudge** — `satisfaction − 0.05 × (extra
   deliveries)` — a clearly-better multi-restaurant split wins, a marginal one
   doesn't beat the convenience of a single delivery.
