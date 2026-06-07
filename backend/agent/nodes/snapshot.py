from langgraph.types import RunnableConfig
from agent.state import CircleState
from db import crud


async def run(state: CircleState, config: RunnableConfig) -> dict:
    db = config["configurable"]["db"]
    cards = crud.get_craving_cards(db, state.room_id)
    participants = crud.get_participants(db, state.room_id)

    pid_to_name = {p.id: p.display_name for p in participants}

    raw_inputs = {
        c.participant_id: {
            "display_name": pid_to_name.get(c.participant_id, "Unknown"),
            "veg": c.veg,
            "budget_max": c.budget_max,
            "cuisine_vibe": c.cuisine_vibe or "",
            "must_have": c.must_have or "",
            "allergies": list(c.allergies) if c.allergies else [],
            "deal_breakers": list(c.deal_breakers) if c.deal_breakers else [],
        }
        for c in cards
    }

    return {"raw_inputs": raw_inputs}
