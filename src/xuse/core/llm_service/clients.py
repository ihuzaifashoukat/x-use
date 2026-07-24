"""Single OpenAI-compatible LLM client.

Every provider that matters — OpenAI, OpenRouter, Azure (its OpenAI-compatible
endpoint), Gemini (its OpenAI-compatible endpoint), local vLLM/Ollama — speaks
the OpenAI chat-completions API, so one AsyncOpenAI client with a configurable
base_url covers all of them. The old per-provider clients (gemini/azure) and
the service-preference fallback loop are gone.

Config (config/settings.json)::

    "llm": {
        "api_key": "...",                            // or env OPENAI_API_KEY
        "base_url": "https://openrouter.ai/api/v1",  // or env OPENAI_BASE_URL
        "model": "openai/gpt-4o-mini"                // or env OPENAI_MODEL
    }

Resolution: env vars win over the "llm" block; the legacy
api_keys.openai_api_key is honored as a last-resort key source. Legacy
gemini/azure keys are ignored with a one-time warning. Key values are never
logged; only the supplying source label is.
"""
import logging
import os
from typing import Any, Dict, Optional, Tuple

from xuse.core.config_loader import ConfigLoader
from xuse.utils.env import load_env

from .constants import API_KEY_PLACEHOLDERS

logger = logging.getLogger(__name__)

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except Exception:  # pragma: no cover - SDK is a hard dependency in practice
    AsyncOpenAI = None  # type: ignore
    OPENAI_AVAILABLE = False

DEFAULT_MODEL = "gpt-4o-mini"

# Legacy per-provider keys that are now ignored (warned about once per build).
_LEGACY_IGNORED_KEYS = (
    "gemini_api_key",
    "azure_openai_api_key",
    "azure_openai_endpoint",
    "azure_openai_deployment",
    "azure_api_version",
)


def _is_api_key_valid(key_name: str, key_value: Optional[str]) -> bool:
    if not key_value:
        return False
    placeholder = API_KEY_PLACEHOLDERS.get(key_name)
    if placeholder and key_value.strip().upper() == placeholder:
        return False
    if "YOUR_" in key_value.upper() and "_KEY" in key_value.upper():
        return False
    return True


def _resolve_api_key(key_name: str, config_value: Optional[str]) -> Tuple[Optional[str], str]:
    """Resolve an API key: OPENAI_API_KEY env var first, then the config value.

    Returns (value, source_label). The value is never logged; the source
    label is safe to log.
    """
    env_value = os.environ.get("OPENAI_API_KEY")
    if env_value and env_value.strip():
        return env_value, "env var OPENAI_API_KEY"
    if config_value and str(config_value).strip():
        return str(config_value), "settings.json"
    return None, "none"


def build_client(config_loader: ConfigLoader) -> Tuple[Optional[Any], Dict[str, Any]]:
    """Build the single OpenAI-compatible client.

    Returns (client_or_None, resolved) where resolved is the secret-free
    dict {"model", "base_url", "key_source"}.
    """
    load_env()  # idempotent; no-op when no .env file exists

    llm_block = config_loader.get_setting("llm", {}) or {}
    if not isinstance(llm_block, dict):
        llm_block = {}
    api_keys = config_loader.get_setting("api_keys", {}) or {}
    if not isinstance(api_keys, dict):
        api_keys = {}

    legacy_present = [k for k in _LEGACY_IGNORED_KEYS if api_keys.get(k)]
    if legacy_present:
        logger.warning(
            "Legacy per-provider keys %s are ignored. The LLM client is a single "
            "OpenAI-compatible client now — move to the 'llm' block "
            "(api_key / base_url / model).", legacy_present)

    api_key, key_source = _resolve_api_key(
        "openai_api_key", llm_block.get("api_key") or api_keys.get("openai_api_key"))
    base_url = os.environ.get("OPENAI_BASE_URL") or llm_block.get("base_url") or None
    model = os.environ.get("OPENAI_MODEL") or llm_block.get("model") or DEFAULT_MODEL

    resolved = {"model": model, "base_url": base_url, "key_source": key_source}

    if not OPENAI_AVAILABLE:
        logger.warning("openai SDK not installed; server-side LLM features disabled.")
        return None, resolved
    if not _is_api_key_valid("openai_api_key", api_key):
        logger.info(
            "No LLM API key configured (source: %s); server-side generation disabled. "
            "Interactive MCP use does not need one — the calling agent writes the text.",
            key_source)
        return None, resolved
    try:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        logger.info(
            "LLM client initialized (model=%s, base_url=%s, key source: %s).",
            model, base_url or "api.openai.com", key_source)
        return client, resolved
    except Exception as e:
        logger.error("Failed to initialize the LLM client: %s", e, exc_info=True)
        return None, resolved
