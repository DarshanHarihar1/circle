import logging

from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger("llm")

_client = AsyncOpenAI(
    base_url=settings.OPENROUTER_BASE_URL,
    api_key=settings.OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": "https://circle.app",
        "X-Title": "Circle",
    },
)

PARSE_MODEL = "meta-llama/llama-3.3-70b-instruct"
SCORE_MODEL = "meta-llama/llama-3.1-8b-instruct"
RATIONALE_MODEL = "liquid/lfm-2.5-1.2b-thinking:free"


async def chat(model: str, system: str, user: str, response_format=None) -> str:
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    if response_format:
        kwargs["response_format"] = response_format

    logger.info("LLM -> %s\n  system: %s\n  user: %s", model, system[:500], user[:1500])
    resp = await _client.chat.completions.create(**kwargs)
    out = resp.choices[0].message.content
    logger.info("LLM <- %s\n  output: %s", model, out)
    return out
