import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from string_utils import truncate, reverse_words, count_vowels, slugify


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------

def test_truncate_long_string():
    assert truncate("Hello, World!", 8) == "Hello..."


def test_truncate_exact_length():
    assert truncate("Hello", 5) == "Hello"


def test_truncate_short_string():
    assert truncate("Hi", 10) == "Hi"


def test_truncate_invalid_max_len():
    with pytest.raises(ValueError):
        truncate("test", 0)


def test_truncate_negative_max_len():
    with pytest.raises(ValueError):
        truncate("test", -1)


# ---------------------------------------------------------------------------
# reverse_words  (exposes the missing [::-1] bug)
# ---------------------------------------------------------------------------

def test_reverse_words_multiple():
    assert reverse_words("the quick brown fox") == "fox brown quick the"


def test_reverse_words_two_words():
    assert reverse_words("hello world") == "world hello"


def test_reverse_words_single():
    assert reverse_words("hello") == "hello"


def test_reverse_words_extra_whitespace():
    # split() with no arg collapses runs of whitespace
    assert reverse_words("  a  b  c  ") == "c b a"


# ---------------------------------------------------------------------------
# count_vowels
# ---------------------------------------------------------------------------

def test_count_vowels_mixed():
    assert count_vowels("Hello World") == 3


def test_count_vowels_uppercase():
    assert count_vowels("AEIOU") == 5


def test_count_vowels_none():
    assert count_vowels("gym") == 0


def test_count_vowels_empty():
    assert count_vowels("") == 0


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_special_chars():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_already_slug():
    assert slugify("already-a-slug") == "already-a-slug"
