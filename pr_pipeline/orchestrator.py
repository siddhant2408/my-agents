"""
PR Pipeline Orchestrator — a Multi-Agent System

This orchestrator coordinates two specialist agents to decide whether a
pull request is ready to merge:

  ┌─────────────────────────────────────────────────────────────────────┐
  │                       Orchestrator Agent                            │
  │                      (this file's loop)                             │
  │                                                                     │
  │   tool: run_testing_agent          tool: run_review_agent           │
  │         │                                │                          │
  │         ▼                                ▼                          │
  │   testing_agent/agent.py        review_agent/agent.py               │
  │   (its own agentic loop)        (its own agentic loop)              │
  └─────────────────────────────────────────────────────────────────────┘

What the orchestrator does:
  1. Calls run_testing_agent  → gets baseline: did tests pass before any fixes?
  2. Calls run_review_agent   → lints files, writes fixes, reports what changed
  3. Calls run_testing_agent  → re-checks: did the review fixes break anything?
  4. Synthesises all three reports into a single merge verdict

The critical multi-agent insight:
  - The TOOL implementations here are full agents, not simple functions.
  - From the orchestrator's perspective, calling run_review_agent looks
    identical to calling read_file in a single agent — it just returns a string.
  - But under the hood, each subagent runs its OWN while-True loop, making
    its own API calls, until it reaches end_turn.
  - The orchestrator never sees any of that — it only sees the final string.

This is the composability of agents: each agent is a black box that takes
input and returns a string. Nesting them is no different from nesting functions.

Iteration budget:
  - Orchestrator loop:        MAX_ITERATIONS = 6
    (only 3 tool calls needed in the happy path; 6 gives headroom)
  - Testing agent sub-loop:   governed by testing_agent/agent.py (its own cap)
  - Review agent sub-loop:    governed by review_agent/agent.py (MAX 20)
"""

import json
import os
import sys

import anthropic

# ---------------------------------------------------------------------------
# SUBAGENT IMPORTS
#
# Each subagent lives in its own directory and has its own PROJECT_ROOT.
# We import their entry-point functions here so the orchestrator can call
# them as regular Python functions — which then become tools.
# ---------------------------------------------------------------------------

# Add the project root to the path so we can import sibling packages
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)
sys.path.insert(0, _PROJECT)

from review_agent.agent import run_review          # run_review(changed_files: list) -> str
from testing_agent.agent import run_agent as _testing_agent_run  # run_agent(task: str) -> str


MAX_ITERATIONS = 6


# ---------------------------------------------------------------------------
# 1.  TOOL DEFINITIONS
#
# Two tools — one per specialist agent.
#
# The descriptions are written from the ORCHESTRATOR's perspective:
#   - What does this specialist DO?
#   - WHEN should you call it?
#   - WHAT INPUT does it need?
#   - WHAT OUTPUT does it return?
#
# Claude reads these descriptions to decide when to delegate and to whom.
# The fact that each tool is secretly an agent is invisible to the LLM —
# it just sees a tool that takes input and returns a string.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "run_testing_agent",
        "description": (
            "Delegate test execution to the Testing Agent specialist. "
            "This agent discovers test files, runs pytest, reads source files "
            "for any failures, and returns a plain-English analysis including: "
            "pass/fail counts, traceback summaries, and suggested fixes.\n\n"
            "When to call:\n"
            "  - At the START of the pipeline, to capture the baseline state.\n"
            "  - At the END of the pipeline, to verify that review fixes did not "
            "    break any tests that were previously passing.\n\n"
            "Returns: a multi-line string report from the testing agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "A plain-English instruction for the testing agent, e.g. "
                        "'Run the full test suite in tests/ and explain any failures.' "
                        "Be explicit about whether you want source files read "
                        "for failed tests."
                    ),
                }
            },
            "required": ["task"],
        },
    },
    {
        "name": "run_review_agent",
        "description": (
            "Delegate code review and auto-fixing to the Review Agent specialist. "
            "This agent reads every changed file, runs the linter on each, runs "
            "the test suite, writes minimal fixes for lint issues and test failures, "
            "re-verifies after each fix, and returns a structured report with: "
            "files reviewed, issues found vs fixed, and a merge verdict.\n\n"
            "When to call:\n"
            "  - AFTER the baseline test run, so you know the pre-fix state.\n"
            "  - Pass the same changed_files list the user gave you.\n\n"
            "Returns: a structured review report from the review agent, ending "
            "with APPROVED or CHANGES REQUIRED."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of relative file paths that changed in this PR, "
                        "e.g. ['src/math_utils.py', 'src/string_utils.py']. "
                        "Paths are relative to the review agent's project root."
                    ),
                }
            },
            "required": ["changed_files"],
        },
    },
]


# ---------------------------------------------------------------------------
# 2.  TOOL IMPLEMENTATIONS
#
# This is where multi-agent magic happens.
#
# In a single agent, execute_tool calls simple functions:
#   read_file(filepath)  →  opens a file, returns its text
#
# Here, execute_tool calls FULL AGENTS:
#   _testing_agent_run(task)   →  runs its own while-True loop
#   run_review(changed_files)  →  runs its own while-True loop
#
# Both return a plain string. The orchestrator cannot tell the difference.
# That's the beauty of the pattern: agents compose like functions.
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch an orchestrator tool call to the correct subagent."""

    if tool_name == "run_testing_agent":
        task = tool_input["task"]

        print(f"\n{'─' * 65}")
        print(f"  SUBAGENT STARTING: Testing Agent")
        print(f"  Task: {task}")
        print(f"{'─' * 65}")

        # This kicks off the testing agent's FULL agentic loop.
        # It will make multiple API calls, run pytest, read source files,
        # and only return when it reaches end_turn.
        result = _testing_agent_run(task)

        print(f"\n{'─' * 65}")
        print(f"  SUBAGENT DONE: Testing Agent")
        print(f"{'─' * 65}")

        return result

    elif tool_name == "run_review_agent":
        changed_files = tool_input["changed_files"]

        print(f"\n{'─' * 65}")
        print(f"  SUBAGENT STARTING: Review Agent")
        print(f"  Files: {', '.join(changed_files)}")
        print(f"{'─' * 65}")

        # This kicks off the review agent's FULL agentic loop (up to 20 iterations).
        # It lints, writes fixes, re-runs tests, and returns when done.
        result = run_review(changed_files)

        print(f"\n{'─' * 65}")
        print(f"  SUBAGENT DONE: Review Agent")
        print(f"{'─' * 65}")

        return result

    else:
        return f"Error: unknown tool '{tool_name}'."


# ---------------------------------------------------------------------------
# 3.  SYSTEM PROMPT
#
# The orchestrator's system prompt is different from the specialists':
#
#   Specialist system prompts  →  define HOW to do a specific job
#                                 (step-by-step workflows, hard rules)
#
#   Orchestrator system prompt →  defines WHEN to delegate and to WHOM,
#                                 and HOW to synthesise multiple reports
#
# The orchestrator should NOT tell Claude how the specialists work internally.
# It only needs to know: what does each specialist produce, and in what
# order should they be called.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
## Role
You are a PR Pipeline Orchestrator. Your job is to coordinate two specialist
agents — a Testing Agent and a Review Agent — and produce a single, unified
merge verdict for a pull request.

You do not run tests or lint files yourself. You delegate everything to the
appropriate specialist and synthesise their reports.

## Pipeline (always follow this order)

**Step 1 — Baseline test run**
Call run_testing_agent with a task to run the full test suite and explain any
failures. This captures the state BEFORE any fixes are applied. Note how many
tests pass and fail.

**Step 2 — Code review and auto-fix**
Call run_review_agent with the list of changed files. The review agent will lint
the files, fix issues, re-run tests, and report what it did. Note the verdict
(APPROVED / CHANGES REQUIRED) and any unresolved issues.

**Step 3 — Post-fix test run**
Call run_testing_agent again to verify the current state of the tests AFTER
the review agent's fixes. Check whether:
  - Tests that were failing before are now passing.
  - Tests that were passing before are still passing (no regressions).

**Step 4 — Unified report**
Produce your final report (see format below).

## Hard Rules
- Never skip Step 3. The review agent changes files — you must verify the
  test suite is still healthy after those changes.
- Never fabricate test results or review outcomes. Use only what the agents
  actually reported.
- If a subagent reports an error (e.g. "agent hit iteration limit"), include
  that in your report under Blockers rather than ignoring it.

## Final Report Format
End your response with exactly this structure:

---
### PR Pipeline Report

**Changed files:** <comma-separated list>

#### Baseline (before review)
- Tests: <N passed, N failed>
- Key failures: <brief list, or "None">

#### Review Agent Actions
- Lint issues: <N found> → <N fixed>
- Files modified: <list, or "None">
- Unresolved issues: <list, or "None">

#### Post-Fix Verification
- Tests: <N passed, N failed>
- Regressions introduced: <Yes — list them / No>
- Failures resolved by review: <Yes — list them / No>

#### Final Verdict
<One of:>
  ✅ MERGE APPROVED — all lint checks pass, all tests pass, no regressions.
  ⚠️  MERGE WITH CAUTION — <reason: e.g. pre-existing failures not caused by this PR>
  ❌ DO NOT MERGE — <reason: unresolved issues or regressions>
---
""".strip()


# ---------------------------------------------------------------------------
# 4.  THE ORCHESTRATOR LOOP
#
# Structurally identical to any other agentic loop. The ONLY difference is
# that some tool calls in execute_tool() spin up sub-loops.
#
# From the loop's perspective, a tool call that takes 0.1s (read_file) and
# one that takes 60s (run_review_agent with 20 iterations) are identical:
# send tool_use block, wait, get back a string.
#
# This is why agents compose so cleanly — the loop does not need to know
# whether it's calling a function or an agent.
# ---------------------------------------------------------------------------

def run_pipeline(changed_files: list) -> str:
    """
    Run the full PR pipeline for a list of changed files.

    Args:
        changed_files: File paths that changed in this PR, relative to
                       each subagent's project root.

    Returns:
        The orchestrator's unified merge verdict as a string.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    file_list = "\n".join(f"  - {f}" for f in changed_files)
    initial_message = (
        "A pull request has been submitted with the following changed files:\n\n"
        f"{file_list}\n\n"
        "Run the full PR pipeline: baseline tests, code review, post-fix "
        "verification, then produce your unified merge verdict."
    )

    messages = [{"role": "user", "content": initial_message}]

    print(f"\n{'═' * 65}")
    print("PR PIPELINE ORCHESTRATOR")
    print(f"Changed files: {', '.join(changed_files)}")
    print(f"{'═' * 65}")

    iteration = 0

    while True:
        iteration += 1

        if iteration > MAX_ITERATIONS:
            msg = (
                f"Orchestrator hit the {MAX_ITERATIONS}-iteration limit. "
                "Check subagent output above for partial results."
            )
            print(f"\n[WARN] {msg}")
            return msg

        print(f"\n[orchestrator loop #{iteration}] Sending {len(messages)} messages …")

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        print(
            f"[orchestrator loop #{iteration}] stop_reason={response.stop_reason!r}  "
            f"tokens=({response.usage.input_tokens} in / "
            f"{response.usage.output_tokens} out)"
        )

        # CASE A: Orchestrator is done — extract the unified report
        if response.stop_reason == "end_turn":
            final = next(
                (b.text for b in response.content if b.type == "text"),
                "(no text response)",
            )
            print(f"\n{'═' * 65}")
            print("ORCHESTRATOR FINAL REPORT")
            print(f"{'═' * 65}")
            print(final)
            return final

        # CASE B: Orchestrator wants to call a subagent
        #
        # Note: when the orchestrator calls run_review_agent here, Python
        # blocks on that call until the review agent's entire loop finishes.
        # The orchestrator's loop is paused during that time. This is
        # synchronous multi-agent — simpler to reason about than async,
        # and fine for pipelines where steps depend on each other.
        if response.stop_reason == "tool_use":

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                print(f"\n[orchestrator] Delegating to → {block.name}")
                print(f"  Input: {json.dumps(block.input)[:200]}")

                # This is the key line. execute_tool may call a full subagent loop.
                result = execute_tool(block.name, block.input)

                preview = result[:300].replace("\n", "\\n")
                print(f"\n[orchestrator] Got back from {block.name}:")
                print(f"  {preview}{'…' if len(result) > 300 else ''}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        print(f"[WARN] Unexpected stop_reason: {response.stop_reason!r} — exiting.")
        break

    return "(orchestrator stopped unexpectedly)"


# ---------------------------------------------------------------------------
# 5.  ENTRY POINT
#
# Same files the review_agent already knows about — it has intentional bugs
# in src/math_utils.py and src/string_utils.py that the review agent will
# find and fix. The testing_agent works against testing_agent/tests/.
#
# Note: the two subagents have separate PROJECT_ROOTs and separate src/
# directories. In a real setup they'd point at the same repo. Here, each
# agent reviews its own copy of the buggy files so both can run independently.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_pipeline(
        changed_files=[
            "src/math_utils.py",
            "src/string_utils.py",
        ]
    )
