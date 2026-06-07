from typing import Literal
from pydantic import BaseModel


class PrefSpec(BaseModel):
    participant_id: str
    display_name: str
    veg: Literal["veg", "non_veg", "either"]
    budget_max: int | None = None
    allergies: list[str] = []
    excludes: list[str] = []
    soft: list[str] = []


class SubOrderItem(BaseModel):
    participant_id: str
    item_id: str
    item_name: str
    variant: dict | None = None
    addons: list[dict] = []
    price: int


class SubOrder(BaseModel):
    restaurant_id: str
    restaurant_name: str
    items: list[SubOrderItem] = []
    coupon_code: str | None = None
    subtotal: int = 0
    fees: int = 0
    discount: int = 0
    total: int = 0


class Plan(BaseModel):
    plan_id: str
    kind: Literal["single", "multi"]
    sub_orders: list[SubOrder]
    total: int
    satisfaction: float = 0.0
    n_deliveries: int
    rationale: str = ""
    why_not_runner_up: str = ""
    per_person_fit: dict[str, str] = {}


class CircleState(BaseModel):
    """
    Graph state. Collections are stored as plain JSON-serializable dicts (not
    nested Pydantic models) so the Postgres checkpointer can round-trip them
    cleanly on interrupt/resume. PrefSpec / Plan / SubOrder above are used as
    typed helpers *inside* nodes (build, then .model_dump() into state).
    """
    room_id: str
    host_user_id: str
    address_id: str
    raw_inputs: dict = {}
    pref_specs: list[dict] = []        # PrefSpec.model_dump()
    candidates: list[dict] = []
    feasibility_map: dict = {}
    plans: list[dict] = []             # Plan.model_dump()
    chosen_plan_id: str | None = None
    current_sub_order_idx: int = 0
    placed_orders: list[dict] = []
    split: list[dict] = []
    conflict_message: str | None = None
