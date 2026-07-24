"""LLMService: facade over the single OpenAI-compatible client.

Interactive MCP use needs no server-side LLM at all — the calling agent
(Claude, Codex, ...) does the analysis and writing. This service exists for
the API-backed paths: background automation, "auto" text generation, and
structured analysis. When no key is configured it returns None rather than
raising, so heuristic fallbacks (e.g. the analyzer's keyword relevance) keep
working keyless.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from xuse.core.config_loader import ConfigLoader

from .clients import DEFAULT_MODEL, build_client
from .prompts import build_structured_json_prompt
from .parsing import extract_json_from_response_text

logger = logging.getLogger(__name__)


class LLMService:
    """Single-client text generation (free-form and structured JSON)."""

    def __init__(self, config_loader: ConfigLoader):
        self.config_loader = config_loader
        self.client, self.resolved = build_client(config_loader)
        self.default_model = self.resolved.get("model", DEFAULT_MODEL)

    async def generate_text(
        self,
        prompt: str,
        service_preference: Optional[str] = None,  # DEPRECATED: ignored — single client
        system_prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        **call_params: Any,
    ) -> Optional[str]:
        if self.client is None:
            logger.info("LLM not configured; generate_text returning None.")
            return None
        model = (call_params.pop("model_name", None)
                 or call_params.pop("model", None)
                 or self.default_model)
        call_params.setdefault("max_tokens", 1200)  # reasoning-model headroom
        built_messages = messages if messages is not None else (
            ([{"role": "system", "content": system_prompt}] if system_prompt else [])
            + [{"role": "user", "content": prompt}]
        )
        try:
            response = await self.client.chat.completions.create(
                model=model, messages=built_messages, **call_params)
            text = (response.choices[0].message.content or "").strip()
            if not text:
                # Empty content with no exception usually means the completion
                # budget was consumed by hidden reasoning tokens (finish_reason
                # "length") — raise max_tokens, don't retry as-is.
                logger.warning(
                    "LLM returned empty content (model=%s, finish_reason=%s, max_tokens=%s).",
                    model,
                    getattr(response.choices[0], "finish_reason", None),
                    call_params.get("max_tokens"),
                )
            return text
        except Exception as e:
            logger.error("LLM generation failed: %s", e, exc_info=True)
            return None

    async def generate_structured(
        self,
        task_instruction: str,
        schema: Dict[str, Any],
        *,
        service_preference: Optional[str] = None,  # DEPRECATED: ignored — single client
        require_markdown_fences: bool = False,
        max_retries: int = 2,
        system_prompt: Optional[str] = None,
        few_shots: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        hard_character_limit: Optional[int] = None,
        **call_params: Any,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Generate a structured JSON object following the provided schema.
        Returns (parsed_json_dict, error_message). On success, error_message
        is None. The first attempt uses the API's JSON mode; later attempts
        fall back to plain text (some OpenAI-compatible providers reject
        response_format)."""
        prompt = build_structured_json_prompt(
            task_instruction,
            schema,
            require_markdown_fences=require_markdown_fences,
            few_shots=few_shots,
            hard_character_limit=hard_character_limit,
        )

        last_err: Optional[str] = None
        for attempt in range(max_retries + 1):
            params = dict(call_params)
            if attempt == 0 and "response_format" not in params:
                params["response_format"] = {"type": "json_object"}
            text = await self.generate_text(
                prompt=prompt,
                system_prompt=system_prompt,
                **params,
            )
            if not text:
                last_err = "No response from LLM"
                continue

            data, err = extract_json_from_response_text(text)
            if data is not None:
                return data, None
            last_err = err or "Unknown parse error"

        return None, last_err or "Failed to produce valid JSON after retries"
