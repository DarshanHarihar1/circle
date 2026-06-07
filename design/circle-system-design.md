# Circle — Group Food Ordering on Swiggy MCP

**Working name:** Circle (alt: Gather, Loop, Potluck)
**One-liner:** A shareable room where friends drop their cravings, an agent works out *who eats what from where*, and presents the group a ranked set of order plans to choose from — then places it. Decision *support*, not a decision *maker*.

**Status:** Design v1 · Target platform: Swiggy MCP (Food server, optionally Dineout)

---

## 1. Concept

Ordering food for a group is a coordination mess: collecting everyone's cravings, reconciling diets/budgets/allergies, picking a place that works for all, and splitting the bill. Circle turns that into a single shared session.

- One person (the **host**) authenticates with Swiggy once.
- Everyone else joins via a **link**, enters a name, and submits what they want — no Swiggy account, no app install.
- When inputs are in, the host runs **`/activate`**. An agent gathers everything, the host confirms/edits, and the **resolver** produces a ranked shortlist of order plans.
- The group **chooses** a plan. The host gives a final confirm, and the order(s) are placed (COD).

**Why this idea:** the growth loop is built in. Every order is a 3–5 person event, and most of those people are touching the product for the first time *because a friend invited them*. Acquisition is the core action, not a bolt-on.

---

## 2. Design principles

1. **Options, not decisions.** The agent never silently picks. It ranks plans and explains trade-offs (price vs. satisfaction vs. number of deliveries). Humans choose.
2. **Hard vs. soft constraints.** Allergies and diet are *hard* (pass/fail filters). "Wants biryani", "spicy", "cheap" are *soft* (optimize, trade off if needed). An allergy is never treated as a preference.
3. **Human in the loop at money + safety gates.** Preference confirmation, plan selection, and order placement are all explicit human steps. This is also mandated by the Swiggy docs for `place_food_order`.
4. **Single source of truth is server-side.** Never cache cart state in agent memory; always re-read before mutating or confirming.
5. **Free/OSS-first.** Every component has a free tier or self-hostable OSS option.

---

## 3. Platform constraints that shape the design

These are derived from the Swiggy MCP docs and are non-negotiable for v1.

| Constraint | Source | Design consequence |
|---|---|---|
| **One cart = one restaurant; switching auto-flushes** | multi-turn-state doc | Multi-restaurant plans are executed as **sequential** single-restaurant orders in the host session, not one combined cart. |
| **Cart is server-side, session-keyed, has a TTL** | multi-turn-state doc | Re-read `get_food_cart` at every boundary; handle `CART_EXPIRED` by rebuilding. Never cache. |
| **Orders ≥ ₹1000 are blocked (beta)** | place_food_order doc | Per sub-order must stay < ₹1000. Detect at cart-build; trim or split into more sub-orders; warn the host. |
| **Payment auto-selected; effectively COD** | place_food_order / get_food_cart | Bill split is **informational**: host fronts the money, friends repay out-of-band (UPI deep links). Cannot collect per-person payment via MCP. |
| **Coupons may require online payment** | fetch_food_coupons / errors (`COUPON_REQUIRES_ONLINE_PAYMENT`) | Only consider COD-eligible coupons. `fetch_food_coupons` already filters for COD — respect it. |
| **Auth is OAuth 2.1 + PKCE, phone+OTP; whitelist-only prod** | authenticate / delegated-auth / quickstart | Host connects their own Swiggy. Dev runs on `localhost` (allowed) before access is granted. |
| **Access token 5 days; session 30 days sliding** | delegated-auth | Silent re-auth most of the time; treat any 401 as "re-run authorize". No refresh tokens in v1. |
| **Rate limits (planned): 120 req/min/user/server, 30 writes/min, 50k/day/client_id** | rate-limits | Cap resolver fan-out (don't pull menus for 50 restaurants — cap ~8–12 candidates). Don't poll `track_*` faster than 10s. Cache addresses + restaurant metadata. |
| **No non-veg-only filter** | search_menu | `vegFilter=1` for veg-only; for "non-veg only" pass 0 and filter in our own logic. |
| **Cancellation has no tool** | place_food_order doc | If a user wants to cancel, surface Swiggy customer care (080-67466729). Do not attempt via MCP. |
| **No Zomato MCP** | platform scope | Stay within Swiggy. No cross-platform price comparison. |

---

## 4. System architecture

### 4.1 Components

```
┌──────────────────────────────────────────────────────────────────┐
│                          Client (web app)                          │
│  Host view: connect Swiggy · invite/kick · /activate · confirm     │
│  Guest view: enter name · craving card + chat · vote on plans      │
└───────────────┬───────────────────────────────┬──────────────────┘
                │ realtime (websocket)           │ REST
                ▼                                 ▼
┌──────────────────────────────┐    ┌────────────────────────────────┐
│   Realtime / Room service     │    │      App backend (FastAPI)      │
│  room state, presence, chat,  │◄──►│  rooms, participants, prefs,    │
│  craving cards, plan voting   │    │  /activate orchestration entry  │
└──────────────────────────────┘    └───────────────┬────────────────┘
                                                     │
                          ┌──────────────────────────┼───────────────────────┐
                          ▼                          ▼                        ▼
              ┌────────────────────┐    ┌────────────────────┐    ┌──────────────────────┐
              │  LangGraph agent    │    │  Token vault        │    │  Swiggy MCP client   │
              │  (the orchestrator) │◄──►│  (encrypted host    │◄──►│  food server (+ auth)│
              │                     │    │   Swiggy tokens)    │    │                      │
              └────────────────────┘    └────────────────────┘    └──────────────────────┘
                          │
                          ▼
                ┌────────────────────┐
                │  LLM via OpenRouter │  (Llama 3.3 70B / 8B / Claude Haiku)
                └────────────────────┘
```

### 4.2 The room / session model

- A **Room** has: `room_id`, `host_user_id`, `address_id` (host's chosen Swiggy delivery address), status (`collecting → activated → planning → choosing → ordering → tracking → done`), and a list of **Participants**.
- A **Participant** has: `participant_id`, `display_name`, `inputs` (chat messages + a structured **craving card**), and a derived `pref_spec`.
- **Host controls:** invite (share link / QR), kick participant, set delivery address, `/activate`, confirm prefs, final order confirm.
- **Guests** need no auth. They only write to room state in our backend.

**Host onboarding (address selection):** immediately after the host connects Swiggy, call `get_addresses` and present *all* saved addresses (the tool returns them sorted by last order date). The host picks one as the room's delivery `address_id` before inviting anyone. This is a hard prerequisite — every downstream call (`search_restaurants`, `search_menu`, `fetch_food_coupons`, `get_food_cart`, `place_food_order`) requires that `addressId`. If the host has no saved address, fall back to Instamart's `create_address` (shared across Food + Instamart) to add one.

**Input mode (resolving your "not sure about chat" question):**
Use **two channels, one source of truth**:
- A structured **craving card** per guest — this is authoritative. Fields: `name`, `veg/non-veg/either`, `budget_max`, `cuisine_vibe` (free text), `must_have` (free text), `allergies` (structured, explicit), `deal_breakers`. Allergies live in their own field, never buried in chat — that's the safety-critical one.
- An optional **freeform chat** for banter and nuance ("let's do something light, we ate heavy at lunch"). The parser reads it as *soft* signal only.

Recommendation: ship the craving card in v1 (deterministic, safe, easy to parse), add chat as a "vibe" layer that enriches soft preferences. Never let an allergy be expressed *only* in chat.

### 4.3 Auth model

**v1 — host-only auth (recommended).** Only the host runs the Swiggy OAuth 2.1 + PKCE flow (delegated-auth pattern). The host's scoped access token is stored encrypted in the token vault. All MCP calls run on the host's behalf. Guests never authenticate. Host fronts payment (COD), split is informational.

**v2 — everyone authenticates (advanced, optional).** Each participant connects their own Swiggy via delegated auth. This *natively* solves two things: multi-restaurant orders can run in parallel (each from a different account) and each person pays their own COD sub-order — no out-of-band settlement. Cost: more friction (everyone does phone+OTP once) and you lose single-delivery-fee savings. Offer it as a toggle for groups who want clean per-person payment.

### 4.4 The agent pipeline (LangGraph)

A directed graph with three **human-in-the-loop interrupts** (host confirm prefs, group choose plan, host confirm order). Nodes:

| # | Node | Type | What it does | MCP tools |
|---|---|---|---|---|
| 1 | `snapshot_inputs` | plain | On `/activate`, freeze each participant's craving card + chat into raw inputs. | — |
| 2 | `parse_preferences` | LLM | Normalize each participant → `PreferenceSpec` with hard vs. soft split. Batched/parallel. | — |
| 3 | `host_confirm_prefs` | **interrupt** | Show parsed prefs to host; host edits/approves. | — |
| 4 | `discover` | tools | Get host address; search restaurants across the *union* of cuisines; cap candidates (~8–12). Filter `availabilityStatus = OPEN`. | `get_addresses`, `search_restaurants` |
| 5 | `feasibility` | tools + LLM | For each candidate, pull menu; map each participant to candidate dish(es); score hard-constraint coverage + soft satisfaction. Pass both `restaurantId` and `addressId` to `get_restaurant_menu`. | `search_menu`, `get_restaurant_menu` |
| 6 | `build_plans` | logic | Assemble candidate **plans**: best single-restaurant plan(s) + the best multi-restaurant combo that reaches full coverage. Annotate each with totals, satisfaction %, #deliveries, and a generated rationale (see §4.6). | — |
| 7 | `price_plans` | tools | For each plan's sub-orders, build a draft cart, read totals, fetch best COD coupon. (Read-only pricing; carts are rebuilt at order time.) | `update_food_cart`, `get_food_cart`, `fetch_food_coupons`, `apply_food_coupon` |
| 8 | `present_options` | **interrupt** | Present ranked shortlist (e.g. top 3) to the group, each with a plain-language *why this fits everyone* and a contrastive *why not the runner-up*; collect votes / host pick. | — |
| 9 | `build_cart` | tools | For the chosen plan, build each sub-order's cart **sequentially** (cart binds to one restaurant). Re-read cart after each mutation. | `update_food_cart`, `get_food_cart`, `apply_food_coupon` |
| 10 | `split_bill` | logic | Compute per-person share (own items + pro-rata fees − pro-rata discount). Generate UPI deep links. | — |
| 11 | `confirm_and_order` | **interrupt → tools** | Host final confirm per sub-order; assert each < ₹1000; place. Handle `CART_EXPIRED` by rebuilding. | `get_food_cart`, `place_food_order` |
| 12 | `track` | tools | Poll order status (≤ every 10s), broadcast to the room. | `get_food_orders`, `track_food_order`, `get_food_order_details` |

**Conditional edges (the interesting ones):**
- `feasibility → build_plans`: if **no single restaurant** covers all hard constraints, the multi-restaurant branch is the *only* full-coverage option — surface it explicitly rather than erroring.
- `build_plans → present_options`: if **no plan** can satisfy a hard constraint for someone (e.g. a niche allergy + tiny budget), don't fail silently — present the conflict to the group as a decision ("Meera's constraints can't be met nearby — proceed without her item, or change restaurant?").
- `confirm_and_order`: multi-restaurant plans loop node 9→11 **once per sub-order, sequentially**, because each cart flush destroys the previous one.

### 4.5 State schema (illustrative, Pydantic)

```python
class PreferenceSpec(BaseModel):
    participant_id: str
    display_name: str
    veg: Literal["veg", "non_veg", "either"]
    budget_max: int | None            # rupees
    allergies: list[str]              # HARD
    excludes: list[str]               # HARD (e.g. "paneer")
    soft: list[str]                   # ["biryani", "spicy", "light", "cheapest"]
    raw_chat: str = ""

class SubOrderItem(BaseModel):
    participant_id: str
    item_id: str
    item_name: str
    variant: dict | None              # variations OR variantsV2 — never both
    addons: list[dict] = []
    price: int

class SubOrder(BaseModel):
    restaurant_id: str
    restaurant_name: str
    items: list[SubOrderItem]
    coupon_code: str | None
    subtotal: int
    fees: int
    discount: int
    total: int                        # must be < 1000

class Plan(BaseModel):
    plan_id: str
    kind: Literal["single", "multi"]
    sub_orders: list[SubOrder]
    total: int
    satisfaction: float               # 0..1 across soft prefs
    n_deliveries: int
    notes: str                        # trade-off explanation
    rationale: str                    # "why this fits everyone" (generated from scores)
    why_not_runner_up: str            # contrastive note vs the next-best plan
    per_person_fit: dict              # participant_id -> short fit explanation

class CircleState(BaseModel):
    room_id: str
    host_user_id: str
    address_id: str
    participants: list[PreferenceSpec]
    candidates: list[dict] = []       # restaurants (capped)
    plans: list[Plan] = []
    chosen_plan_id: str | None = None
    placed_orders: list[dict] = []    # {order_id, restaurant_id, status}
    split: list[dict] = []            # {participant_id, amount, upi_link}
```

### 4.6 The resolver scoring function (the part that makes or breaks it)

For a candidate restaurant `R` and the set of participants `P`:

1. **Hard filter:** for each `p` in `P`, does `R` have ≥1 menu item satisfying `p.veg`, `p.excludes`, `p.allergies`, and `p.budget_max`? If any participant has zero feasible items, `R` cannot be a *single-restaurant* solution for the whole group (it can still be part of a multi plan).
2. **Allergy caution:** menu allergen data is unreliable. If an item can't be *verified* allergen-safe, mark it `needs_confirmation` and route a yes/no back to that participant; never assume safe.
3. **Soft score:** for each participant, pick their best feasible item and score how well it matches their soft prefs (cuisine match, spice, "light", price headroom). Average across participants → satisfaction.
4. **Plan economics:** single-restaurant = 1 delivery fee, easiest to keep < ₹1000; multi-restaurant = N delivery fees but can hit 100% coverage. Both surfaced.
5. **Explainability:** each plan carries a generated `rationale` ("why this restaurant works for everyone"), a `why_not_runner_up` note contrasting it with the next-best plan ("Biryani House ranked lower — no veg-light option under ₹300 for Ananya"), and a `per_person_fit` map (one line per participant: the dish chosen and which of their prefs it hits/misses). The LLM writes these *from* the deterministic scores and coverage flags — it never invents a reason the numbers don't support.

Keep this auditable and mostly deterministic; use the LLM for fuzzy menu-item-to-preference matching and for turning scores into prose, not for the final ranking math.

### 4.7 Payments & settlement

Swiggy MCP places one COD order per sub-order, paid by the host. MCP cannot collect money from individual guests, so settlement is a separate layer. Two options:

**Default (free, v1): UPI deep links.** The `split_bill` node computes each guest's share and generates a UPI deep link (`upi://pay?pa=<host-vpa>&am=<share>&tn=Circle`). Guests tap and pay the host directly. Zero fees, zero KYC, instant peer-to-peer. Limitation: no automatic confirmation of who actually paid — reconciliation is manual (host eyeballs it), so we keep a simple in-app "mark as paid" ledger.

**Opt-in upgrade: Razorpay MCP payment links.** Razorpay's MCP server exposes Payment Links tools (create, fetch, fetch-all, update, cancel, notify) alongside Orders/Payments/Refunds/Settlements. It plugs into the same LangGraph tool set as a second MCP server. The `split_bill` node calls `create payment link` per guest, sends each the returned hosted `short_url`, and gets **real reconciliation** — paid/unpaid status via fetch or webhooks, plus automated reminders. This is the one thing UPI deep links can't do.

Trade-offs to weigh before enabling it:
- **Merchant account + KYC required.** Razorpay is built for a business collecting from customers, not friends splitting a bill. Someone must be the registered merchant.
- **Not free.** ~2% per transaction; funds route through the merchant account and settle to a bank later (not instant P2P).
- **"Who is the merchant" matters.** If *Circle* is the merchant collecting everyone's shares, it is holding third-party money — that drifts into payment-aggregator territory with its own compliance burden. Cleaner model: **the host connects their own Razorpay via OAuth** (Razorpay MCP supports OAuth), so the host is the merchant collecting into their own account and the app never touches funds — at the cost of every host needing Razorpay KYC.

**Recommendation:** UPI deep links as the free default; Razorpay payment links as an opt-in for recurring / large / office groups where automatic reconciliation is worth the fee and KYC. Auth model for the upgrade: host's own Razorpay account via OAuth, never a Circle-owned merchant collecting on others' behalf. (This is an engineering note, not legal/financial advice — confirm the collect-on-behalf rules with Razorpay's terms before building it.)

---

## 5. Tools used and their arguments

All Food-server tools: `POST mcp.swiggy.com/food`. Session auth supplied automatically (Bearer token); never passed as an argument. Args below are verified against the reference docs unless marked *(inferred)*.

### Discover / read

| Tool | Required args | Optional args | Notes |
|---|---|---|---|
| `get_addresses` | — | — | Returns saved addresses, sorted by last order. Cache per session. |
| `search_restaurants` | `addressId`, `query` | `offset` | Returns `availabilityStatus` (OPEN/CLOSED/UNAVAILABLE), `distanceKm`, cuisines, rating, costForTwo. Only use OPEN. |
| `search_menu` | `addressId`, `query` | `restaurantIdOfAddedItem`, `vegFilter` (0/1), `offset` | Items carry `variations` **or** `variantsV2` (never both) + `addons`. `vegFilter=1` for veg-only. |
| `get_restaurant_menu` | `restaurantId`, `addressId` | `page`, `pageSize` *(inferred names)* | Full menu, paginated by category. Use to browse beyond search hits. `addressId` is required by the API even though it is not listed in the published schema. |
| `get_food_cart` | `addressId` | `restaurantName` | Returns items, bill breakdown, `availablePaymentMethods`, `valid_addons`. `offers.coupon_applied` with `coupon_discount=0` means *suggested, not applied*. |
| `fetch_food_coupons` | `restaurantId`, `addressId` | `couponCode` | Already filters to **COD-valid** coupons. Returns best coupons + applicability + discount. |

### Mutate / cart

| Tool | Required args | Optional args | Notes |
|---|---|---|---|
| `update_food_cart` | `restaurantId`, `cartItems` (object[]), `addressId` | `restaurantName` | Binds cart to one restaurant; switching restaurants auto-flushes. **No widget** — must call `get_food_cart` after. Each item uses the SAME variant format it was returned in. For items with variants, `cartItems` must include `variantGroups: [{groupId, variationId}]` — sending only `itemId` + `quantity` results in an empty cart with no error. |
| `apply_food_coupon` | `couponCode`, `addressId` | `cartId` | Returns updated cart with discount. |
| `flush_food_cart` | `addressId` *(inferred)* | — | Clears the cart. Used when restarting a sub-order. |

### Order / track

| Tool | Required args | Optional args | Notes |
|---|---|---|---|
| `place_food_order` | `addressId` | `paymentMethod` | **Cart must be < ₹1000.** Requires explicit user confirmation (mandated). Show only `availablePaymentMethods`. Use the response message as-is (Swiggy branding). |
| `get_food_orders` | — | — | Active orders + status. |
| `get_food_order_details` | `orderId` *(inferred)* | — | Full detail for one order. |
| `track_food_order` | `orderId` *(inferred)* | — | Live status. **Poll ≤ once / 10s.** |

### Optional (Dineout server, future)
If a group would rather go out: `get_saved_locations`, `search_restaurants_dineout`, `get_available_slots`, `create_cart`, `book_table` (FREE reservations only, `isFree=true`), `get_booking_status`.

### Optional (Razorpay MCP, opt-in settlement)
Separate MCP server, plugged into the same tool set. Used only if the host enables Razorpay settlement (see §4.7). Auth via the host's own Razorpay account (API keys or OAuth).

| Tool (Payment Links category) | Purpose |
|---|---|
| create payment link | One hosted `short_url` per guest for their share. Typical args: `amount` (paise), `currency` (`INR`), `description`, `customer` (name/contact), `notify` (sms/email), `reminder_enable`, `callback_url`. |
| fetch / fetch-all payment link | Reconciliation — check paid/unpaid status per guest. |
| update / cancel payment link | Adjust or void a share. |
| notify (resend) | Trigger a reminder to an unpaid guest. |

Razorpay MCP also exposes Orders, Payments, Refunds, QR Codes, Settlements, and Payouts (35+ tools total) — not needed for v1 split, but available if settlement logic grows.

---

## 6. User flow example

**Scene:** Friday dinner, four friends in Koramangala, Bengaluru. Host = Darshan.

1. **Create room.** Darshan opens Circle and connects Swiggy (phone+OTP, one time). The app fetches his saved addresses via `get_addresses` and shows them; he picks "Home — Koramangala" as the room's delivery address. Gets a share link + QR.
2. **Invite.** Drops the link in WhatsApp. Ananya, Rohit, Meera, Karan tap it, enter names, land in the room. (Darshan can kick a stray join.)
3. **Cravings.** Each fills a craving card (+ optional chat banter):
   - Ananya — veg, exclude paneer, budget ₹250, vibe "something light".
   - Rohit — non-veg, "biryani, spicy", no budget cap.
   - Meera — either, **allergy: peanut**, budget ₹300, vibe "Chinese".
   - Karan — either, budget ₹150, "cheapest thing".
4. **`/activate`.** Darshan runs the command. The agent parses cards → specs and shows Darshan the structured summary. He fixes one thing (Ananya actually wants ₹300) and approves.
5. **Resolve → plans.** The agent searches multi-cuisine restaurants near the address, checks menus, and returns a ranked shortlist — each plan with a rationale and a *why-not* against the runner-up:
   - **Plan A (single restaurant):** "Spice Junction" covers all four. Satisfaction ~88%, 1 delivery, ≈ ₹820 after a COD coupon. *Why:* one stop satisfies veg-light, biryani, Chinese, and a cheap roll. *Why not Plan C:* C is ₹130 cheaper but drops Rohit's biryani for a basic rice bowl. Meera's Chinese dish flagged *confirm peanut-free*.
   - **Plan B (two restaurants):** biryani specialist for Rohit + Chinese/veg place for the rest. Satisfaction ~100%, 2 deliveries, ≈ ₹1,040. *Why not Plan A:* fully happy but +₹220 and a second delivery fee.
   - **Plan C (cheapest):** single restaurant, ≈ ₹690, satisfaction ~74%.
6. **Group chooses.** The room votes; Plan A wins. Meera taps "yes, peanut-free confirmed" on her flagged item.
7. **Build + split.** Agent builds the Spice Junction cart, applies the best COD coupon, computes shares:
   - Ananya ₹184 · Rohit ₹281 · Meera ₹252 · Karan ₹106 (sums to the ₹823 total; the coupon is shared pro-rata, not pocketed).
   - Each guest gets a one-tap UPI link to repay Darshan.
8. **Confirm + order.** Darshan sees the final cart (< ₹1000 ✓), confirms, COD order placed. Everyone watches live tracking in the room.

*(If Plan B had won, steps 7–8 would run twice, sequentially — build+place the biryani order fully, then build+place the second order, because the cart flushes between restaurants.)*

---

## 7. Tech stack (free / OSS-first)

| Layer | Choice | Why / cost |
|---|---|---|
| Frontend | **Next.js / React** | OSS. Deploy free on Vercel/Cloudflare Pages. |
| Realtime room + chat + voting | **Supabase Realtime** (or self-hosted Socket.IO) | Supabase free tier: Postgres + realtime + row-level security in one. |
| App DB | **Postgres** (Supabase / Neon / Railway free tier) | Rooms, participants, prefs, plans, orders. |
| Backend / agent | **FastAPI (Python)** | Your stack; pairs with LangGraph. |
| Orchestration | **LangGraph** | Multi-agent graph + native human-in-the-loop interrupts. OSS. |
| MCP client | **`langchain-mcp-adapters`** or raw `mcp` Python SDK | Listed as supported; OSS. |
| LLM routing | **OpenRouter** | OpenAI-compatible API. Single key routes to Llama 3.3 70B (parse), Llama 3.1 8B (scoring), Claude Haiku (rationale). Pay-per-token, no infra. |
| Token vault | Postgres column encrypted with **libsodium/Fernet** | Store host Swiggy tokens encrypted at rest; never log; 5-day expiry handling. |
| Settlement (default) | **UPI deep links** | Free, no KYC, peer-to-peer to the host. |
| Settlement (opt-in) | **Razorpay MCP** payment links | Auto reconciliation + reminders; host's own merchant account; ~2% fee + KYC. |
| Hosting (backend) | **Render / Fly.io / Railway** free allowance, or a small VPS | FastAPI + worker. |

**The only thing that costs money is LLM inference** — OpenRouter's pay-per-token model keeps it cheap at launch scale. A full group-order activation (parse + feasibility + rationale) costs well under ₹1 at Llama 3.x pricing.

---

## 8. Deployment plan

**Phase 0 — Local dev (no whitelist needed).**
- Everything on `localhost`. Swiggy allows `http://localhost` redirect URIs for dev.
- Build the full flow end-to-end against the Food server with your own Swiggy account as the host.
- Record a short Loom/Drive demo of the whole journey (this is explicitly what speeds up access approval).

**Phase 1 — Request access.**
- Apply at `mcp.swiggy.com/builders/access/` with: integration name, org, exact-match HTTPS redirect URIs, requested servers (`food`, optionally `dineout`), expected volume, use case, and the demo video link (or email `builders@swiggy.in`).
- Receive `client_id` + staging credentials. Register your production HTTPS callback URL.

**Phase 2 — Deploy on free tiers.**
- Frontend → Vercel/Cloudflare Pages.
- Backend + agent → Render/Fly/Railway.
- DB + realtime → Supabase.
- Wire the registered HTTPS `redirect_uri` for the host OAuth callback.
- Encrypt and store host tokens; implement 401 → re-authorize.

**Phase 3 — Scale / harden.**
- If you'll exceed planned quotas (120 req/min/user/server, 50k/day/client_id), email `builders@swiggy.in` *before* launch to negotiate a ceiling.
- Use a **separate `client_id`** for any background/analytics jobs so they don't starve interactive traffic.
- Add observability (structured logs, retry/backoff with jitter, `report_error` integration for diagnostics).

---

## 9. Phased product roadmap

- **v1 (MVP, demo-ready):** host-only auth · host picks delivery address on login · craving cards · single + multi-restaurant plans · ranked shortlist with per-plan rationale + why-not + group vote · sequential multi-order placement · informational bill split with UPI links · live tracking. Hard cap each sub-order < ₹1000.
- **v1.1:** freeform chat as a soft-preference "vibe" layer · kick/mute moderation polish · in-app "mark as paid" ledger.
- **v2:** optional per-participant auth (clean per-person COD payment + parallel multi-restaurant) · opt-in Razorpay payment-link settlement (host's own account) · Dineout branch ("go out instead?") with free table booking · reorder-favourites for recurring groups.
- **v3:** taste memory per group · scheduled recurring circles (office lunch) · "Circle Wrapped" shareable recap for extra virality.

---

## 10. Open questions / risks

1. **Menu allergen data depth.** Almost certainly incomplete. Mitigation: explicit allergy field + `needs_confirmation` flagging + per-participant yes/no. Validate on day one with real menu responses.
2. **₹1000 cap vs. group size.** Four hungry people can exceed it on a single restaurant. Mitigation: detect at cart-build, auto-split into more sub-orders or trim, always warn the host.
3. **Out-of-band settlement.** MCP can't collect per-person payment. Mitigation: UPI deep links by default (host fronts the bill, guests repay him); optional Razorpay payment links for auto-reconciliation (§4.7). v2 per-participant auth removes the fronting entirely.
4. **Cart TTL / `CART_EXPIRED`.** Long-running rooms may go stale. Mitigation: rebuild carts at confirm time, re-read before placing.
5. **Production access is whitelist-gated.** This is the meaning of "prod needs approval": Swiggy's *production* MCP endpoints require an approved `client_id`. You can build, run, and demo the entire app on `localhost` with no permission (Swiggy allows `http://localhost` redirect URIs for dev), but you cannot point *real users* at real Swiggy accounts / real orders until you apply at `/access`, submit a demo video, and are granted production credentials. It gates go-live, not development. Mitigation: build + record the demo on localhost first; the video is the documented fast path through review.
6. **Razorpay merchant model (if enabling settlement).** Collecting on others' behalf can be a regulated activity. Mitigation: host connects their *own* Razorpay account; Circle never acts as a merchant holding third-party funds. Confirm against Razorpay's terms before building.
