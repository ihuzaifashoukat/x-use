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
import importlib.util
import logging
import os
from typing import Any, Dict, Optional, Tuple

from xuse.core.config_loader import ConfigLoader
from xuse.utils.env import load_env

from .constants import API_KEY_PLACEHOLDERS

logger = logging.getLogger(__name__)

# Probed without importing the SDK. `import openai` costs roughly three seconds
# and every process start paid it, including MCP stdio boot, where the client on
# the other end is waiting on the handshake before it will ask for anything.
# Nothing in this module needs the SDK until a client is actually constructed,
# so the import moved down to that one spot.
OPENAI_AVAILABLE = importlib.util.find_spec("openai") is not None

DEFAULT_MODEL = "gpt-4o-mini"

# Legacy per-provider keys that are now ignored (warned about once per build).
_LEGACY_IGNORED_KEYS = (
    "gemini_api_key",
    "azure_openai_api_key",
    "azure_openai_endpoint",
    "azure_openai_deployment",
    "azure_api_version",
)


# Placeholder markers: values containing these are template stubs, not keys.
# Shared with doctor so a placeholder that disables the runtime client is
# reported the same way on both sides.
_PLACEHOLDER_MARKERS = (
    "YOUR_",      # YOUR_OPENAI_API_KEY, YOUR_KEY_HERE, ...
    "YOUR-",      # your-key, your-api-key, ...
    "CHANGEME",
    "CHANGE_ME",
    "CHANGE-ME",
    "REPLACE_ME",
    "REPLACE-ME",
    "PASTE_",
    "INSERT_",
)
_PLACEHOLDER_EXACT = {"SK-...", "SK-XXX", "XXX", "TODO", "TBD"}


def is_placeholder_key(key_name: str, key_value: Optional[str]) -> bool:
    """True when key_value is present but is a template placeholder rather
    than a real key (e.g. the wizard's YOUR_OPENAI_API_KEY default)."""
    if key_value is None:
        return False
    value = str(key_value).strip()
    if not value:
        return False
    upper = value.upper()
    placeholder = API_KEY_PLACEHOLDERS.get(key_name)
    if placeholder and upper == placeholder:
        return True
    if upper in _PLACEHOLDER_EXACT:
        return True
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def _is_api_key_valid(key_name: str, key_value: Optional[str]) -> bool:
    if not key_value or not str(key_value).strip():
        return False
    return not is_placeholder_key(key_name, key_value)


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
            "Interactive MCP use does not need one, the calling agent writes the text.",
            key_source)
        return None, resolved
    try:
        from openai import AsyncOpenAI  # deferred, see OPENAI_AVAILABLE above

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        logger.info(
            "LLM client initialized (model=%s, base_url=%s, key source: %s).",
            model, base_url or "api.openai.com", key_source)
        return client, resolved
    except Exception as e:
        logger.error("Failed to initialize the LLM client: %s", e, exc_info=True)
        return None, resolved
