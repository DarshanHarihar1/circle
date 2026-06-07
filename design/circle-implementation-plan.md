# Circle — Implementation Plan

Each phase ends with a **milestone** (what you can demo/run) and **testing criteria** (specific things that must pass before moving on). Phases build on each other — do not start Phase N+1 until Phase N testing criteria are fully green.

---

## Design System

**All UI must follow the design specified in [`DESIGN.md`](./DESIGN.md).** The design is a Wired-magazine editorial language — strict black-on-white, no chromatic accents, square geometry. Key rules:

| Token | Value | Rule |
|---|---|---|
| `colors.primary` / `colors.ink` | `#000000` | The only "accent" — wordmark, every CTA, footer fill |
| `colors.canvas` | `#ffffff` | Default page background; never use a tinted or dark canvas |
| `colors.hairline` | `#e0e0e0` | 1 px dividers between sections — the only elevation cue |
| `colors.body` | `#757575` | Secondary metadata, bylines, supporting labels |
| `colors.link` | `#057dbc` | Inline body links only — never on buttons or nav |
| `rounded.none` | `0px` | Every button, input, and card — no rounded corners |
| `rounded.full` | `9999px` | Circular icon containers only (avatars, share buttons) |

**Typography stack** (use open-source substitutes):

| Role | Family | Substitute |
|---|---|---|
| Display headlines | WiredDisplay (proprietary serif) | **Playfair Display** weight 400 |
| Body / bylines | BreveText (humanist serif) | **Lora** or Source Serif Pro |
| Nav, buttons, labels | Apercu (humanist sans) | **Inter** or Manrope weights 400/700 |

**Elevation**: no drop-shadows — use `1px solid #e0e0e0` hairline borders for hierarchy. Modals/toasts use the same flat chrome.

**Do not** introduce any orange, green, purple, or gradient accents. The design operates on a pure ink-on-canvas duet.

---

## Phase 0 — Monorepo Scaffold

**Goal:** `docker compose up` starts everything, `hello world` endpoints respond.

### Work

- Init monorepo at `circle/` with the directory layout from LLD §1
- `backend`: FastAPI skeleton — `main.py`, `config.py`, health endpoint `GET /health`
- `frontend`: Next.js 14 with TypeScript + App Router + Tailwind CSS (`create-next-app`)
- Frontend package installs:
  ```
  npx shadcn@latest init           # component library (Radix + Tailwind)
  npm install framer-motion        # animations
  npm install sonner               # toast notifications
  npm install qrcode.react         # QR code for invite link
  npm install canvas-confetti      # confetti on card submit + delivered
  ```
- Install shadcn components used across all screens:
  ```
  npx shadcn@latest add button card badge progress dialog sheet separator avatar
  ```
- Configure Tailwind theme to match `DESIGN.md` tokens — add to `tailwind.config.ts`:
  - **Fonts**: `display: ['Playfair Display', 'serif']`, `body: ['Lora', 'serif']`, `sans: ['Inter', 'Manrope', 'sans-serif']`
  - **Colors**: extend with `ink: '#000000'`, `canvas: '#ffffff'`, `canvas-soft: '#f5f5f5'`, `hairline: '#e0e0e0'`, `body-muted: '#757575'`, `link: '#057dbc'`
  - **Border-radius**: set `DEFAULT` to `0` (`rounded-none` is the base everywhere); `full` stays `9999px`
  - Load **Playfair Display**, **Lora**, and **Inter** from Google Fonts via `next/font/google` in `app/layout.tsx`
- Override shadcn component defaults to use `rounded-none` and `bg-ink text-canvas` for primary buttons (match `button-primary` from `DESIGN.md`)
- `infra/docker-compose.yml`: Postgres + Supabase local stack
- Alembic wired up: `scripts/migrate.sh` runs all migrations
- All schema from LLD §3 in initial migration
- `.env.example` with all keys from LLD §2
- `scripts/dev.sh`: starts API (uvicorn), frontend (next dev), and docker compose together

### Milestone
Running `./scripts/dev.sh` starts all services. `curl localhost:8000/health` → `{"status":"ok"}`. Next.js page loads at `localhost:3000` with Tailwind styles applied. Postgres tables exist (verified via psql).

### Testing Criteria
- [ ] `GET /health` returns 200
- [ ] All 10 DB tables created by migration (verify with `\dt` in psql)
- [ ] Next.js builds without TS errors (`next build`)
- [ ] shadcn `Button` renders with `rounded-none` and black fill — no rounded corners, no orange (confirm design tokens applied)
- [ ] Playfair Display loads for display text, Inter for body/labels (verify in DevTools → Network → Fonts)
- [ ] `docker compose down && docker compose up` is idempotent

---

## Phase 1 — Auth + MCP Foundation

**Goal:** Host logs in with Swiggy via OAuth, token is stored encrypted, and we can call `get_addresses`.

### Work

- `routers/auth.py`: `GET /auth/start` and `GET /auth/callback`
  - Dynamic Client Registration against Swiggy
  - PKCE generation (verifier, challenge)
  - State stored in server-side session (use `itsdangerous` signed cookie)
  - Token exchange + `vault.store()`
- `vault.py`: Fernet encrypt/decrypt against `host_tokens` table
- `agent/runner.py`: `run_graph` using `MultiServerMCPClient` from `langchain-mcp-adapters` (LLD §7)
- Smoke-test MCP connection: open `MultiServerMCPClient` with stored token, call `get_addresses`, confirm tools load
- `GET /rooms/:id/addresses`: calls `get_addresses` via the adapter, returns parsed address list to frontend
- Frontend: `/auth/callback` page that redirects to `/room/:id` after token stored
- Frontend: "Connect Swiggy" button on landing that calls `GET /auth/start?room_id=...`

### Milestone
Host clicks "Connect Swiggy", completes phone+OTP in browser, is redirected back, and the API can call `get_addresses` returning their real saved addresses.

### Testing Criteria
- [ ] Full OAuth round-trip completes (click → OTP → redirect back) with no 4xx/5xx
- [ ] `host_tokens` table has 1 encrypted row after login
- [ ] `GET /rooms/test/addresses` returns ≥1 address with real Swiggy data
- [ ] Calling `GET /rooms/test/addresses` a second time uses the cached token (no new DCR call)
- [ ] Manually expire the token row in DB → endpoint returns 401 with `re_auth_required: true`

---

## Phase 2 — Room + Participants

**Goal:** Host creates a room, shares a link, guests join and fill craving cards — all visible in real-time.

### Work

- `routers/rooms.py`:
  - `POST /rooms`: create room, return `{room_id, join_url}`
  - `POST /rooms/:id/address`: set `rooms.address_id` (host only, requires auth)
  - `POST /rooms/:id/join`: create participant, broadcast `participant:joined`
  - `DELETE /rooms/:id/participants/:pid`: kick, broadcast `participant:kicked`
  - `POST /rooms/:id/cards`: upsert craving card, broadcast `card:submitted`
  - `GET /rooms/:id`: return room state
- Supabase Realtime broadcast setup in API (service role client)
- `routers/rooms.py` + DB (listed above)
- Supabase Realtime broadcast setup in API (service role client)
- Supabase Realtime subscription in frontend on `room:{room_id}` channel

> **Design:** All screens in this phase must follow `DESIGN.md`. Use `bg-ink text-canvas` (black fill, white text) for primary CTAs, `border border-ink` for outline buttons, `rounded-none` everywhere, `font-display` (Playfair Display) for room-name / section headings, and `font-sans` (Inter) for nav and labels. Hairline dividers (`border-hairline`) between sections. No drop-shadows.

#### Screen 1 — Host Lobby `/room/[id]`

```
┌──────────────────────────────────────────────────┐
│  ● Circle          Bellandur, Bengaluru      [↗] │  ← address pill, edit link
├───────────────────────────┬──────────────────────┤
│                           │  Participants  3 / 4  │
│   [QR CODE  200×200]      │                       │
│                           │  ● Darshan  (you) ✓  │  ← host chip
│   Room code: XKCD-42      │  ● Ananya         ✓  │  ← green check slides in
│   [Copy link]  [Share]    │  ● Rohit          ✓  │    via Framer Motion
│                           │  ○ Meera       ...   │  ← waiting, pulsing dot
│                           │                [✕]   │  ← kick, hover-only
│                           │                       │
│                           │  ████████░░  3 of 4  │  ← shadcn Progress
├───────────────────────────┴──────────────────────┤
│          [Activate when everyone's ready →]       │  ← disabled+grey until 4/4
└──────────────────────────────────────────────────┘
```

- **Participant rows**: `motion.div` with `layout` prop — new joins animate in from bottom, kicked members slide out and shrink
- **Card submitted**: checkmark fades in via `AnimatePresence`; Sonner toast fires `"Ananya submitted her card"`
- **Progress bar**: shadcn `Progress`, updates live on each `card:submitted` event
- **Activate button**: pulses (`scale: [1, 1.03, 1]` loop) only when all cards in; disabled state is greyed with `cursor-not-allowed`
- **Address picker**: shadcn `Sheet` slides up from bottom on click; lists all saved addresses; selected address shown as a pill in the header
- **QR code**: `<QRCodeSVG value={joinUrl} size={200} />` from `qrcode.react`

#### Screen 2 — Guest Craving Card `/join/[id]`

Two-step flow: name entry → craving card. Do not show the full form on step 1.

**Step 1 — Name**
```
┌─────────────────────────────────┐
│         👋 You're invited       │
│      Friday dinner with         │
│         Darshan's circle        │
│                                 │
│   What should we call you?      │
│   [________________________]    │
│                                 │
│          [Join the circle →]    │
└─────────────────────────────────┘
```

**Step 2 — Craving Card** (slides in via `x: "100%"` → `x: 0`)
```
┌─────────────────────────────────┐
│  Your craving card              │
│  ─────────────────────────────  │
│  Diet                           │
│  [Veg]  [Non-veg]  [Either]     │  ← shadcn ToggleGroup, large touch targets
│                                 │
│  Budget per person              │
│  ₹ [_____]  (leave blank = any) │
│                                 │
│  Cuisine vibe                   │
│  [eg. biryani, something light] │  ← large textarea
│                                 │
│  Must have                      │
│  [eg. chicken, extra spicy    ] │
│                                 │
│  ⚠ Allergies  (safety-critical) │  ← red label
│  [peanut ×] [shellfish ×] [+]  │  ← red chip tags
│                                 │
│  Deal breakers                  │
│  [paneer ×] [+]                 │  ← grey chip tags
│                                 │
│        [Submit my card ✓]       │  ← black fill (bg-ink), full-width
└─────────────────────────────────┘
```

- **Allergy field**: red-bordered input + red chip tags — visually distinct from deal-breakers (grey). Label says "⚠ Allergies — these will never appear in your order"
- **Tag input**: press Enter or comma to add a chip; × to remove
- **Submit**: triggers confetti burst (`canvas-confetti`, 1 call) + navigates to a waiting screen showing the host's room status

### Milestone
3 browser tabs (1 host, 2 guests) — guests join, fill cards, host sees live updates with animations. Host can kick a guest and the guest's tab shows "removed from room".

### Testing Criteria
- [ ] Guest join creates a `participants` row and triggers `participant:joined` event in host tab in < 500ms
- [ ] New participant row animates into the host lobby list without a page refresh
- [ ] Progress bar increments immediately when `card:submitted` event fires
- [ ] Activate button is disabled until all participants have submitted; pulses when they all have
- [ ] Allergy chips render in red; deal-breaker chips render in grey
- [ ] Allergies field saves as a JSON array (not a plain string)
- [ ] Card submit triggers confetti burst on the guest tab
- [ ] Sonner toast fires in host tab: "Ananya submitted her card"
- [ ] Host kick: participant slides out of list in host tab AND guest tab shows kicked state
- [ ] `POST /rooms/:id/address` with a non-host token returns 403
- [ ] QR code renders and the scanned URL opens the guest join page correctly
- [ ] **Design check:** No rounded corners on any button or input (`border-radius: 0`); Playfair Display used for the room heading; all CTAs are black-fill with white text; no orange, gradient, or drop-shadow anywhere on these screens

---

## Phase 3 — Agent Part 1: Parse → Discover → Feasibility

**Goal:** `/activate` triggers the LangGraph graph. Agent parses prefs, discovers restaurants, scores feasibility. Host sees parsed pref summary and can approve/edit.

### Work

- `agent/state.py`: full `CircleState` Pydantic model
- `agent/graph.py`: wire up nodes 1–5, compile with PostgresSaver checkpointer
- `agent/nodes/snapshot.py`: reads `craving_cards` from DB for all participants in room
- `agent/nodes/parse_prefs.py`:
  - One OpenRouter call per participant (parallel via `asyncio.gather`)
  - Model: `meta-llama/llama-3.3-70b-instruct`, JSON mode
  - Writes parsed `pref_specs` to DB
  - Broadcasts `prefs:parsed` Realtime event
  - Hits `host_confirm_prefs` interrupt → waits
- `agent/nodes/discover.py`:
  - Builds union of cuisine vibes across participants' soft prefs
  - Calls `tools["search_restaurants"].ainvoke(...)` for each cuisine (deduplicated)
  - Filters `availabilityStatus == OPEN`
  - Caps at 12 candidates, writes to `candidates` table
- `agent/nodes/feasibility.py`:
  - For each candidate, calls `tools["get_restaurant_menu"].ainvoke({restaurantId, addressId})`
  - For each (item, participant) pair, calls `SCORE_MODEL` to get `{feasible, score, reason}`
  - Builds `feasibility_map`: `{restaurant_id: {participant_id: best_item}}`
- `POST /rooms/:id/activate`: enqueues graph run via `BackgroundTasks`, calls `run_graph()` from `agent/runner.py`
- `POST /rooms/:id/approve-prefs`: calls `resume_graph()` which opens a fresh `MultiServerMCPClient` session
- Frontend host view: after `prefs:parsed` event, show pref summary panel with edit controls + "Approve" button

### Milestone
Host hits `/activate` → within 30s, sees a structured pref card per participant (veg, allergies, soft prefs). Host approves → agent discovers restaurants and scores them (no UI needed for this yet, just verify in DB).

### Testing Criteria
- [ ] `parse_preferences` produces valid `PrefSpec` JSON for all participants
- [ ] Allergy field in parsed spec matches exactly what was entered in craving card (spot-check with "peanut")
- [ ] `host_confirm_prefs` interrupt actually stops the graph (verify `placed_orders` is empty and `candidates` table is empty before host approves)
- [ ] After approve: `candidates` table has 8–12 rows, all with `availability = OPEN`
- [ ] `get_restaurant_menu` calls include `addressId` (verify by checking no error in logs)
- [ ] `feasibility_map` in graph state has an entry for every candidate × every participant
- [ ] OpenRouter calls stay under 5s per participant parse (measure in logs)
- [ ] Graph state is checkpointed (kill the API mid-run, restart, graph resumes from last node)

---

## Phase 4 — Resolver: Build Plans + Voting UI

**Goal:** Agent produces a ranked shortlist of ≤3 plans with rationale. Group sees them and votes. Host picks a winner.

### Work

- `agent/resolver.py`: `score_restaurant`, `build_single_plans`, `build_multi_plan`, `rank_plans` (from LLD §10)
- `agent/nodes/build_plans.py`:
  - Calls resolver to produce single + multi plans
  - If conflict: sets `state.conflict_message`, includes a "partial coverage" plan with conflict noted
  - Writes plans to `plans` table
- `agent/nodes/price_plans.py`:
  - For each plan's sub-orders: `update_food_cart` → `get_food_cart` (read total) → `fetch_food_coupons` → `flush_food_cart`
  - Updates plan totals with real cart data
- `agent/nodes/build_plans.py` → generates rationale + why_not_runner_up via `RATIONALE_MODEL`
- Broadcasts `plans:ready` Realtime event
- Hits `present_options` interrupt → waits for vote/pick
- `POST /rooms/:id/vote`: insert `plan_votes`; broadcast `plan:vote`
- `POST /rooms/:id/choose-plan`: sets `plan.chosen = true`; resumes graph
> **Design:** Follow `DESIGN.md`. Plan cards use `story-card-large` chrome: white canvas, black ink, hairline bottom border — no card shadow. Satisfaction progress bar: filled segment `bg-ink`, track `bg-hairline`. Rationale text in `font-body` (Lora) italic at 16 px. Per-person avatar chips: circular (`rounded-full`) — the one exception allowed by the design. Host "Choose Plan" button: `button-primary` (black fill, square). Conflict banner: `1px solid #e0e0e0` amber is NOT in the palette — use a `border-ink` inset box with a `body-muted` label instead.

#### Screen 3 — Plan Reveal + Voting

**Loading state** (shown between `/activate` and `plans:ready` event — can be 20–40s):
```
┌──────────────────────────────────────┐
│                                      │
│   🔍  Finding the best options       │
│       for your circle...             │
│                                      │
│   ░░░░░░░░░░░░░░░░░░  Discovering    │  ← animated step label cycles:
│                        restaurants   │    Discovering → Checking menus
│                                      │    → Scoring → Building plans
└──────────────────────────────────────┘
```
Use a Framer Motion `AnimatePresence` cycling through step labels every ~8s.

**Plan cards** (stagger in: each card `delay = index * 0.15s`, `y: 40 → 0`, `opacity: 0 → 1`):
```
┌──────────────────────────────────────────────────┐
│  🥇  Plan A — Spice Junction          [Vote →]  │
│  ─────────────────────────────────────────────── │
│  ₹820  ·  1 delivery  ·  ~35 min                │
│                                                  │
│  Satisfaction  ████████████░░  88%              │  ← shadcn Progress, green
│                                                  │
│  "One stop covers biryani, veg-light, Chinese,  │
│   and a cheap option — with a COD coupon."      │  ← italic, muted
│                                                  │
│  ▾ Why not Plan B?                               │  ← Collapsible
│    "Plan B costs ₹220 more with a second         │
│     delivery — same satisfaction."              │
│                                                  │
│  Per person:  Ananya · Rohit · Meera · Karan    │  ← avatar chips, hover shows dish
│                                                  │
│  Votes: ●●○  (2 votes)                          │  ← avatar dots animate in live
└──────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────┐
│  🥈  Plan B — Biryani Blues + Green Bowl  [Vote]│
│  …                                               │
└──────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────┐
│  🥉  Plan C — The Biryani Life          [Vote]  │
│  …                                               │
└──────────────────────────────────────────────────┘

         [Choose Plan A for the group]             ← host-only button, appears after
                                                      any vote lands; black fill (bg-ink), full-width
```

- **Vote button**: on tap, button fills with the participant's avatar color + "Voted ✓"; cannot unvote
- **Live votes**: when `plan:vote` event fires, a new avatar dot `AnimatePresence`-appears under the plan — no page refresh
- **Conflict banner**: if `conflict_message` present, show an amber `Alert` above cards: "Meera's constraints couldn't be fully met nearby — her item is flagged for confirmation"
- **Per-person chips**: hover/tap a chip → tooltip shows "Ananya → Veg Makhani Wrap ₹189 · matches: light, veg, under ₹250"
- **Host choose button**: shadcn `Dialog` confirmation before finalising — "This will lock in Plan A for everyone"

### Milestone
End-to-end: craving cards → `/activate` → host approves prefs → agent returns 3 plan cards in the UI with rationale text. Group votes on one. Host confirms selection. `plans.chosen = true` in DB.

### Testing Criteria
- [ ] At least 1 single-restaurant plan and 1 multi-restaurant plan generated for a 4-person group with mixed preferences
- [ ] Each plan has non-empty `rationale` and `why_not_runner_up`
- [ ] `per_person_fit` has an entry for every participant
- [ ] Plan totals match what `get_food_cart` returned (not estimated)
- [ ] If all participants are veg, no non-veg items appear in any plan
- [ ] Conflict scenario: manually set one participant's budget to ₹1 and allergy to a common ingredient → `conflict_message` set, UI shows conflict banner
- [ ] `present_options` interrupt stops the graph (verify `placed_orders` still empty before vote)
- [ ] After `choose-plan`: `plan.chosen = true` in DB, graph proceeds (verify in logs)
- [ ] Voting: 2 votes on Plan A, 1 on Plan B → vote counts correct in UI
- [ ] **Design check:** Plan cards have no drop-shadow; satisfaction bar uses `bg-ink` fill on `bg-hairline` track; rationale text uses Lora serif; "Choose Plan" button is black-fill square; no orange anywhere

---

## Phase 5 — Cart Build + Order Placement

**Goal:** For the chosen plan, agent builds the real cart, splits the bill, and host places the order (single-restaurant first).

### Work

- `agent/nodes/build_cart.py`:
  - Flush cart → `update_food_cart` with `variantGroups` per item → `get_food_cart` to verify
  - Handle `CART_EXPIRED` with 2-retry rebuild (from LLD §12)
  - Assert total < ₹1000; raise if violated
  - `apply_food_coupon` if coupon available from `price_plans`
  - Broadcasts `cart:ready` (custom event)
- `agent/nodes/split_bill.py`: compute per-person amounts + UPI deep links (LLD §11)
- `agent/nodes/confirm_order.py`: hits `confirm_and_order` interrupt, waits for host
- `POST /rooms/:id/confirm-order`: resumes graph → calls `place_food_order`
- After placement: writes `placed_orders` row, writes `splits` rows, broadcasts `order:placed` + `split:ready`
- `_order_router`: for multi-restaurant plan, loops back to `build_cart` for next sub-order
> **Design:** Follow `DESIGN.md`. The cart screen reads like a magazine invoice. Section headings (`font-display`, Playfair Display 32 px) above each participant block. Line items in `font-sans` (Inter) 14 px, muted for amounts. Totals row: `1px solid #e0e0e0` hairline above, bold `font-sans` 17 px. UPI buttons: `button-outline` chrome (white fill, black `1px` border, `rounded-none`). "Place Order" button: `button-primary` (black fill, white text, square). Allergy badge: black `border-ink` inset chip with bold label — no amber/red fill.

#### Screen 4 — Cart Review + Bill Split

```
┌──────────────────────────────────────────────────┐
│  Your order  ·  Spice Junction                   │
│  ─────────────────────────────────────────────── │
│                                                  │
│  Ananya                               ₹184       │
│    Veg Makhani Wrap (1×)         ₹189            │
│                                                  │
│  Rohit                                ₹281       │
│    Chicken Biryani - Serves 1 (1×)   ₹279        │
│    Raita (1×)                          ₹25        │
│                                                  │
│  Meera  ⚠ peanut-free confirmed       ₹252       │
│    Veg Hakka Noodles (1×)            ₹249        │
│                                                  │
│  Karan                                ₹106       │
│    Egg Roll (1×)                      ₹99         │
│                                                  │
│  ─────────────────────────────────────────────── │
│  Subtotal                             ₹836        │
│  Delivery fee                          ₹40        │
│  Coupon (SWIGGY50)                   −₹53        │
│  Total                                ₹823        │
│  ─────────────────────────────────────────────── │
│                                                  │
│  Send payment requests                           │
│  Ananya   ₹184   [Pay Darshan via UPI →]         │  ← outline button (border-ink), opens UPI
│  Rohit    ₹281   [Pay Darshan via UPI →]         │
│  Meera    ₹252   [Pay Darshan via UPI →]         │
│  Karan    ₹106   [Pay Darshan via UPI →]         │
│                                                  │
│           [Place Order]                          │  ← requires scroll to reach;
│                                                  │    shadcn Dialog confirm on tap
└──────────────────────────────────────────────────┘
```

- **Allergy confirmation badge**: any item flagged `needs_confirmation` shows `⚠ peanut-free confirmed` inline — green after participant confirmed, amber if still pending (block "Place Order" if any unconfirmed)
- **UPI buttons**: tap opens the UPI deep link directly; on desktop shows a QR code for the amount in a Dialog
- **Place Order**: sticky at the bottom of the screen on mobile; shadcn `Dialog` shows "Confirm: place COD order for ₹823 at Spice Junction?" before calling the API
- **Multi-restaurant**: if Plan B was chosen, the screen shows two stacked cart sections with a horizontal divider; "Place Order" button is labelled "Place Order 1 of 2" and advances sequentially

### Milestone
Single-restaurant happy path: chosen plan → cart built → split shown → host taps "Place Order" → real Swiggy COD order placed → `placed_orders` row has `swiggy_order_id`.

### Testing Criteria
- [ ] `update_food_cart` with variant items results in non-empty cart (verify via `get_food_cart`)
- [ ] Cart total matches the plan's priced total (within ₹5 for rounding)
- [ ] Total < ₹1000 enforced — if a plan's total is ≥ ₹1000, a warning is shown and host must remove items
- [ ] UPI links are valid format: `upi://pay?pa=...&am=...&tn=...&cu=INR`
- [ ] Per-person amounts sum to the cart total (no money lost/gained in split math)
- [ ] `confirm_and_order` interrupt stops the graph — hitting "Place Order" without host confirm does nothing
- [ ] After order placed: `placed_orders.swiggy_order_id` is non-null
- [ ] Multi-restaurant: second sub-order's cart is built only after first order is placed (sequential, not parallel)
- [ ] CART_EXPIRED simulation: manually call `flush_food_cart` between build_cart and confirm → retry builds a fresh cart
- [ ] **Design check:** Cart screen has a Playfair Display section heading; line items in Inter 14 px; UPI buttons are `button-outline` (white fill, black 1 px border, square); Place Order is `button-primary` (black fill, square); no rounded corners, no drop-shadow, no orange

---

## Phase 6 — Tracking + Polish

**Goal:** Live tracking in the room UI after order placement. Bill split UI complete. Error states handled.

### Work

- `agent/nodes/track.py`:
  - Polls `track_food_order` every 10s (use `asyncio.sleep(10)`)
  - Also polls `get_food_orders` to catch status changes
  - Broadcasts `order:tracking` with status + ETA
  - Stops when status is `Delivered` or `Cancelled`
> **Design:** Follow `DESIGN.md`. The tracking screen uses a horizontal stepper with `bg-ink` (black) filled circles for completed steps, a pulsing `border-ink` ring for the active step, and `bg-hairline` (grey) for upcoming. Status text in `font-display` (Playfair Display) small — the delivery milestone heading is editorial. Split rows use `hairline-divider` between each person. "Delivered" banner: full-width `bg-ink text-canvas` strip (inverted), not a coloured success green. Confetti on delivered is fine (it disappears immediately). No amber, no green UI fill.

#### Screen 5 — Live Tracking

```
┌──────────────────────────────────────────────────┐
│  Spice Junction  ·  Order #237xxx        ~25 min │
│                                                  │
│  ●────────●────────●────────○                   │
│  Accepted   Preparing  Picked up   Delivered     │
│             ↑ active: pulsing orange dot         │
│                                                  │
│  "Your order is being prepared"                  │
│                                                  │
│  ─────────────────────────────────────────────── │
│  Split — mark as received                        │
│                                                  │
│  Ananya   ₹184   [Mark paid ○]                  │
│  Rohit    ₹281   [Mark paid ○]                  │
│  Meera    ₹252   [Mark paid ✓]  ← toggled green  │
│  Karan    ₹106   [Mark paid ○]                  │
│                                                  │
│  Need to cancel? Call 080-67466729               │
└──────────────────────────────────────────────────┘
```

- **Progress stepper**: 4 nodes connected by a line; completed = `bg-ink` filled circle (black); active = pulsing `border-ink` ring (`animate={{ scale: [1, 1.4, 1] }}` loop, 1.5s) on a white fill; upcoming = `bg-hairline` grey
- **ETA chip**: updates on every `order:tracking` event with a `AnimatePresence` number flip (new number slides up, old slides out)
- **Status text**: fades between status strings on each event
- **Mark paid toggles**: shadcn `Switch`; on toggle calls `PATCH /rooms/:id/splits/:id` and turns green immediately (optimistic update)
- **Delivered state**: all 4 nodes fill (`bg-ink`), a full-width `bg-ink text-canvas` banner slides down — "Order delivered. Enjoy." — with `canvas-confetti` burst (confetti is ephemeral, the banner itself stays ink-on-canvas)
- **Multi-restaurant**: two stacked tracker sections, each with its own progress stepper; second one shows as "Waiting to place..." until first order is picked up
- Error handling:
  - 401 from MCP → broadcast `auth:expired` event, show "Host must reconnect Swiggy" in UI
  - `COUPON_REQUIRES_ONLINE_PAYMENT` → skip coupon silently, continue without it
  - `report_error` MCP tool called when unexpected errors occur, report ID logged
- `apply_food_coupon` failure modes handled in `build_cart`: if coupon apply fails, proceed without coupon and log
- Rate limit awareness: if `search_restaurants` fan-out would exceed 12 calls, deduplicate cuisine queries first

### Milestone
Full end-to-end on localhost: room create → guests join → cards → activate → prefs approved → plans shown → vote → choose → cart → split → order placed → live tracking updates in all tabs → "Delivered" status shown.

### Testing Criteria
- [ ] `order:tracking` events arrive in all tabs within 10s of status change
- [ ] Tracking stops broadcasting after `Delivered` (no runaway polling)
- [ ] "Mark as paid" toggle persists to DB and reflects in UI on refresh
- [ ] Cancel scenario: if order is `Cancelled`, show Swiggy care number (080-67466729), not an error
- [ ] Manually revoke the Swiggy token → API returns 401 → UI shows "Host must reconnect" with reconnect button
- [ ] Coupon apply failure: order still placed, coupon simply not applied (no crash)
- [ ] `report_error` is called for unexpected MCP errors — report ID appears in server logs
- [ ] **Design check:** Stepper nodes use black (`bg-ink`) fill for completed, black pulsing ring for active, grey for upcoming — no orange; delivered banner is `bg-ink text-canvas` (inverted), not green; split rows separated by hairline dividers; no rounded corners on toggles or badges

---

## Phase 7 — Demo Prep + Access Request

**Goal:** Localhost demo is polished enough to record a Loom. All known edge cases handled. Ready to submit for Swiggy production access.

### Work

- Run the full user flow from §6 of the system design doc (Darshan + 3 friends scenario) end-to-end
- Fix any rough edges found during dry run
- Confirm allergy warning flow: item marked `needs_confirmation` → guest sees yes/no modal → response stored before order placed
- Add `report_error` call in all catch blocks with structured context
- Review rate limit exposure: count MCP calls per room activation and ensure it stays under 120/min
- Verify cart format for variant items against multiple restaurants (not just Potful)
- Record Loom demo of complete flow
- Fill out access request at `mcp.swiggy.com/builders/access/` with:
  - Integration name: Circle
  - Servers requested: `food`
  - Expected volume: < 1000 req/day initially
  - Demo video link

### Milestone
A recorded demo video showing the complete flow from room creation to order placed + tracking. Submitted to Swiggy access portal.

### Testing Criteria
- [ ] Dry run with 4 real participants (or 4 browser windows) completes without manual intervention beyond the 3 intended human-in-the-loop steps
- [ ] Allergy confirmation flow: participant with peanut allergy sees a flagged item and must confirm before order is placed
- [ ] Multi-restaurant plan (2 sub-orders) placed sequentially without errors
- [ ] No MCP call exceeds 120/min burst (log MCP call timestamps in dev)
- [ ] Demo Loom video is < 5 minutes and shows all 8 steps from §6 of system design
- [ ] Access request submitted with all required fields
- [ ] **Design audit:** Walk every screen against `DESIGN.md`. Verify: (1) no chromatic accent (no orange, blue, green on buttons/nav), (2) Playfair Display renders on all headings, (3) Inter/Manrope on all labels and buttons, (4) zero rounded corners on interactive elements, (5) no drop-shadows on cards or modals, (6) hairline `#e0e0e0` is the only divider used

---

## Summary Table

| Phase | Key deliverable | Gate to next phase |
|---|---|---|
| 0 | Monorepo scaffold + DB | All 4 criteria green |
| 1 | OAuth + MCP foundation | Real addresses returned from Swiggy |
| 2 | Room + craving cards | 3-tab real-time demo works |
| 3 | Parse + discover + feasibility | Candidates in DB, checkpoint verified |
| 4 | Resolver + plans + voting | 3 plans in UI, group can vote |
| 5 | Cart + order placement | Real Swiggy order ID in DB |
| 6 | Tracking + polish | Full end-to-end green on localhost |
| 7 | Demo + access request | Loom submitted to Swiggy |
