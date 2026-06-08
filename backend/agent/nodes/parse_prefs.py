import asyncio
import json
import logging

from langgraph.types import RunnableConfig

from agent.state import CircleState, PrefSpec
from db import crud
from llm import chat, PARSE_MODEL

logger = logging.getLogger(__name__)

_SYSTEM = """You are extracting structured food preferences from a craving card.
Return JSON matching exactly this schema (no extra fields):
{
  "veg": "veg" | "non_veg" | "either",
  "budget_max": <integer rupees or null>,
  "allergies": ["item1", "item2"],
  "excludes": ["item1", "item2"],
  "soft": ["cuisine1", "descriptor2"]
}
Rules:
- allergies and excludes are HARD constraints — never move them to soft.
- soft is a list of concise strings: cuisine names, textures, descriptors (e.g. "biryani", "spicy", "light", "Chinese").
- If the person said "veg only" map to "veg"; "non-veg" or "non_veg" → "non_veg"; otherwise "either".
- Deal breakers from the card map directly to excludes.
- Return valid JSON only — no markdown fences."""


async def _parse_single(participant_id: str, card: dict) -> PrefSpec:
    user_msg = (
        f"veg/nonveg/either: {card['veg']}\n"
        f"budget: {card.get('budget_max') or 'any'}\n"
        f"cuisine vibe: {card.get('cuisine_vibe') or 'not specified'}\n"
        f"must have: {card.get('must_have') or 'not specified'}\n"
        f"allergies: {card.get('allergies', [])}\n"
        f"deal breakers: {card.get('deal_breakers', [])}"
    )

    try:
        raw = await chat(
            model=PARSE_MODEL,
            system=_SYSTEM,
            user=user_msg,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("LLM parse failed for %s: %s — using card data directly", participant_id, exc)
        data = {}

    veg_raw = data.get("veg", card["veg"])
    if veg_raw not in ("veg", "non_veg", "either"):
        veg_raw = card["veg"] if card["veg"] in ("veg", "non_veg", "either") else "either"

    return PrefSpec(
        participant_id=participant_id,
        display_name=card["display_name"],
        veg=veg_raw,
        budget_max=data.get("budget_max") or card.get("budget_max"),
        allergies=data.get("allergies") or list(card.get("allergies", [])),
        excludes=data.get("excludes") or list(card.get("deal_breakers", [])),
        soft=data.get("soft") or [],
    )


async def run(state: CircleState, config: RunnableConfig) -> dict:
    db = config["configurable"]["db"]
    supabase_url = config["configurable"].get("supabase_url", "")
    supabase_key = config["configurable"].get("supabase_key", "")

    tasks = [
        _parse_single(pid, card)
        for pid, card in state.raw_inputs.items()
    ]
    pref_specs: list[PrefSpec] = list(await asyncio.gather(*tasks))

    # Persist to DB
    for spec in pref_specs:
        crud.upsert_pref_spec(db, spec)

    # Update room status so frontend can detect parsing is done
    crud.set_room_status(db, state.room_id, "planning")

    logger.info("parse_prefs: wrote %d pref_specs for room %s", len(pref_specs), state.room_id)
    # Store as plain dicts for checkpoint-safety (see CircleState docstring)
    return {"pref_specs": [s.model_dump() for s in pref_specs]}
