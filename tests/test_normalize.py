"""Utterance normalization."""

import pytest

from custom_components.assist_learner.normalize import (
    NormalizationError,
    check_utterance,
    normalize_text,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("It's too dark in here!", "it's too dark in here"),
        ("Set it to 40%", "set it to 40 percent"),
        ("Make it 72\u00b0F", "make it 72 degrees"),
        ("Dim to 2.5, please.", "dim to 2.5 please"),
        ("It\u2019s   cold", "it's cold"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    """Punctuation goes, numbers and apostrophes stay."""
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("turn on [the] lights", "template"),
        ("turn on {name}", "template"),
        ("lights on | off", "template"),
        ("lights on", "fewer than"),
        ("Yes, do it", "stop phrase"),
        ("lamp on now", None),
        ("okay", "stop phrase"),
        ("Do it!", "stop phrase"),
        ("thank you", "stop phrase"),
    ],
)
def test_rejected(raw: str, reason: str) -> None:
    """Template characters, short utterances, and stop phrases are rejected."""
    if reason is None:
        assert check_utterance(raw) == raw
        return
    with pytest.raises(NormalizationError, match=reason):
        check_utterance(raw)
