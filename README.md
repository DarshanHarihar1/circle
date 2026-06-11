# Circle

**Circle** turns a group's conflicting food cravings into one or two
tap-to-vote plans and places the **real Swiggy order** for everyone — every
restaurant, menu, price, coupon, and order is live via Swiggy's **MCP server**.

A host creates a **room** and shares an invite link; anyone can join and drop in
their craving card (diet, budget, allergies, cuisine, must-have dish). Circle
then deterministically finds one or two restaurant plans that genuinely satisfy
everyone, lets the group vote, and places the real Swiggy order — splitting
across two restaurants when no single place works.

**Highlights**
- **Deterministic planning** — no LLM in the hot path; rule-based feasibility,
  scoring, ranking, and rationale.
- **DB-backed segments** — the LangGraph agent runs as four idempotent,
  restart-safe segments; `rooms.status` is the FSM cursor.
- **Three guarantees** — hard constraints (diet/budget/allergies) → must-have
  dish present → cuisine craving present in every shown plan.
- **Real Swiggy** — live search, menus, cart pricing (`to_pay`), auto-coupons,
  order placement, and tracking via MCP.

---

# Architecture (as-built)

This describes the system **as it is actually built**. See
[Key design decisions](#key-design-decisions) for the rationale behind the
non-obvious choices.

## Overview

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

## Request lifecycle & the room FSM

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

## The agent: segmented LangGraph

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

> `build_full_graph()` keeps the complete interrupt-based graph for reference and
> single-invocation tests; production uses the segment graphs.

**Segments** (see `agent/graph.py`, driven by `agent/runner.py`):

| # | Trigger | Graph | Nodes | Writes |
|---|---|---|---|---|
| 1 | `/activate` | `build_parse_graph` | `snapshot_inputs → parse_preferences` | `pref_specs`, status→`planning` |
| 2 | `/approve-prefs` | `build_discover_graph` | `discover → feasibility → build_plans → price_plans` | `candidates`, `plans`, status→`choosing` |
| 3 | `/choose-plan` | `build_order_graph` | `build_cart → split_bill` | `splits`, status→`confirming` |
| 4 | `/confirm-order` | `build_track_graph` | `track` | `placed_orders`, status→`tracking`/`done` |

Segments 2–4 open a **single persistent MCP session** for the whole segment
(re-handshaking per call quadruples HTTP traffic and trips Swiggy's 429 rate limit).

**Pipeline nodes (segment 2):**

- **discover** (`nodes/discover.py`) — expands each member's cuisine vibe into
  Swiggy search queries (`search_restaurants`), filters dish-only results and
  closed restaurants, caps at 12 candidates.
- **feasibility** (`nodes/feasibility.py`) — fetches real paged menus
  (`get_restaurant_menu`), applies hard constraints + soft scoring + the
  craving-coverage gate (see [The matching engine](#the-matching-engine)).
- **build_plans** (`nodes/build_plans.py` + `resolver.py`) — builds the
  single- and multi-restaurant options, ranks them, attaches a deterministic
  rationale.
- **price_plans** (`nodes/price_plans.py`) — builds real Swiggy carts
  (`search_menu` → `update_food_cart` → `get_food_cart`) for true `to_pay`
  totals and auto-applies suggested coupons.

## Data model (Supabase Postgres)

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
restaurants.

## The matching engine

The core of Circle is `nodes/feasibility.py` + `resolver.py`. It enforces three
guarantees, in order of strength.

**1. Hard constraints (`_check_feasibility`).** An item is **infeasible** (never
served to that person) if it violates:
- **diet** — `veg` person never gets non-veg, and vice versa;
- **budget** — price over `budget_max`;
- **allergies** — allergen substring in name/description;
- **excludes** — deal-breaker substring;
- **must-have** — the card promises *"this exact dish lands in your order,"* so an
  item that doesn't contain the must-have can't satisfy that person.

**2. Soft cuisine scoring.** Among feasible items, score = fraction of the
person's soft (cuisine) tags found in the item text. Price-efficiency is a
**tie-breaker only** — folding it into the score would let a cheap off-cuisine
dish tie a perfect cuisine match.

**3. Craving-coverage gate (the "you get what you crave" rule).** A restaurant
only **covers** a person if their best dish actually matches their **cuisine
craving**. The must-have/protein term is *excluded* from this check so a generic
must-have (e.g. `chicken`) can't let a chicken pizza masquerade as a `biryani`
craving. Multi-word vibes (`spicy biryani`) match on a significant word
(`biryani`).

> Effect: plans that would hand someone the "best available compromise" instead of
> their actual craving are dropped or flagged as a conflict — so **every shown
> plan genuinely satisfies everyone**.

**Plan construction & ranking (`resolver.py`):**
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

## Swiggy MCP integration

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

Auth uses **OAuth Dynamic Client Registration + PKCE** (`routers/auth.py`):
`/auth/start` discovers the OAuth metadata, registers a client at runtime with
`redirect_uris=[SWIGGY_REDIRECT_URI]`, then runs the authorize → callback → token
exchange — so the redirect URI is self-registered, not pre-whitelisted.

## Realtime & auth

- **Realtime** — the backend POSTs room events to Supabase
  `/realtime/v1/api/broadcast` (service-role key); the frontend
  (`lib/supabase.ts`) subscribes to per-room / per-participant channels. Events:
  member joined, card submitted, plans ready, `auth:expired`, `order:tracking`.
  The UI reacts instead of hammering the API.
- **Auth** — Swiggy OAuth (`routers/auth.py`: `/auth/start`, `/auth/callback`).
  The host's access token is encrypted at rest as `BYTEA` in `host_tokens`
  (`backend/vault.py`) with expiry tracked; a 401 mid-flow broadcasts
  `auth:expired` so the UI prompts a reconnect.

## API endpoints

**Auth** (`routers/auth.py`, prefix `/auth`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/auth/start` | Begin Swiggy OAuth (DCR + PKCE) |
| GET | `/auth/callback` | OAuth redirect target; mints the session cookie |

**Rooms** (`routers/rooms.py`, prefix `/rooms`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/rooms` | Create a room (host) |
| GET | `/rooms/{id}` | Room snapshot (participants, status) |
| POST | `/rooms/{id}/address` | Set delivery address |
| GET | `/rooms/{id}/addresses` | List the host's Swiggy addresses |
| POST | `/rooms/{id}/join` | Join as a guest |
| DELETE | `/rooms/{id}/participants/{pid}` | Remove a participant |
| POST | `/rooms/{id}/cards` | Submit a craving card |
| POST | `/rooms/{id}/activate` | Snapshot cards → parse prefs (segment 1) |
| GET | `/rooms/{id}/pref-specs` | Parsed prefs |
| PATCH | `/rooms/{id}/pref-specs/{pid}` | Host edits a parsed pref |
| POST | `/rooms/{id}/approve-prefs` | Approve → discover/plan/price (segment 2) |
| GET | `/rooms/{id}/plans` | Ranked plans + votes |
| POST | `/rooms/{id}/vote` | Vote for a plan |
| POST | `/rooms/{id}/choose-plan` | Host locks plan → build cart + split (segment 3) |
| GET | `/rooms/{id}/splits` | Per-person bill |
| PATCH | `/rooms/{id}/splits/{split_id}` | Mark a split paid |
| GET | `/rooms/{id}/placed-orders` | Placed orders + status |
| POST | `/rooms/{id}/confirm-order` | Place order(s) → track (segment 4) |

**Health** — `GET /health` → `{"status":"ok"}`.

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | Next.js (App Router, Turbopack), React, TypeScript, Tailwind, framer-motion, sonner, `@supabase/supabase-js` |
| Backend | FastAPI, Pydantic, SQLAlchemy |
| Agent | LangGraph (segmented), `langchain-mcp-adapters` |
| Food platform | Swiggy MCP (streamable HTTP, OAuth) |
| Data / realtime | Supabase Postgres + Supabase Realtime; Alembic migrations |
| LLM (optional) | OpenRouter (`backend/llm.py`) — not in the planning hot path |

## Key design decisions

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

---

# Design system

The UI follows an editorial, Wired-magazine-inspired language: a strict
black-on-white duet, tall serif display headlines, a humanist-serif body, a clean
sans for labels, and square geometry. Implemented in Tailwind (`tailwind.config`)
and loaded via `next/font` in `app/layout.tsx`.

**Key characteristics**
- A strict black-and-white duet with **no chromatic accent** except the inline
  link blue (`#057dbc`). The brand reads as a printed magazine.
- Three-face type system — a high-contrast serif for display, a humanist serif for
  body, a humanist sans for metadata and buttons.
- **Square** buttons and inputs (`rounded.none`); the only circular shape is icon
  containers (avatars, share buttons).
- A magazine-style stack separated by 1px hairline dividers; **no drop-shadows** —
  hairlines and surface contrast carry hierarchy.

## Colors

| Role | Token | Value | Use |
|---|---|---|---|
| Brand / Ink | `primary` / `ink` | `#000000` | The only "accent" — wordmark, every CTA, footer fill |
| On-primary | `on-primary` | `#ffffff` | Text on black fills |
| Ink soft | `ink-soft` | `#1a1a1a` | Near-black emphasis (caption-strong, footer links) |
| Body | `body` | `#757575` | Secondary metadata, bylines, supporting lines |
| Hairline | `hairline` | `#e0e0e0` | 1px dividers / input borders — the only "line" |
| Canvas | `canvas` | `#ffffff` | Default page background |
| Canvas soft | `canvas-soft` | `#f5f5f5` | Rare tint (hover, soft surfaces) |
| Link | `link` | `#057dbc` | Inline body links only — never on buttons or nav |

No separate error/success/warning palette — validation uses the ink/body hierarchy.

## Typography

Three families ladder the system (open-source substitutes in parentheses):
1. **Display serif** — *Playfair Display* (weight 400) — hero/section headlines.
2. **Body serif** — *Lora* / Source Serif Pro — long-form body and bylines.
3. **Sans** — *Inter* / Manrope (400/700) — nav, buttons, eyebrows, metadata, captions.

> Serif for narrative, sans for structure. The serif faces never carry button/nav
> labels; the sans never carries article body. Display stays weight 400 — elegance
> comes from the typeface, not bold weight.

| Token | Size | Weight | Line height | Tracking | Use |
|---|---|---|---|---|---|
| `display-hero` | 64px | 400 | 59.5px | -0.5px | Cover headline |
| `display-lg` | 48px | 400 | 50.4px | -0.4px | Major feature headlines |
| `display-md` | 32px | 400 | 35.2px | -0.3px | Story-card large |
| `display-sm` | 26px | 400 | 28px | 0 | Sub-display headings |
| `display-xs` | 20px | 700 | 24px | -0.28px | Sans micro-headings (category callouts) |
| `body-serif-lg` | 19px | 400 | 27.93px | 0.108px | Lead paragraph |
| `body-serif-md` | 16px | 400 | 24px | 0.09px | Default body |
| `body-md` | 17px | 400 | 20px | 0 | Sans body (nav/metadata) |
| `body-md-strong` | 17px | 700 | 22px | -0.144px | Bold sans body |
| `body-sm` | 14px | 400 | 18px | 0.4px | Secondary sans body |
| `body-sm-strong` | 14px | 700 | 18px | 0.4px | Bold caption / nav-link |
| `byline` | 12.73px | 700 | 28px | 0.108px | Article byline (serif) |
| `caption` | 12px | 400 | 16px | 0 | Fine print, image captions |
| `button-md` | 16px | 700 | 20px | 0.3px | Button label |

## Layout

- **Base unit:** 4px. Tokens: `xxs` 2 · `xs` 4 · `sm` 8 · `md` 12 · `lg` 16 ·
  `xl` 20 · `2xl` 24 · `3xl` 32 · `4xl` 48.
- **Section padding:** hero/story grid use `4xl` (48px) top/bottom on desktop.
- **Container:** wide (~1400px max); cover-story grid is 1 large hero + 2-up
  secondary + a vertical stack with hairline dividers.

| Breakpoint | Width | Key changes |
|---|---|---|
| Mobile | < 768px | Hero 64→40px; all grids 1-up; hamburger nav |
| Tablet | 768–1023px | 2-up secondary grid |
| Desktop | ≥ 1024px | Full magazine grid |

Primary buttons render ~44px tall (WCAG AAA touch target).

## Elevation & depth

| Level | Treatment | Use |
|---|---|---|
| 0 — Flat | No shadow, no border | Default — almost every surface |
| 1 — Hairline | 1px solid `hairline` | Row dividers, input borders |
| 2 — Heavy border | 2px solid `ink` | Emphasis CTAs on campaign moments |

No drop-shadows — surface contrast and hairlines carry all hierarchy.

## Shapes

| Token | Value | Use |
|---|---|---|
| `rounded.none` | 0px | Every interactive shape — buttons, inputs, cards |
| `rounded.full` | 9999px | Circular icon containers only (share, avatar) |

## Components

- **`button-primary`** — square black CTA: `primary` bg, `on-primary` text,
  `button-md` label, padding `md xl`, `rounded.none`.
- **`button-outline`** — `canvas` bg, `ink` text, 1px `ink` border; same type/padding/shape.
- **`button-icon-circular`** — `canvas` bg, ink icon, `rounded.full`.
- **`text-input`** — `canvas` bg, `ink` text, 1px `ink` border, `body-md`,
  padding `md lg`, `rounded.none`.
- **`story-card-large` / `story-card`** — cover / secondary cards on `canvas`, no
  border (the photo does the work).
- **`story-row`** — bylined list row with a 1px `hairline` bottom border.
- **`nav-bar` / `nav-link`** — light top nav (`canvas`/`ink`), links in `body-sm-strong`.
- **`hero-band`** — white hero hosting the cover headline in `display-hero`.
- **`footer`** — black band (`primary`/`on-primary`), body in `body-sm`.
- **`hairline-divider`** — the 1px `hairline` rule between rows.

## Do's & Don'ts

**Do**
- Reserve black (`primary`) for the wordmark, every CTA, and the footer fill.
- Set hero headlines in `display-hero` (serif, weight 400).
- Use `rounded.none` (0px) on every button and form input.
- Pair the three faces by role — display serif / body serif / sans labels.
- Use 1px `hairline` dividers as the only elevation cue.

**Don't**
- Introduce a chromatic brand accent (the link blue is for inline body links only).
- Round button corners or add soft drop-shadows.
- Use a sans for display headlines, or push display weight beyond 400.

---

# Deployment

| Component | Host | URL |
|---|---|---|
| Backend (FastAPI) | **Render** (`render.yaml`) | `https://circle-api-bmc6.onrender.com` (health: `/health`) |
| Frontend (Next.js) | **Vercel** | `https://circle-tau-jet.vercel.app` |
| Database / realtime | **Supabase** (managed) | — |

The backend is a Render **web service** (not serverless) because the agent runs
post-response via FastAPI `BackgroundTasks`. Notes:

- **Single instance** — OAuth PKCE/DCR state is in-memory (`routers/auth.py`),
  so don't scale out.
- **Free tier sleeps** after ~15 min idle (~50s cold start) — warm `/health`
  before demoing.
- **Required backend env vars:** `DATABASE_URL`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, `VAULT_FERNET_KEY`, `SECRET_KEY`,
  `OPENROUTER_API_KEY`, `SWIGGY_MCP_BASE_URL`, `SWIGGY_REDIRECT_URI`
  (`https://<backend>/auth/callback`), `FRONTEND_URL` (the Vercel origin, **no
  trailing slash**), `COOKIE_SAMESITE=none`, `COOKIE_SECURE=true`.
- **Frontend env vars (Vercel):** `NEXT_PUBLIC_API_URL` (the Render URL),
  `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`.

**Known constraints for the hosted link:**
- **Swiggy whitelists OAuth redirect URIs.** `localhost` is allowed for dev, but
  the Render host must be added to Swiggy's allowlist before OAuth works in
  production. Until then, run the backend locally for the live order flow.
- **Cross-site session cookie** — with frontend ≠ backend domain the cookie is
  third-party; works in Chrome (allow third-party cookies) but is blocked by
  Safari/Brave. A shared parent domain or token-based auth removes this.

# Where the code lives

| Concern | Path |
|---|---|
| Agent graph & segments | `backend/agent/graph.py`, `backend/agent/runner.py` |
| Matching engine | `backend/agent/nodes/feasibility.py`, `backend/agent/resolver.py` |
| MCP helpers | `backend/agent/nodes/cart_mcp.py` |
| API | `backend/routers/rooms.py`, `backend/routers/auth.py` |
| Schema / migrations | `backend/db/models.py`, `backend/db/migrations/` |
| Frontend | `frontend/app/`, `frontend/components/` |
| Deploy | `render.yaml` (backend on Render); frontend on Vercel |
