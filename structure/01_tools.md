# Tools

A tool is a **contract between you and Claude**. You're saying: *"Here are capabilities you can request. If you request them, I promise to run them and give you the result."*

Claude never executes anything. It reads your tool definitions, decides which ones to call, and hands you back a structured request. You run the actual code.

---

## Anatomy of a tool definition

```python
{
    "name": "run_tests",
    "description": (
        "Run pytest on a specific file or directory and return the output. "
        "The output includes which tests passed, which failed, and the "
        "full traceback for any failures. "
        "Use this after you have a sense of the codebase structure."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "File or directory to run, e.g. 'tests/' or 'tests/test_math_utils.py'",
            },
            "verbose": {
                "type": "boolean",
                "description": "If true, show each test name as it runs (pytest -v). Default false.",
            },
        },
        "required": ["target"],
    },
}
```

Each field has a specific job:

| Field | What Claude uses it for |
|---|---|
| `name` | How Claude *calls* the tool — appears in the `tool_use` block it sends back |
| `description` | **The most important field.** Claude reads this to decide *when* and *why* to use the tool |
| `input_schema` | Tells Claude what arguments to provide — it follows this like a type contract |
| `properties[x].description` | Guides Claude on *what value* to put in each argument |
| `required` | Claude will always provide these. Anything not in `required` is optional |

---

## The description is Claude's decision engine

Claude has no hardcoded logic for when to call one tool vs another. It reasons entirely from the description.

**Vague description → Claude uses it wrong:**
```python
# Bad — Claude doesn't know when to use this vs read_file
"description": "Does pytest stuff"
```

**Good description → Claude uses it correctly:**
```python
# Good — tells Claude the WHEN, the WHY, and what to expect back
"description": (
    "Run pytest on a specific file or directory and return the output. "
    "The output includes which tests passed, which failed, and the "
    "full traceback for any failures. "
    "Use this after you have a sense of the codebase structure."  # ← ordering hint
)
```

That last sentence — *"Use this after you have a sense of the codebase structure"* — is why Claude calls `list_files` first without you hardcoding that sequence anywhere.

---

## What Claude actually sends back when it calls a tool

When Claude decides to use a tool, the API response looks like this:

```python
response.stop_reason  # → "tool_use"

response.content  # → list of blocks, e.g.:
[
    TextBlock(type="text", text="Let me run the tests first."),
    ToolUseBlock(
        type="tool_use",
        id="toolu_01XzY...",        # ← unique ID you MUST echo back
        name="run_tests",           # ← matches your tool's name field
        input={                     # ← Claude filled this from input_schema
            "target": "tests/",
            "verbose": True
        }
    )
]
```

Three things to notice:

1. **There's often a `TextBlock` too.** Claude narrates what it's about to do. You can print this or ignore it.
2. **The `id` is a one-time token.** It links the request to its result. Wrong id → 400 error.
3. **`input` is always a dict.** Even if your tool only takes one argument, it comes back as `{"target": "tests/"}`, never a bare string.

---

## What you send back — the tool result

After executing the tool, you send results back as a `user` message:

```python
{
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_01XzY...",  # must match the id above
            "content": "[pytest exit code: 1 — FAILED]\n\n1 failed, 8 passed..."
        }
    ]
}
```

`content` is always a **plain string**. This is why all tool implementations return `str` — even errors:

```python
def list_files(directory: str) -> str:
    if not os.path.isdir(abs_dir):
        return f"Error: '{directory}' is not a directory."  # string, not raise
```

You return errors as strings instead of raising exceptions because Claude needs to *read* the error and adapt. If you raise, the loop crashes. If you return `"Error: not a directory"`, Claude sees it and may try a different path.

---

## Multiple tool calls in one response

Claude can request several tools in a single response:

```python
response.content = [
    ToolUseBlock(id="toolu_001", name="list_files", input={"directory": "tests"}),
    ToolUseBlock(id="toolu_002", name="list_files", input={"directory": "src"}),
]
```

You execute both, collect both results, and send them back in **one** user message:

```python
{
    "role": "user",
    "content": [
        {"type": "tool_result", "tool_use_id": "toolu_001", "content": "tests/test_math.py"},
        {"type": "tool_result", "tool_use_id": "toolu_002", "content": "src/math_utils.py"},
    ]
}
```

The API requires that every `tool_use_id` in the request gets exactly one result. No result → error. Two results for the same id → error.

---

## The full lifecycle

```
YOUR CODE                        CLAUDE
─────────────────────────────────────────────────────
Define TOOLS list
  │
  ▼
Send to API with messages ──────► Reads tool descriptions
                                  Decides: "I need run_tests"
                                  Fills in input from input_schema
                          ◄────── Returns ToolUseBlock(id, name, input)
  │
  ▼
Your execute_tool() runs
  result = run_tests("tests/")
  │
  ▼
Send tool_result back ──────────► Reads the result string
  {"tool_use_id": id,             Reasons about what it means
   "content": result}             Decides: next tool, or final answer
                          ◄────── Returns next ToolUseBlock or end_turn
```

---

## The `input_schema` is JSON Schema

The `input_schema` follows the [JSON Schema](https://json-schema.org/) spec. Useful patterns:

```python
# Enum — Claude will only pick from these values
"status": {
    "type": "string",
    "enum": ["pass", "fail", "skip"]
}

# Array of strings
"files": {
    "type": "array",
    "items": {"type": "string"}
}

# Nested object
"options": {
    "type": "object",
    "properties": {
        "timeout": {"type": "integer"},
        "retry":   {"type": "boolean"}
    }
}

# Optional field — describe the default in the description
"verbose": {
    "type": "boolean",
    "description": "Default false."
}
```

---

## A note on tool count

Keep the tool set small and focused. Each tool you add:
- Takes up context window tokens (every schema is sent on every API call)
- Adds cognitive overhead for Claude — more choices = more reasoning needed
- Can introduce ambiguity if two tools do similar things

For a testing agent, three tools is the right number. For a coding agent you might have 5–6. Avoid going over 10 unless you're using tool search to load them dynamically.
