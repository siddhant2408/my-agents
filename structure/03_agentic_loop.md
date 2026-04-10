# The Agentic Loop

The agentic loop is the `while True` in the agent. It's the mechanism that turns a single API call into an autonomous agent that can take multiple steps, use tools, observe results, and keep going until it's done.

Without the loop, you have a chatbot. With it, you have an agent.

---

## The fundamental problem it solves

A single API call looks like this:

```
You → Claude → Answer
```

But some tasks can't be answered in one shot. Claude needs to *do something*, see the result, then continue:

```
You → Claude → "I need to run tests first"
           → You run tests → Result back to Claude
           → Claude → "Tests failed, let me read the source"
           → You read file → Result back to Claude
           → Claude → "Found the bug, here's the fix"
```

Each `→` is a separate API call. The loop is what connects them.

---

## The loop in full, annotated

```python
while True:
    iteration += 1

    # ─── STEP 1: Ask Claude what to do next ───────────────────────────────
    # Every iteration sends the FULL conversation history.
    # Claude has no memory between calls — the messages list IS its memory.
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=system_prompt,
        tools=TOOLS,
        messages=messages,       # grows on every iteration
    )

    # ─── STEP 2: Read the exit condition ──────────────────────────────────
    # stop_reason tells you WHY Claude stopped generating.
    # There are only two cases you need to handle in a basic agent.

    # CASE A: Claude is done. No more tools needed.
    if response.stop_reason == "end_turn":
        final = next(b.text for b in response.content if b.type == "text")
        return final            # ← exit the loop

    # CASE B: Claude wants to use tools. Keep going.
    if response.stop_reason == "tool_use":

        # Append Claude's response to history BEFORE executing tools.
        # This is mandatory — the API needs to see what Claude "said"
        # before it sees the tool results.
        messages.append({
            "role": "assistant",
            "content": response.content,
        })

        # Execute every tool Claude requested this iteration.
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = execute_tool(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        # Send all results back as a single user message.
        messages.append({
            "role": "user",
            "content": tool_results,
        })

        continue    # ← back to top, Claude sees the results
```

---

## The three rules you cannot break

**Rule 1: Always append the full `response.content`, not just the text.**

```python
# ❌ Wrong — loses the ToolUseBlock, API will reject the next call
messages.append({
    "role": "assistant",
    "content": response.content[0].text
})

# ✅ Right — preserves the full list of blocks
messages.append({
    "role": "assistant",
    "content": response.content
})
```

The API validates that every `tool_use` block in the assistant turn has a matching `tool_result` in the next user turn. Strip the `ToolUseBlock` out and that contract breaks.

---

**Rule 2: All tool results go in one user message.**

```python
# ❌ Wrong — two separate user messages for two tools
messages.append({"role": "user", "content": [result_1]})
messages.append({"role": "user", "content": [result_2]})

# ✅ Right — both results in one message
messages.append({
    "role": "user",
    "content": [result_1, result_2]
})
```

The API requires alternating `user`/`assistant` turns. Two consecutive `user` messages is invalid.

---

**Rule 3: `tool_use_id` must exactly match `block.id`.**

```python
# Claude's request came with this id:
block.id  # → "toolu_01XzYabc..."

# Your result must reference the same id:
{
    "type": "tool_result",
    "tool_use_id": block.id,   # ← copy it directly, don't construct it
    "content": result
}
```

The id is how the API stitches together requests and responses in the conversation history. Wrong id → 400 error.

---

## `stop_reason` — every value you'll encounter

`stop_reason` is the loop's decision gate:

```python
# ── end_turn ──────────────────────────────────────────────────────────────
# Claude finished naturally. Extract the text and exit the loop.
if response.stop_reason == "end_turn":
    return next(b.text for b in response.content if b.type == "text")

# ── tool_use ──────────────────────────────────────────────────────────────
# Claude wants to call tools. Execute them and loop back.
if response.stop_reason == "tool_use":
    ...

# ── max_tokens ────────────────────────────────────────────────────────────
# Response was cut off because it hit max_tokens.
# The output is incomplete — don't use it as a final answer.
# Fix: increase max_tokens, or use streaming for very long outputs.
if response.stop_reason == "max_tokens":
    raise RuntimeError("Response truncated — increase max_tokens")

# ── pause_turn ────────────────────────────────────────────────────────────
# Only happens with server-side tools (web search, code execution).
# The server-side loop hit its iteration limit and paused.
# Fix: re-send the same messages — the server resumes automatically.
if response.stop_reason == "pause_turn":
    messages.append({"role": "assistant", "content": response.content})
    continue   # re-send, no new user message needed
```

For a tool-use agent like this one, you'll only ever see `end_turn` and `tool_use` in practice. But `max_tokens` will bite you eventually — always set it higher than you think you need.

---

## Parallel vs sequential tool calls

Claude decides how many tools to call per iteration. You don't control this directly.

**Sequential** — Claude calls one tool, reads the result, then decides the next:
```
Iteration 1: list_files("tests/")
Iteration 2: run_tests("tests/")       ← needed list_files result first
Iteration 3: read_file("src/math.py")  ← needed run_tests result first
```

**Parallel** — Claude calls multiple tools in one shot when results are independent:
```
Iteration 1: list_files("tests/"), list_files("src/")   ← independent
Iteration 2: run_tests("tests/")
```

Claude parallelises automatically when it determines results don't depend on each other. You get efficient batching without writing scheduling logic.

---

## How the loop terminates — and how it can go wrong

Normal termination: Claude decides it has enough information and returns `end_turn`.

**Infinite loop** — Claude keeps calling tools that return errors, or retries the same thing repeatedly:

```python
MAX_ITERATIONS = 10

while True:
    if iteration > MAX_ITERATIONS:
        return "Agent hit iteration limit — task may be too complex."
    ...
```

**Token budget exhaustion** — `messages` grows on every iteration. Monitor usage:

```python
print(f"tokens: {response.usage.input_tokens} in, {response.usage.output_tokens} out")
```

**Tool returning an error string** — Claude will usually adapt, but if the same tool keeps failing, the loop spins. Detect repeated failures in your dispatcher and return a clear "give up" message after N retries.

---

## The loop is just a conversation

Strip away the code and this is what's actually happening:

```
You:    "Run the tests and explain failures."
Claude: "Let me list the files first."     [calls list_files]
You:    "Here's what's there: tests/test_math_utils.py"
Claude: "Running them now."                [calls run_tests]
You:    "1 failed: test_divide_by_zero"
Claude: "Let me see the source."           [calls read_file]
You:    "Here's math_utils.py: line 15 has no zero guard"
Claude: "Found it. The bug is on line 15. Here's the fix."
```

The loop is automating that conversation. The `messages` list is the transcript. Each iteration is one exchange. `stop_reason == "end_turn"` is Claude saying *"I'm done, here's my answer."*

Everything else — tool definitions, system prompt, the dispatcher — exists to make that conversation productive.
