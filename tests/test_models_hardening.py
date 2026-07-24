"""LLMSettings field bounds: nonsensical generation parameters (negative
max_tokens, temperature outside [0, 2]) are rejected at validation instead
of flowing to the provider and failing silently as a 400."""
import pytest
from pydantic import ValidationError

from xuse.models import LLMSettings


def test_max_tokens_must_be_at_least_1():
    with pytest.raises(ValidationError):
        LLMSettings(max_tokens=-5)
    with pytest.raises(ValidationError):
        LLMSettings(max_tokens=0)


def test_temperature_must_be_within_provider_range():
    with pytest.raises(ValidationError):
        LLMSettings(temperature=99)
    with pytest.raises(ValidationError):
        LLMSettings(temperature=-0.1)
    with pytest.raises(ValidationError):
        LLMSettings(temperature=2.5)


def test_boundary_values_are_accepted():
    assert LLMSettings(max_tokens=1).max_tokens == 1
    assert LLMSettings(temperature=0.0).temperature == 0.0
    assert LLMSettings(temperature=2.0).temperature == 2.0


def test_defaults_unchanged():
    settings = LLMSettings()
    assert settings.max_tokens == 1200
    assert settings.temperature == 0.7
