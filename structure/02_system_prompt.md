# System Prompt

The system prompt is **Claude's briefing before the conversation starts**. It never comes from the user. It's your voice as the developer, setting up who Claude is and how it should behave for the entire session.

---

## Where it sits in the API call

It's a separate parameter from `messages` — not part of the conversation history:

```python
response = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    system=system_prompt,   # ← sits here, separate
    tools=TOOLS,
    messages=messages,      # ← conversation history sits here
)
```

Internally, the API renders them in this order before Claude sees anything:

```
tools    →    system    →    messages
  (1)           (2)            (3)
```

This matters for prompt caching (stable things first), but the key point is: **system prompt always comes before the conversation**.

---

## What it does in the testing agent

```python
system_prompt = """You are an expert QA engineer and testing agent.

Your job is to:
1. Discover and understand the codebase structure
2. Run the test suite
3. For any failures: read the relevant source file and explain exactly what
   the bug is, which line it's on, and how to fix it
4. Provide a clear final summary: how many passed, how many failed,
   and actionable fix suggestions for each failure

Always read the source file of a failing module before explaining the bug —
don't guess from the test output alone.

Be concise and practical. Developers reading your output are busy."""
```

It's doing four distinct things.

---

## Thing 1: Persona — `"You are an expert QA engineer"`

This single line changes how Claude reasons. Claude has absorbed an enormous amount of writing about how QA engineers think — what they care about, how they communicate, what a good bug report looks like.

Compare what you get without vs with it:

```
Without persona:
  "Test 'test_divide_by_zero' failed. The error is ZeroDivisionError."

With persona:
  "test_divide_by_zero FAILED — root cause: divide() on line 15 has no
   guard for b=0. The function raises ZeroDivisionError instead of the
   expected ValueError. Fix: add `if b == 0: raise ValueError('Cannot
   divide by zero')` before the return statement."
```

Same tools. Same test output. Different persona → different quality of analysis.

---

## Thing 2: Job description — the numbered list

```
Your job is to:
1. Discover and understand the codebase structure
2. Run the test suite
3. For any failures: read the relevant source...
4. Provide a clear final summary...
```

This is **not** a hardcoded script. Claude doesn't follow it mechanically like a `for` loop. It uses it as a checklist to reason against.

If the user says *"just run tests/test_math.py, don't explore"*, Claude will skip step 1 because the user's instruction overrides it. The system prompt sets defaults — the user's message in `messages` can always narrow or redirect.

The key difference between this and code:

```python
# Code — rigid, always executes in this order
list_files()
run_tests()
if failures: read_file()

# System prompt — flexible, Claude reasons about whether each step applies
"Your job is to: 1. Discover... 2. Run... 3. For failures..."
```

---

## Thing 3: A hard rule — `"Always read the source file..."`

```
Always read the source file of a failing module before explaining the bug —
don't guess from the test output alone.
```

The word **"Always"** is load-bearing. Without it, Claude might skip `read_file` when the traceback looks obvious and give a hallucinated explanation. With it, Claude treats this as a non-negotiable constraint.

This is the system prompt's most powerful use case: **enforcing behaviors Claude might otherwise skip as an optimization**.

Other examples of hard rules that belong in system prompts:

```
Always cite the line number when describing a bug.
Never suggest deleting test files as a fix.
If you cannot find the source file, say so explicitly — do not guess.
```

The pattern: they prevent Claude from taking a *plausible but wrong* shortcut.

---

## Thing 4: Output format — `"Be concise and practical"`

```
Be concise and practical. Developers reading your output are busy.
```

Without this, Claude's final report might look like:

> *"Thank you for asking me to run the test suite. I've carefully analyzed the results and I'm happy to share my findings with you. First, let me walk you through what I discovered..."*

With it:

> *"8 passed, 1 failed.*
> *FAIL: test_divide_by_zero — divide() missing zero guard on line 15. Fix: raise ValueError before returning."*

Format instructions are often overlooked but make a large practical difference.

---

## System prompt vs user message — when to use each

| Use system prompt for | Use user messages for |
|---|---|
| Persona / role | The actual task |
| Rules that always apply | Task-specific instructions |
| Output format | Dynamic context (filenames, user data) |
| Things that never change between requests | Things that change per request |

A concrete example. This belongs in the **system prompt** — it's always true:

```python
system = "Always output your final report in markdown with a ## Summary section."
```

This belongs in the **user message** — it's specific to this run:

```python
messages = [{"role": "user", "content": "Run the tests in tests/auth/"}]
```

---

## The wrong way to use a system prompt

The most common mistake is putting dynamic data in the system prompt:

```python
# ❌ Wrong — timestamp changes every request, breaks caching,
#            and the system prompt isn't the right place for this
system = f"""You are a QA agent.
Current time: {datetime.now()}
Testing directory: {user_chosen_dir}
"""
```

Put dynamic data in the user message instead:

```python
# ✅ Right — system prompt is stable, dynamic data goes in messages
system = "You are an expert QA engineer..."

messages = [{
    "role": "user",
    "content": f"Run the tests in {user_chosen_dir}. Today is {date}."
}]
```

Why does this matter? The system prompt is cached by the API after the first request. If it changes on every call (because of a timestamp), you pay full price every time. If it's stable, subsequent calls are dramatically cheaper and faster.

---

## System prompt vs tool descriptions — the division of labor

Both influence Claude's behavior. Here's how they divide the work:

```
System prompt                     Tool descriptions
─────────────────────────────     ──────────────────────────────
WHO Claude is                     WHAT each tool does
HOW Claude should behave          WHEN to call each tool
FORMAT of responses               WHAT arguments to provide
Rules that span all tools         Rules specific to one tool
```

In the testing agent:

```python
# System prompt — applies to everything
"Always read the source file of a failing module before explaining the bug"

# Tool description — specific to run_tests
"Use this after you have a sense of the codebase structure."
```

Both work together. The system prompt sets the overall QA mindset. The tool descriptions handle micro-decisions within that mindset.

---

## A production-quality system prompt pattern

As agents get more complex, system prompts get more structured:

```python
system_prompt = """
## Role
You are a QA engineer agent. Your goal is accurate, actionable test reports.

## Workflow
1. Always start by discovering files with list_files
2. Run the full suite before reading any source
3. For each failure: read the source, identify the exact bug, suggest the fix

## Output format
End every response with a markdown report:

### Test Summary
- Passed: N
- Failed: N

### Failures
**test_name** — one-line root cause
- File: src/foo.py, line N
- Fix: [specific code change]

## Constraints
- Never suggest deleting tests as a fix
- Never guess at a bug without reading the source file
- If a file is not found, report it — do not assume its contents
"""
```

Sections: **Role**, **Workflow**, **Output format**, **Constraints**. Each shapes a different dimension of Claude's behavior. At scale, this structure makes system prompts easier to maintain and reason about.
