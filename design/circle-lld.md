# Circle — Low Level Design

**LLM provider:** OpenRouter (OpenAI-compatible)
**Repo layout:** Monorepo

---

## 1. Monorepo Structure

```
circle/
├── frontend/                   # Next.js 14 (App Router)
│   ├── app/
│   │   ├── page.tsx                    # landing / create room
│   │   ├── auth/callback/page.tsx      # OAuth redirect handler
│   │   ├── room/[id]/page.tsx          # host view
│   │   └── join/[id]/page.tsx          # guest join + craving card
│   ├── components/
│   │   ├── CravingCard.tsx
│   │   ├── PlanCard.tsx
│   │   ├── TrackingBar.tsx
│   │   └── SplitSheet.tsx
│   ├── lib/
│   │   ├── supabase.ts                 # Supabase browser client
│   │   └── api.ts                      # typed fetch wrappers
│   ├── package.json
│   └── .env.local
│
├── backend/                    # FastAPI
│   ├── main.py
│   ├── routers/
│   │   ├── auth.py                     # /auth/*
│   │   ├── rooms.py                    # /rooms/*
│   │   └── agent.py                    # /agent/* (internal)
│   ├── agent/
│   │   ├── graph.py                    # LangGraph graph definition
│   │   ├── runner.py                   # CircleAgentRunner (holds MCP session)
│   │   ├── nodes/
│   │   │   ├── snapshot.py
│   │   │   ├── parse_prefs.py
│   │   │   ├── discover.py
│   │   │   ├── feasibility.py
│   │   │   ├── build_plans.py
│   │   │   ├── price_plans.py
│   │   │   ├── build_cart.py
│   │   │   ├── split_bill.py
│   │   │   ├── confirm_order.py
│   │   │   └── track.py
│   │   ├── resolver.py                 # scoring + ranking logic
│   │   └── state.py                    # CircleState Pydantic model
│   ├── db/
│   │   ├── models.py                   # SQLAlchemy ORM models
│   │   ├── crud.py
│   │   └── migrations/                 # Alembic
│   ├── vault.py                        # token encrypt/decrypt
│   ├── llm.py                          # OpenRouter client
│   ├── config.py                       # settings from env
│   ├── requirements.txt
│   └── .env
│
├── infra/
│   ├── docker-compose.yml      # postgres + supabase local
│   └── .env.example
│
└── scripts/
    ├── dev.sh                  # starts all services
    └── migrate.sh
```

---

## 2. Environment Variables

```bash
# backend/.env

# Supabase
SUPABASE_URL=http://localhost:54321
SUPABASE_SERVICE_ROLE_KEY=...
DATABASE_URL=postgresql://postgres:postgres@localhost:54322/postgres

# Swiggy MCP
SWIGGY_MCP_BASE_URL=https://mcp.swiggy.com
SWIGGY_REDIRECT_URI=http://localhost:8000/auth/callback

# Token vault
VAULT_FERNET_KEY=...          # generate: Fernet.generate_key()

# OpenRouter
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1

# App
SECRET_KEY=...                # FastAPI session signing
FRONTEND_URL=http://localhost:3000
```

```bash
# frontend/.env.local
NEXT_PUBLIC_SUPABASE_URL=http://localhost:54321
NEXT_PUBLIC_SUPABASE_ANON_KEY=...
NEXT_PUBLIC_API_URL=http://localhost:8000
```

---

## 3. Database Schema

```sql
-- rooms
CREATE TABLE rooms (
    id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    host_user_id  TEXT NOT NULL,
    address_id    TEXT,                    -- set after host picks address
    status        TEXT NOT NULL DEFAULT 'collecting',
                  -- collecting | activated | planning | choosing
                  -- | ordering | tracking | done
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

-- participants
CREATE TABLE participants (
    id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    room_id       TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    display_name  TEXT NOT NULL,
    is_host       BOOLEAN NOT NULL DEFAULT false,
    joined_at     TIMESTAMPTZ DEFAULT now()
);

-- craving cards (one per participant per room)
CREATE TABLE craving_cards (
    id               TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    participant_id   TEXT NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    room_id          TEXT NOT NULL,
    veg              TEXT NOT NULL DEFAULT 'either',   -- veg | non_veg | either
    budget_max       INT,                              -- rupees, null = no cap
    cuisine_vibe     TEXT,                             -- free text
    must_have        TEXT,                             -- free text
    allergies        JSONB NOT NULL DEFAULT '[]',      -- ["peanut", "shellfish"]
    deal_breakers    JSONB NOT NULL DEFAULT '[]',      -- ["paneer"]
    submitted_at     TIMESTAMPTZ DEFAULT now()
);

-- parsed preference specs (written by agent after parse_preferences node)
CREATE TABLE pref_specs (
    id               TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    participant_id   TEXT NOT NULL REFERENCES participants(id),
    room_id          TEXT NOT NULL,
    veg              TEXT NOT NULL,
    budget_max       INT,
    allergies        JSONB NOT NULL DEFAULT '[]',
    excludes         JSONB NOT NULL DEFAULT '[]',
    soft             JSONB NOT NULL DEFAULT '[]',      -- ["biryani","spicy"]
    raw_chat         TEXT NOT NULL DEFAULT '',
    approved         BOOLEAN NOT NULL DEFAULT false,   -- set by host confirm
    updated_at       TIMESTAMPTZ DEFAULT now()
);

-- restaurant candidates shortlisted by discover node
CREATE TABLE candidates (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    room_id           TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    restaurant_id     TEXT NOT NULL,
    restaurant_name   TEXT NOT NULL,
    cuisines          JSONB,
    rating            NUMERIC(3,1),
    cost_for_two      INT,
    distance_km       NUMERIC(4,2),
    availability      TEXT,
    metadata          JSONB                            -- full search_restaurants row
);

-- generated plans
CREATE TABLE plans (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    room_id           TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,                  -- single | multi
    sub_orders        JSONB NOT NULL,                 -- list[SubOrder]
    total             INT NOT NULL,
    satisfaction      NUMERIC(4,3),                   -- 0..1
    n_deliveries      INT NOT NULL,
    notes             TEXT,
    rationale         TEXT,
    why_not_runner_up TEXT,
    per_person_fit    JSONB,
    rank              INT,                            -- 1 = best
    chosen            BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ DEFAULT now()
);

-- plan votes
CREATE TABLE plan_votes (
    participant_id    TEXT NOT NULL REFERENCES participants(id),
    plan_id           TEXT NOT NULL REFERENCES plans(id),
    voted_at          TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (participant_id, plan_id)
);

-- placed orders (one row per sub-order)
CREATE TABLE placed_orders (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    room_id           TEXT NOT NULL REFERENCES rooms(id),
    plan_id           TEXT REFERENCES plans(id),
    swiggy_order_id   TEXT,
    restaurant_id     TEXT NOT NULL,
    restaurant_name   TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    sub_order_data    JSONB,                          -- full SubOrder snapshot
    placed_at         TIMESTAMPTZ
);

-- bill split
CREATE TABLE splits (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    room_id           TEXT NOT NULL REFERENCES rooms(id),
    participant_id    TEXT NOT NULL REFERENCES participants(id),
    amount            INT NOT NULL,                   -- rupees
    upi_link          TEXT,
    paid              BOOLEAN NOT NULL DEFAULT false
);

-- host token vault
CREATE TABLE host_tokens (
    host_user_id      TEXT PRIMARY KEY,
    encrypted_token   BYTEA NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- enable RLS on all tables (Supabase)
ALTER TABLE rooms ENABLE ROW LEVEL SECURITY;
ALTER TABLE participants ENABLE ROW LEVEL SECURITY;
-- (policies defined per table — guests read own room, host reads all)
```

---

## 4. API Routes (FastAPI)

### Auth — `routers/auth.py`

```
GET  /auth/start?room_id=...
     → registers Swiggy DCR client, builds PKCE, redirects to Swiggy authorize URL
     → stores (state, verifier, room_id) in server-side session

GET  /auth/callback?code=...&state=...
     → exchanges code for token, encrypts into vault
     → redirects to frontend /room/:id

GET  /auth/status
     → returns {authenticated: bool, expires_at} for the current session host
```

### Rooms — `routers/rooms.py`

```
POST /rooms
     body: {}
     → creates room row, returns {room_id, join_url}

GET  /rooms/:id
     → returns full room state: status, participants, cards submitted count

POST /rooms/:id/address
     body: {address_id: str}
     → sets room.address_id (host only)

POST /rooms/:id/join
     body: {display_name: str}
     → creates participant row, returns {participant_id}
     → broadcasts participant:joined via Supabase Realtime

DELETE /rooms/:id/participants/:pid
     → host only; sets participant as kicked
     → broadcasts participant:kicked

POST /rooms/:id/cards
     body: CravingCard
     → upserts craving_cards for participant_id
     → broadcasts card:submitted

POST /rooms/:id/activate
     → host only; validates all cards submitted
     → sets room.status = 'activated'
     → enqueues LangGraph run (BackgroundTasks)
     → returns {run_id}

POST /rooms/:id/approve-prefs
     body: {edits: list[PrefSpecEdit]}  # host may edit parsed prefs
     → updates pref_specs.approved = true
     → resumes LangGraph graph at host_confirm_prefs interrupt

POST /rooms/:id/vote
     body: {plan_id: str}
     → inserts plan_vote
     → broadcasts plan:vote

POST /rooms/:id/choose-plan
     body: {plan_id: str}
     → host only; sets plan.chosen = true, room.status = 'ordering'
     → resumes graph at present_options interrupt

POST /rooms/:id/confirm-order
     body: {sub_order_index: int}
     → host only; resumes graph at confirm_and_order interrupt

GET  /rooms/:id/addresses
     → calls get_addresses MCP tool, returns list for host to pick from
```

### Agent internal — `routers/agent.py`

```
POST /agent/interrupt-response
     body: {run_id, node, payload}
     → internal endpoint called by interrupt-resume mechanism
```

---

## 5. Supabase Realtime Events

All events go on channel `room:{room_id}`. Frontend subscribes on join.

```typescript
type RealtimeEvent =
  | { event: 'participant:joined';  payload: { participant_id, display_name } }
  | { event: 'participant:kicked';  payload: { participant_id } }
  | { event: 'card:submitted';      payload: { participant_id, display_name } }
  | { event: 'room:status';         payload: { status: RoomStatus } }
  | { event: 'prefs:parsed';        payload: { pref_specs: PrefSpec[] } }     // → shows confirm UI to host
  | { event: 'plans:ready';         payload: { plans: Plan[] } }              // → shows voting UI
  | { event: 'plan:vote';           payload: { participant_id, plan_id } }
  | { event: 'order:placed';        payload: { swiggy_order_id, restaurant } }
  | { event: 'order:tracking';      payload: { swiggy_order_id, status, eta_mins } }
  | { event: 'order:conflict';      payload: { message: string } }            // hard constraint failure
  | { event: 'split:ready';         payload: { splits: Split[] } }
```

Backend broadcasts via Supabase service role client:
```python
supabase.channel(f"room:{room_id}").send_broadcast(event=event_name, payload=payload)
```

---

## 6. LangGraph Agent

### State

```python
# backend/agent/state.py
from typing import Annotated, Literal
from langgraph.graph.message import add_messages
from pydantic import BaseModel

class PrefSpec(BaseModel):
    participant_id: str
    display_name: str
    veg: Literal["veg", "non_veg", "either"]
    budget_max: int | None
    allergies: list[str]       # HARD
    excludes: list[str]        # HARD
    soft: list[str]            # soft prefs

class SubOrderItem(BaseModel):
    participant_id: str
    item_id: str
    item_name: str
    variant: dict | None       # variantGroups: [{groupId, variationId}]
    addons: list[dict] = []
    price: int                 # rupees

class SubOrder(BaseModel):
    restaurant_id: str
    restaurant_name: str
    items: list[SubOrderItem]
    coupon_code: str | None = None
    subtotal: int
    fees: int
    discount: int
    total: int                 # enforced < 1000

class Plan(BaseModel):
    plan_id: str
    kind: Literal["single", "multi"]
    sub_orders: list[SubOrder]
    total: int
    satisfaction: float
    n_deliveries: int
    rationale: str
    why_not_runner_up: str
    per_person_fit: dict[str, str]

class CircleState(BaseModel):
    room_id: str
    host_user_id: str
    address_id: str
    raw_inputs: dict = {}          # participant_id -> {card, chat}
    pref_specs: list[PrefSpec] = []
    candidates: list[dict] = []
    feasibility_map: dict = {}     # restaurant_id -> {participant_id -> best_item}
    plans: list[Plan] = []
    chosen_plan_id: str | None = None
    current_sub_order_idx: int = 0
    placed_orders: list[dict] = []
    split: list[dict] = []
    conflict_message: str | None = None
```

### Graph Definition

```python
# backend/agent/graph.py
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres import PostgresSaver

from .nodes import (
    snapshot, parse_prefs, discover, feasibility,
    build_plans, price_plans, build_cart,
    split_bill, confirm_order, track
)
from .state import CircleState

def build_graph(db_url: str) -> StateGraph:
    checkpointer = PostgresSaver.from_conn_string(db_url)
    builder = StateGraph(CircleState)

    builder.add_node("snapshot_inputs",    snapshot.run)
    builder.add_node("parse_preferences",  parse_prefs.run)
    builder.add_node("host_confirm_prefs", lambda s: s)   # interrupt point
    builder.add_node("discover",           discover.run)
    builder.add_node("feasibility",        feasibility.run)
    builder.add_node("build_plans",        build_plans.run)
    builder.add_node("price_plans",        price_plans.run)
    builder.add_node("present_options",    lambda s: s)   # interrupt point
    builder.add_node("build_cart",         build_cart.run)
    builder.add_node("split_bill",         split_bill.run)
    builder.add_node("confirm_and_order",  lambda s: s)   # interrupt point
    builder.add_node("track",              track.run)

    builder.set_entry_point("snapshot_inputs")
    builder.add_edge("snapshot_inputs",    "parse_preferences")
    builder.add_edge("parse_preferences",  "host_confirm_prefs")
    builder.add_edge("host_confirm_prefs", "discover")
    builder.add_edge("discover",           "feasibility")
    builder.add_conditional_edges("feasibility", _feasibility_router)
    builder.add_edge("build_plans",        "price_plans")
    builder.add_edge("price_plans",        "present_options")
    builder.add_edge("present_options",    "build_cart")
    builder.add_edge("build_cart",         "split_bill")
    builder.add_edge("split_bill",         "confirm_and_order")
    builder.add_conditional_edges("confirm_and_order", _order_router)
    builder.add_edge("track",              END)

    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["host_confirm_prefs", "present_options", "confirm_and_order"],
    )

def _feasibility_router(state: CircleState) -> str:
    if state.conflict_message:
        return "build_plans"   # surface conflict in plans, don't error
    return "build_plans"

def _order_router(state: CircleState) -> str:
    plan = next(p for p in state.plans if p.plan_id == state.chosen_plan_id)
    if state.current_sub_order_idx < len(plan.sub_orders):
        return "build_cart"    # loop for multi-restaurant
    return "track"
```

### Interrupt Resume Pattern

```python
# backend/routers/rooms.py — interrupt resume (see §7 for full implementation with MCP session)
```

---

## 7. MCP Client — `langchain-mcp-adapters`

Swiggy supports `langchain-mcp-adapters` in "Bearer token only" mode: obtain the access token via our OAuth flow, then pass it as a header. The adapter wraps every Swiggy tool as a standard LangChain `BaseTool`, callable via `.ainvoke()` in any LangGraph node.

```
pip install langchain-mcp-adapters
```

### Setup — `backend/agent/runner.py`

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.types import RunnableConfig
from .graph import build_graph
from .state import CircleState

FOOD_URL = "https://mcp.swiggy.com/food"

async def run_graph(room_id: str, access_token: str, initial_state: CircleState,
                    db, supabase):
    """
    Opens a single MCP session for the full graph run.
    Tools are injected into every node via LangGraph's configurable.
    """
    async with MultiServerMCPClient({
        "food": {
            "url": FOOD_URL,
            "transport": "streamable_http",
            "headers": {"Authorization": f"Bearer {access_token}"},
        }
    }) as mcp:
        tools = await mcp.get_tools()                    # list[BaseTool]
        tools_by_name = {t.name: t for t in tools}

        graph = build_graph(db)
        config: RunnableConfig = {
            "configurable": {
                "thread_id": room_id,
                "mcp_tools": tools_by_name,              # injected into every node
                "db": db,
                "supabase": supabase,
            }
        }
        await graph.ainvoke(initial_state, config)
```

### Calling tools inside nodes

Every node receives `config: RunnableConfig` and reads tools from it:

```python
# backend/agent/nodes/discover.py
from langgraph.types import RunnableConfig
from ..state import CircleState

async def run(state: CircleState, config: RunnableConfig) -> CircleState:
    tools = config["configurable"]["mcp_tools"]

    result = await tools["search_restaurants"].ainvoke({
        "addressId": state.address_id,
        "query": "biryani",
    })
    # result is a string (the tool's text content)
    ...
    return state
```

All 14 food-server tools are available by name after `mcp.get_tools()`. No typed wrapper file needed — just call by name with the args from LLD §14.

### Resuming after interrupt

When a graph is resumed from a human-in-the-loop interrupt, a new MCP session is opened for that continuation. The session lifetime matches one graph execution segment (start → interrupt, or resume → next interrupt/end):

```python
# routers/rooms.py
async def resume_graph(room_id: str, node: str, update: dict,
                       access_token: str, db, supabase):
    async with MultiServerMCPClient({
        "food": {
            "url": FOOD_URL,
            "transport": "streamable_http",
            "headers": {"Authorization": f"Bearer {access_token}"},
        }
    }) as mcp:
        tools_by_name = {t.name: t for t in await mcp.get_tools()}
        graph = build_graph(db)
        config = {
            "configurable": {
                "thread_id": room_id,
                "mcp_tools": tools_by_name,
                "db": db,
                "supabase": supabase,
            }
        }
        await graph.aupdate_state(config, update, as_node=node)
        await graph.ainvoke(None, config)
```

### Why langchain-mcp-adapters over raw mcp SDK

| | `langchain-mcp-adapters` | raw `mcp` SDK |
|---|---|---|
| Tool format | LangChain `BaseTool` — works with `ToolNode`, bind_tools, agents | Raw JSON-RPC result |
| Boilerplate | `tools_by_name["x"].ainvoke(args)` | Custom wrapper per tool |
| Multiple servers | `MultiServerMCPClient` handles N servers in one context | Manual per-server sessions |
| LangGraph fit | Native — tools already in the right format | Extra conversion layer |
| Auth | Pass `headers` dict — matches Swiggy's Bearer-only requirement | Same |

---

## 8. OpenRouter LLM Integration

```python
# backend/llm.py
from openai import AsyncOpenAI
from .config import settings

# OpenRouter is OpenAI-compatible — same SDK, different base_url
_client = AsyncOpenAI(
    base_url=settings.OPENROUTER_BASE_URL,   # https://openrouter.ai/api/v1
    api_key=settings.OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": "https://circle.app",   # shown in OpenRouter dashboard
        "X-Title": "Circle",
    },
)

# Model assignments — swap freely via env if needed
PARSE_MODEL     = "meta-llama/llama-3.3-70b-instruct"    # structured extraction
SCORE_MODEL     = "meta-llama/llama-3.1-8b-instruct"     # fast, cheap, per-item matching
RATIONALE_MODEL = "anthropic/claude-haiku-4-5"            # prose generation

async def chat(model: str, system: str, user: str, response_format=None) -> str:
    kwargs = dict(model=model, messages=[
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ])
    if response_format:
        kwargs["response_format"] = response_format
    resp = await _client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content
```

### LLM prompts

**parse_preferences node** — one call per participant:
```
system: You are extracting structured food preferences from a craving card.
        Return JSON matching this schema: {veg, budget_max, allergies, excludes, soft}.
        allergies and excludes are HARD constraints — never treat as soft.
        soft is a list of strings: cuisines, textures, descriptors.

user:   Craving card:
        veg/nonveg/either: {card.veg}
        budget: {card.budget_max}
        cuisine vibe: {card.cuisine_vibe}
        must have: {card.must_have}
        allergies: {card.allergies}
        deal breakers: {card.deal_breakers}
```

**feasibility node** — per (participant, restaurant) pair:
```
system: Given a menu item description and a participant's preference spec, return JSON:
        {feasible: bool, score: 0..1, reason: str}.
        feasible=false if: veg mismatch, allergy present, over budget, in excludes list.
        score = how well it matches soft prefs (0=poor, 1=perfect).

user:   Item: {item_name} — {item_description} — ₹{price}
        Participant: veg={veg}, budget={budget_max}, allergies={allergies},
                     excludes={excludes}, soft={soft}
```

**rationale node** — per plan:
```
system: Write a 2-sentence "why this plan works for everyone" using ONLY the data given.
        Also write a 1-sentence "why not the runner-up" using the contrast data given.
        Never invent facts.

user:   Plan: {plan_summary_json}
        Runner-up contrast: {contrast_json}
```

---

## 9. Token Vault

```python
# backend/vault.py
import json
from cryptography.fernet import Fernet
from datetime import datetime, timedelta, timezone
from .config import settings
from .db import crud

class TokenVault:
    def __init__(self):
        self._f = Fernet(settings.VAULT_FERNET_KEY.encode())

    def store(self, host_user_id: str, token_data: dict, db):
        payload = json.dumps(token_data).encode()
        encrypted = self._f.encrypt(payload)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
        crud.upsert_host_token(db, host_user_id, encrypted, expires_at)

    def retrieve(self, host_user_id: str, db) -> dict | None:
        row = crud.get_host_token(db, host_user_id)
        if not row:
            return None
        if row.expires_at < datetime.now(timezone.utc):
            return None   # expired — caller must re-auth
        return json.loads(self._f.decrypt(row.encrypted_token))

    def access_token(self, host_user_id: str, db) -> str | None:
        data = self.retrieve(host_user_id, db)
        return data["access_token"] if data else None

vault = TokenVault()
```

---

## 10. Resolver / Scoring

```python
# backend/agent/resolver.py

def score_restaurant(
    restaurant_id: str,
    menu_items: list[dict],        # from get_restaurant_menu / search_menu
    pref_specs: list[PrefSpec],
    item_scores: dict,             # (item_id, participant_id) -> {feasible, score}
) -> dict:
    """
    Returns:
      coverage: list[participant_id] who have >=1 feasible item
      uncovered: list[participant_id] with no feasible item
      satisfaction: float (avg soft score across covered participants)
      best_items: {participant_id: SubOrderItem}
    """
    coverage, uncovered, best_items = [], [], {}
    for spec in pref_specs:
        feasible = [
            (item, item_scores[(item["id"], spec.participant_id)])
            for item in menu_items
            if item_scores.get((item["id"], spec.participant_id), {}).get("feasible")
        ]
        if feasible:
            coverage.append(spec.participant_id)
            best = max(feasible, key=lambda x: x[1]["score"])
            best_items[spec.participant_id] = best[0]
        else:
            uncovered.append(spec.participant_id)

    satisfaction = (
        sum(item_scores[(best_items[pid]["id"], pid)]["score"] for pid in coverage)
        / len(pref_specs)
        if pref_specs else 0.0
    )
    return dict(coverage=coverage, uncovered=uncovered,
                satisfaction=satisfaction, best_items=best_items)


def build_single_plans(candidates: list[dict], scores: dict) -> list[Plan]:
    # candidates where uncovered == [] → full-coverage single-restaurant plans
    # rank by satisfaction desc, then total asc
    ...

def build_multi_plan(candidates: list[dict], scores: dict,
                     pref_specs: list[PrefSpec]) -> Plan | None:
    # greedy: pick restaurant that covers the most uncovered participants,
    # repeat until all covered or candidates exhausted
    # result is a multi sub-order plan
    ...

def rank_plans(plans: list[Plan]) -> list[Plan]:
    # primary: full coverage first
    # secondary: satisfaction desc
    # tertiary: total asc
    # cap at top 3 for presentation
    return sorted(plans, key=lambda p: (-int(p.n_deliveries == 1),
                                        -p.satisfaction, p.total))[:3]
```

---

## 11. Bill Split

```python
# backend/agent/nodes/split_bill.py
import urllib.parse

def compute_split(sub_order: SubOrder) -> list[dict]:
    """
    Each participant pays:
      own_items_total + pro_rata_share_of(fees - discount)
    Discount is shared proportionally to item cost.
    """
    participant_totals = {}
    for item in sub_order.items:
        participant_totals[item.participant_id] = (
            participant_totals.get(item.participant_id, 0) + item.price
        )
    items_total = sum(participant_totals.values())
    net_extras = sub_order.fees - sub_order.discount  # can be negative (net discount)

    splits = []
    for pid, item_cost in participant_totals.items():
        share_of_extras = round((item_cost / items_total) * net_extras) if items_total else 0
        amount = item_cost + share_of_extras
        splits.append({"participant_id": pid, "amount": amount})
    return splits


def upi_link(host_vpa: str, amount: int, room_id: str) -> str:
    note = urllib.parse.quote(f"Circle {room_id[:6]}")
    return f"upi://pay?pa={host_vpa}&am={amount}&tn={note}&cu=INR"
```

---

## 12. CART_EXPIRED Handling

The `build_cart` node checks `get_food_cart` after each `update_food_cart`. If the response contains `CART_EXPIRED`:

```python
async def build_cart_for_sub_order(client, sub_order: SubOrder, address_id: str,
                                    retries: int = 2) -> str:
    for attempt in range(retries):
        await flush_food_cart(client, address_id)
        await update_food_cart(
            client,
            restaurant_id=sub_order.restaurant_id,
            address_id=address_id,
            cart_items=[item_to_cart_format(i) for i in sub_order.items],
            restaurant_name=sub_order.restaurant_name,
        )
        cart = await get_food_cart(client, address_id)
        if "CART_EXPIRED" in cart:
            continue             # retry
        # assert total < 1000
        total = parse_cart_total(cart)
        if total >= 1000:
            raise ValueError(f"Sub-order total {total} >= 1000 — must trim items")
        return cart
    raise RuntimeError("Cart rebuild failed after retries")
```

---

## 13. Cart Item Format

Items passed to `update_food_cart` must include variant info when the item has variants. Format confirmed from `search_menu` response:

```python
def item_to_cart_format(item: SubOrderItem) -> dict:
    d = {"itemId": item.item_id, "quantity": 1}
    if item.variant:
        # variant is {"groupId": "...", "variationId": "..."}
        d["variantGroups"] = [item.variant]
    if item.addons:
        d["addons"] = item.addons
    return d
```

**Known:** sending only `itemId + quantity` for a variant item returns "Cart updated. Cart is empty." with no error — the server silently drops the item. Always include `variantGroups` for variant items.

---

## 14. Key Constraints Checklist (from testing)

| # | Constraint | Where enforced |
|---|---|---|
| 1 | `get_restaurant_menu` requires `addressId` (undocumented) | `tools.py: get_restaurant_menu` |
| 2 | `update_food_cart` variant items need `variantGroups: [{groupId, variationId}]` | `nodes/build_cart.py` |
| 3 | Sub-order total must be < ₹1000 | `nodes/build_cart.py` assertion |
| 4 | One cart = one restaurant; switching auto-flushes | sequential loop in `_order_router` |
| 5 | Coupons: only COD-eligible ones from `fetch_food_coupons` | `nodes/price_plans.py` |
| 6 | Track poll ≤ once / 10s | `nodes/track.py` sleep |
| 7 | Restaurant fan-out capped at 8–12 | `nodes/discover.py` |
| 8 | `get_food_orders` returns max 5 (server-side cap, no pagination) | `nodes/track.py` |
| 9 | Access token valid 5 days; any 401 → re-auth flow | `vault.py` + `auth.py` |
| 10 | `place_food_order` requires explicit human confirm | `confirm_and_order` interrupt |
