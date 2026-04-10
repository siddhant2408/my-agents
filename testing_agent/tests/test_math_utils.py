import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from math_utils import add, subtract, multiply, divide, factorial

# --- passing tests ---

def test_add_positive():
    assert add(2, 3) == 5

def test_add_negative():
    assert add(-1, -1) == -2

def test_subtract():
    assert subtract(10, 4) == 6

def test_multiply():
    assert multiply(3, 4) == 12

def test_factorial_zero():
    assert factorial(0) == 1

def test_factorial_positive():
    assert factorial(5) == 120

def test_factorial_negative_raises():
    with pytest.raises(ValueError):
        factorial(-1)

# --- failing test (exposes the bug in divide) ---

def test_divide_by_zero():
    # This should raise ZeroDivisionError but divide() has no guard,
    # so it crashes with an unhandled ZeroDivisionError instead of a
    # clean ValueError — the agent should notice this.
    with pytest.raises(ValueError, match="Cannot divide by zero"):
        divide(10, 0)

def test_divide_normal():
    assert divide(10, 2) == 5.0
