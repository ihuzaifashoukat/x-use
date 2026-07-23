"""Shared legacy-key normalization lives in core and is re-exported."""
from xuse.core.config_loader import LEGACY_ACCOUNT_KEY_MAP, normalize_account_dict
from xuse.mcp import sessions


def test_legacy_keys_map_to_current_names():
    raw = {"account_id": "a", "target_keywords_override": ["ai"],
           "action_config_override": {"max_likes_per_run": 1}}
    normalized = normalize_account_dict(raw)
    assert normalized["target_keywords"] == ["ai"]
    assert normalized["action_config"] == {"max_likes_per_run": 1}


def test_current_key_wins_over_legacy():
    raw = {"account_id": "a", "target_keywords": ["new"],
           "target_keywords_override": ["old"]}
    assert normalize_account_dict(raw)["target_keywords"] == ["new"]


def test_sessions_reexports_shared_copy():
    assert sessions.normalize_account_dict is normalize_account_dict
    assert sessions.LEGACY_ACCOUNT_KEY_MAP is LEGACY_ACCOUNT_KEY_MAP
