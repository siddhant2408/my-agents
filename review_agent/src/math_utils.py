"""
Math utility functions.

Deliberate issues planted for the review agent to find and fix:
  1. Line 3 — `import os` is unused  (flake8 F401)
  2. Line 20 — `safe_divide` performs no zero check; raises a bare
               ZeroDivisionError instead of the expected ValueError
               (logic bug caught by test_safe_divide_by_zero)
"""

import os  # F401: 'os' imported but unused


def safe_divide(a: float, b: float) -> float:
    """Divide *a* by *b*.

    Raises:
        ValueError: if *b* is zero.
    """
    # BUG: missing zero guard — should raise ValueError when b == 0
    return a / b


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to the inclusive range [*lo*, *hi*].

    Returns *lo* if *value* < *lo*, *hi* if *value* > *hi*, otherwise
    *value* unchanged.
    """
    return max(lo, min(hi, value))


def is_prime(n: int) -> bool:
    """Return ``True`` if *n* is a prime number, ``False`` otherwise.

    Uses trial division up to √n.  Returns ``False`` for values less than 2.
    """
    if n < 2:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True
