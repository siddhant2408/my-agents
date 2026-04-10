# Sample source code the agent will test.
# It has a deliberate bug in divide() so we can see the agent catch it.

def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b

def divide(a, b):
    # BUG: missing zero-division guard
    return a / b

def factorial(n):
    if n < 0:
        raise ValueError("factorial of negative number")
    if n == 0:
        return 1
    return n * factorial(n - 1)
