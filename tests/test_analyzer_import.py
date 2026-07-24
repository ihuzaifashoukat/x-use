"""Importing the analyzer must not reconfigure the root logger or read config
at import time — logging setup happens lazily and never clobbers a host
application's handlers.
"""
import logging
import sys


def _fresh_import_analyzer():
    for mod in [m for m in sys.modules if m.startswith("xuse.features.analyzer")]:
        del sys.modules[mod]
    import xuse.features.analyzer  # noqa: F401
    return xuse.features.analyzer


def test_importing_analyzer_has_no_root_logger_side_effect():
    root = logging.getLogger()
    before = list(root.handlers)
    _fresh_import_analyzer()
    assert list(root.handlers) == before


def test_analyzer_init_preserves_host_root_handlers():
    module = _fresh_import_analyzer()
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    before = list(root.handlers)
    try:
        fake_llm = type("L", (), {"config_loader": None})()
        module.TweetAnalyzer(llm_service=fake_llm)
        assert list(root.handlers) == before
    finally:
        root.removeHandler(sentinel)
