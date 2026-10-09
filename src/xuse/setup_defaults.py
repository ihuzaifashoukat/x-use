"""Small, credential-free setup templates available in installed wheels.

The complete source preset library remains in presets/. These defaults allow
PyPI users to configure the current MCP runtime without a source checkout.
"""

DEFAULT_SETTINGS = {
    "mcp": {
        "browser_backend": "patchright",
        "browser_headless": True,
        "draft_mode": True,
    },
    "queue": {"auto_drain": {"enabled": False}},
    "logging": {"level": "INFO"},
}

DEFAULT_ACCOUNTS = [{
    "account_id": "your_account_id_here",
    "is_active": False,
    "cookie_file_path": "data/cookies/your_cookies.json",
    "proxy": None,
    "post_to_community": False,
    "target_keywords": [],
    "competitor_profiles": [],
    "persona": "Write brief, specific messages based on verified facts. Respect opt-outs and review each exact message before approval.",
    "action_config": {
        "enable_competitor_reposts": False,
        "enable_keyword_replies": False,
        "enable_keyword_retweets": False,
        "enable_content_curation_posts": False,
        "enable_liking_tweets": False,
        "enable_community_engagement": False,
    },
}]
