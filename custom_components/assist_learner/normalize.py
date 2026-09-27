"""Utterance normalization."""

import re

TEMPLATE_CHARS = frozenset("[](){}<>|")
MIN_WORDS = 3
STOP_PHRASES = frozenset(
    {
        "yes",
        "no",
        "ok",
        "okay",
        "do it",
        "yes please",
        "no thanks",
        "go ahead",
        "sure",
        "thanks",
        "thank you",
        "never mind",
        "cancel",
        "stop",
        "that's right",
        "that is right",
        "do that",
        "yes do it",
        "please do",
    }
)

_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")


class NormalizationError(ValueError):
    """The utterance can't become a sentence."""


def normalize_text(raw: str) -> str:
    """Reduce an utterance to lowercase words, apostrophes, and plain numbers."""
    text = raw.lower().replace("\u2019", "'")
    text = re.sub(r"(\d)\s*%", r"\1 percent ", text)
    text = re.sub(r"(\d)\s*\u00b0\s*[fc]?\b", r"\1 degrees ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
    text = re.sub(r"[^\w\s'.]", " ", text)
    text = re.sub(r"(?<!\w)'|'(?!\w)", " ", text)
    return " ".join(text.split())


def check_utterance(raw: str) -> str:
    """Return the normalized utterance, or raise if it can't be learned."""
    if any(char in TEMPLATE_CHARS for char in raw):
        raise NormalizationError("utterance contains sentence-template characters")
    text = normalize_text(raw)
    if text in STOP_PHRASES:
        raise NormalizationError("utterance is a stop phrase")
    if len(text.split()) < MIN_WORDS:
        raise NormalizationError(f"utterance has fewer than {MIN_WORDS} words")
    return text


def is_number(token: str) -> bool:
    """Return True for a plain integer or decimal token."""
    return bool(_NUMBER.match(token))


def contains_phrase(text: str, phrase: str) -> bool:
    """Return True if the normalized phrase appears in text on word boundaries."""
    phrase = normalize_text(phrase)
    if not phrase:
        return False
    return f" {phrase} " in f" {text} "
