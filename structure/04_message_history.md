# Message History

Message history is the `messages` list you pass on every API call. It's not a side feature — it **is** Claude's memory. Claude has zero state between API calls. Every call starts fresh, and the only context Claude has is what's in that list.

---

## The structure of a single message

Every entry in `messages` follows the same shape:

```python
{
    "role": "user" | "assistant",
    "content": str | list[blocks]
}
```

`content` can be either a plain string (simple case) or a list of typed blocks (complex case):

```python
# Simple — just text
{"role": "user", "content": "Run the tests"}

# Complex — multiple blocks
{"role": "user", "content": [
    {"type": "text",        "text": "Here's the result:"},
    {"type": "tool_result", "tool_use_id": "toolu_001", "content": "8 passed"}
]}
```

In an agent you'll almost always be dealing with the block form because tool calls and results are blocks.

---

## The four block types you'll encounter

**`text`** — regular prose, from either role:
```python
{"type": "text", "text": "Let me run the tests first."}
```

**`tool_use`** — Claude requesting a tool (only in `assistant` turns):
```python
{
    "type":  "tool_use",
    "id":    "toolu_01XzY...",   # unique per call
    "name":  "run_tests",
    "input": {"target": "tests/", "verbose": True}
}
```

**`tool_result`** — your result back to Claude (only in `user` turns):
```python
{
    "type":        "tool_result",
    "tool_use_id": "toolu_01XzY...",   # matches the tool_use id above
    "content":     "1 failed, 8 passed..."
}
```

**`thinking`** — Claude's internal reasoning (only appears when thinking is enabled):
```python
{"type": "thinking", "thinking": "The test failed because...", "signature": "..."}
```

---

## How the list grows over a full agent run

Here is the exact state of `messages` at every point during a full run. Read top to bottom like a play-by-play:

```python
# ── Before loop starts ─────────────────────────────────────────────────────
messages = [
    {"role": "user", "content": "Run the full test suite and explain failures."}
]

# ── After iteration 1 (Claude calls list_files) ────────────────────────────
messages = [
    {"role": "user", "content": "Run the full test suite and explain failures."},

    {"role": "assistant", "content": [
        TextBlock(text="Let me discover the project structure first."),
        ToolUseBlock(id="toolu_001", name="list_files", input={"directory": "tests"})
    ]},

    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_001",
         "content": "tests/test_math_utils.py"}
    ]}
]

# ── After iteration 2 (Claude calls run_tests) ─────────────────────────────
messages = [
    {"role": "user", "content": "Run the full test suite and explain failures."},

    {"role": "assistant", "content": [
        TextBlock(text="Let me discover the project structure first."),
        ToolUseBlock(id="toolu_001", name="list_files", input={"directory": "tests"})
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_001",
         "content": "tests/test_math_utils.py"}
    ]},

    {"role": "assistant", "content": [
        TextBlock(text="Now running the tests."),
        ToolUseBlock(id="toolu_002", name="run_tests", input={"target": "tests/"})
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_002",
         "content": "[exit code: 1 — FAILED]\n1 failed, 8 passed\ntest_divide_by_zero: ZeroDivisionError"}
    ]}
]

# ── After iteration 3 (Claude reads the source) ────────────────────────────
messages = [
    ...same as above...,

    {"role": "assistant", "content": [
        TextBlock(text="test_divide_by_zero failed. Let me read the source."),
        ToolUseBlock(id="toolu_003", name="read_file", input={"filepath": "src/math_utils.py"})
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_003",
         "content": "14 | def divide(a, b):\n15 |     return a / b\n"}
    ]}
]

# ── After iteration 4 (Claude gives final answer) ──────────────────────────
messages = [
    ...same as above...,

    {"role": "assistant", "content": [
        TextBlock(text="8 passed, 1 failed.\n\nFAIL: test_divide_by_zero\n- File: src/math_utils.py, line 15\n- Bug: divide() has no zero guard...")
    ]}
]
```

Each iteration appends exactly **two entries**: one `assistant`, one `user`. The final `assistant` entry has no corresponding `user` entry because the loop exits at `end_turn`.

---

## The alternating role rule

Roles must strictly alternate: `user → assistant → user → assistant → ...`

The first message must always be `user`. Two consecutive messages of the same role → 400 error.

```python
# ❌ Invalid — two assistant messages in a row
[
    {"role": "user",      "content": "Run the tests"},
    {"role": "assistant", "content": [ToolUseBlock(...)]},
    {"role": "assistant", "content": "Here is my analysis"},  # ← error
]

# ✅ Valid — strictly alternating
[
    {"role": "user",      "content": "Run the tests"},
    {"role": "assistant", "content": [ToolUseBlock(...)]},
    {"role": "user",      "content": [tool_result]},
    {"role": "assistant", "content": "Here is my analysis"},
]
```

This is why all tool results go into a `user` message — it's the only way to maintain the alternating pattern while sending results back.

---

## The context window problem

Messages accumulate forever, but Claude's context window doesn't. `claude-opus-4-6` has a 200K token limit. Once your `messages` list exceeds that, the API rejects the call.

In practice you can estimate the token growth per iteration:

```
Each iteration roughly adds:
  - assistant turn:  ~100–500 tokens (tool request + narration)
  - user turn:       depends entirely on tool result size

read_file on a 500-line file ≈ 2,000 tokens
run_tests on a large suite   ≈ 1,000–5,000 tokens

After 20 iterations reading large files:
20 × (300 + 3,000) = ~66,000 tokens — still fine
After 60 iterations: getting large
```

You have three options when this becomes a problem:

**Option 1: Summarise old tool results.** After N iterations, replace old `tool_result` content with a one-line summary.

**Option 2: Drop stale results.** Tool results from early in the run are often irrelevant later. You can truncate their `content` strings while keeping the `tool_use`/`tool_result` block structure (the API requires the pair to exist).

**Option 3: Enable compaction (beta).** The API automatically summarises earlier context server-side when it gets large:

```python
response = client.beta.messages.create(
    betas=["compact-2026-01-12"],
    context_management={"edits": [{"type": "compact_20260112"}]},
    ...
)

# CRITICAL: append response.content, not just text.
# Compaction blocks live in response.content and must be
# preserved for the API to use them on the next call.
messages.append({"role": "assistant", "content": response.content})
```

---

## A practical pattern: seeding history

You can pre-populate `messages` before the loop starts to give Claude context it doesn't need to discover:

```python
# Tell Claude upfront what project it's working on,
# so it doesn't waste an iteration calling list_files
messages = [
    {
        "role": "user",
        "content": "Here is the project structure:\n\nsrc/math_utils.py\ntests/test_math_utils.py"
    },
    {
        "role": "assistant",
        "content": "Got it. I can see the structure. What would you like me to do?"
    },
    {
        "role": "user",
        "content": "Run the tests and explain any failures."
    }
]
```

Claude reads this and skips `list_files` entirely — it already has the information. This is how you make agents faster and cheaper: give them what they'd discover anyway.

---

## Multi-turn conversations vs agents — same mechanism, different shape

A chat app and an agent both use `messages` the same way. The difference is who adds entries:

```
Chat app:                            Agent:
  user types → you append              loop iterates → loop appends
  Claude responds → you append         Claude responds → loop appends
  user types again → you append        tools run → loop appends
```

The list is identical in structure. The agent just has more entries, and some of those entries are tool calls rather than human text.

---

## The one-line mental model

> **`messages` is a transcript. Claude reads the whole transcript on every API call. The loop is what keeps adding to it.**

There's no hidden state, no session object, no server-side memory. If it's not in `messages`, Claude doesn't know it happened.
