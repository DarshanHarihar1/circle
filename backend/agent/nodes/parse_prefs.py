import logging
import re

from langgraph.types import RunnableConfig

from agent.state import CircleState, PrefSpec
from db import crud

logger = logging.getLogger(__name__)


def _split_tags(text: str | None) -> list[str]:
    """Split a free-text field (cuisine vibe / must have) into concise soft tags."""
    if not text:
        return []
    parts = re.split(r"[,/]|\band\b|&", text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _build_spec(participant_id: str, card: dict) -> PrefSpec:
    """Build a PrefSpec straight from the structured craving card.

    The card already captures veg / budget / allergies / deal-breakers as
    structured inputs — there is nothing to "interpret", so we copy them
    verbatim (an LLM here only risks flipping them). The only free-text fields
    are cuisine vibe + must-have, which become soft preference tags; must-have
    is also kept verbatim so the planner can prioritise that exact dish.
    """
    veg = card.get("veg")
    if veg not in ("veg", "non_veg", "either"):
        veg = "either"

    must_have = (card.get("must_have") or "").strip() or None
    soft = _split_tags(card.get("cuisine_vibe")) + _split_tags(must_have)

    return PrefSpec(
        participant_id=participant_id,
        display_name=card["display_name"],
        veg=veg,
        budget_max=card.get("budget_max"),
        allergies=list(card.get("allergies", [])),
        excludes=list(card.get("deal_breakers", [])),
        soft=soft,
        must_have=must_have,
    )


async def run(state: CircleState, config: RunnableConfig) -> dict:
    db = config["configurable"]["db"]

    pref_specs = [
        _build_spec(pid, card) for pid, card in state.raw_inputs.items()
    ]

    for spec in pref_specs:
        crud.upsert_pref_spec(db, spec)

    crud.set_room_status(db, state.room_id, "planning")

    logger.info("parse_prefs: wrote %d pref_specs for room %s", len(pref_specs), state.room_id)
    return {"pref_specs": [s.model_dump() for s in pref_specs]}
