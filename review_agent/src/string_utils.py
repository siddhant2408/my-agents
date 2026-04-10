"""
String utility functions.

Deliberate issues planted for the review agent to find and fix:
  1. Line 3 — `import json` is unused  (flake8 F401)
  2. Line 24  — `reverse_words` returns words in original order instead of
               reversed order (logic bug caught by test_reverse_words)
"""

import re
import json  # F401: 'json' imported but unused


VOWELS = "aeiouAEIOU"


def truncate(text: str, max_len: int) -> str:
    """Truncate *text* to at most *max_len* characters.

    If the string is longer than *max_len*, it is cut and '...' is appended
    so the total length equals *max_len*.

    Raises:
        ValueError: if *max_len* is not a positive integer.
    """
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def reverse_words(sentence: str) -> str:
    """Return *sentence* with the order of its words reversed.

    Words are delimited by whitespace.  Leading/trailing whitespace is
    ignored and the result is a single-space-separated string.

    Example:
        >>> reverse_words("the quick brown fox")
        'fox brown quick the'
    """
    words = sentence.split()
    # BUG: missing [::-1] — returns original order instead of reversed
    return " ".join(words)


def count_vowels(text: str) -> int:
    """Return the number of vowel characters (a e i o u, case-insensitive) in *text*."""
    return sum(1 for ch in text if ch in VOWELS)


def slugify(text: str) -> str:
    """Convert *text* to a URL-safe slug (lowercase, hyphens, no special chars)."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text
