import logging
from typing import Optional, Dict, Any

from xuse.core.llm_service import LLMService
from xuse.models import LLMSettings

logger = logging.getLogger(__name__)

MAX_TWEET_CHARS = 270

def _clamp(text: str, limit: int = MAX_TWEET_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    trimmed = text[:limit]
    # Soft trim at a boundary if possible (avoid mid-word punctuation)
    return trimmed.rstrip(" .,;:!\n")

_PROMPT_STARTERS = ("write", "generate", "compose", "draft", "create", "craft")
_PROMPT_PHRASES = (
    "write an engaging",
    "write a post",
    "write a tweet",
    "generate a post",
    "generate a tweet",
    "post about:",
    "tweet about:",
)


def _looks_like_generation_prompt(text: str) -> bool:
    """Decide whether `text` is an instruction to generate a post rather than
    ready-to-publish text.

    The old substring check ("generate"/"write"/"post" anywhere) sent ready
    posts that merely mention those words ("my new blog post!") through the
    LLM and rewrote them. Now a text counts as a prompt only when it STARTS
    with an imperative generation verb ("Write an engaging X post about: …")
    or contains an explicit instruction phrase.
    """
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    first_word = lowered.split(None, 1)[0].strip(":,.!")
    if first_word in _PROMPT_STARTERS:
        return True
    return any(phrase in lowered for phrase in _PROMPT_PHRASES)


async def generate_post_text_if_needed(
    prompt_text: str,
    llm_settings: Optional[LLMSettings],
    llm_service: LLMService,
) -> str:
    """Generate final post text if the provided text looks like a prompt.

    Returns the original text if no generation is required. Returns "" when
    generation was required but every attempt failed — the prompt itself is
    never returned as post text, so callers must treat "" as a failure.
    """
    text = prompt_text
    if not llm_settings:
        return text

    if not _looks_like_generation_prompt(prompt_text):
        return text

    logger.info("Attempting structured LLM generation for post content.")
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Final post text under 270 characters. Avoid trailing hashtags."},
            "hashtags": {"type": "array", "items": {"type": "string"}, "description": "1-4 concise hashtags (include #)."},
            "mentions": {"type": "array", "items": {"type": "string"}},
            "safety": {
                "type": "object",
                "properties": {
                    "needs_review": {"type": "boolean"},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["needs_review"],
            },
        },
        "required": ["text", "hashtags", "safety"],
    }

    task = (
        f"Generate an engaging X (Twitter) post per the prompt: {prompt_text}. "
        f"Return JSON strictly matching the schema."
    )
    data, err = await llm_service.generate_structured(
        task_instruction=task,
        schema=schema,
        service_preference=llm_settings.service_preference,
        model_name=llm_settings.model_name_override,
        max_tokens=llm_settings.max_tokens,
        # `is not None` so an explicit temperature=0.0 survives to the provider.
        temperature=llm_settings.temperature if llm_settings.temperature is not None else 0.7,
        hard_character_limit=MAX_TWEET_CHARS,
    )
    if data and isinstance(data, dict) and data.get("text"):
        base = (data.get("text") or "").strip()
        hashtags = data.get("hashtags") or []
        suffix = (" " + " ".join([h if h.startswith("#") else f"#{h}" for h in hashtags[:4]])) if hashtags else ""
        # Ensure final composed text does not exceed MAX_TWEET_CHARS
        available = MAX_TWEET_CHARS - len(suffix)
        available = max(0, available)
        base = _clamp(base, available)
        composed = (base + suffix).strip()
        safety = (data.get("safety") or {})
        if isinstance(safety, dict) and safety.get("needs_review"):
            logger.warning(f"LLM flagged content for review. Reasons: {safety.get('reasons')}")
        logger.info("Structured content generated successfully.")
        return _clamp(composed)

    logger.info(f"Structured generation failed ({err}). Falling back to plain text generation.")
    # Strengthen fallback request with explicit constraints
    constrained_prompt = (
        f"{prompt_text}\n\nConstraints: Reply with a single, concise tweet under {MAX_TWEET_CHARS} characters."
        " No hashtags in the text; you may include them only if the prompt explicitly asks."
    )
    generated_text = await llm_service.generate_text(
        prompt=constrained_prompt,
        service_preference=llm_settings.service_preference,
        model_name=llm_settings.model_name_override,
        max_tokens=llm_settings.max_tokens,
        temperature=llm_settings.temperature,
    )
    if not generated_text:
        logger.error("LLM generation failed on every attempt; no usable post text.")
        return ""  # never return the prompt itself as post text
    return _clamp(generated_text)


async def maybe_generate_quote_text(
    quote_text_prompt_or_direct: Optional[str],
    llm_settings_for_quote: Optional[LLMSettings],
    llm_service: LLMService,
) -> Optional[str]:
    """Generate quote text if a prompt was provided; otherwise return the direct text."""
    if not quote_text_prompt_or_direct:
        return None
    if not llm_settings_for_quote:
        return _clamp(quote_text_prompt_or_direct)

    lowered = quote_text_prompt_or_direct.lower()
    if not ("generate quote for" in lowered or "write a quote about" in lowered):
        return _clamp(quote_text_prompt_or_direct)

    logger.info("Generating quote text from prompt for quote tweet.")
    constrained_prompt = (
        f"{quote_text_prompt_or_direct}\n\nConstraints: Keep it under {MAX_TWEET_CHARS} characters, concise, and natural."
        " Avoid hashtags unless explicitly requested."
    )
    generated_quote = await llm_service.generate_text(
        prompt=constrained_prompt,
        service_preference=llm_settings_for_quote.service_preference,
        model_name=llm_settings_for_quote.model_name_override,
        max_tokens=llm_settings_for_quote.max_tokens,
        temperature=llm_settings_for_quote.temperature,
    )
    if not generated_quote:
        logger.error("Failed to generate quote text from prompt.")
        return None
    return _clamp(generated_quote)
