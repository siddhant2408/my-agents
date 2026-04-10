import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from math_utils import safe_divide, clamp, is_prime


# ---------------------------------------------------------------------------
# safe_divide  (exposes the missing zero-guard bug)
# ---------------------------------------------------------------------------

def test_safe_divide_normal():
    assert safe_divide(10, 2) == 5.0


def test_safe_divide_float():
    assert safe_divide(7.5, 2.5) == 3.0


def test_safe_divide_by_zero():
    with pytest.raises(ValueError, match="Cannot divide by zero"):
        safe_divide(10, 0)


def test_safe_divide_negative():
    assert safe_divide(-9, 3) == -3.0


# ---------------------------------------------------------------------------
# clamp
# ---------------------------------------------------------------------------

def test_clamp_within_range():
    assert clamp(5, 1, 10) == 5


def test_clamp_below_min():
    assert clamp(-5, 0, 10) == 0


def test_clamp_above_max():
    assert clamp(15, 0, 10) == 10


def test_clamp_at_boundary():
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10


# ---------------------------------------------------------------------------
# is_prime
# ---------------------------------------------------------------------------

def test_is_prime_small_primes():
    assert is_prime(2) is True
    assert is_prime(3) is True
    assert is_prime(7) is True


def test_is_prime_composites():
    assert is_prime(4) is False
    assert is_prime(9) is False
    assert is_prime(15) is False


def test_is_prime_edge_cases():
    assert is_prime(0) is False
    assert is_prime(1) is False
    assert is_prime(-1) is False
