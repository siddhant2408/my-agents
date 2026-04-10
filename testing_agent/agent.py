"""
A Testing Agent built from scratch on the Anthropic API.

This agent acts like a QA engineer:
  1. Discovers test files in a directory
  2. Reads source files and test files to understand what's being tested
  3. Runs the tests and captures output
  4. Analyzes failures and explains what went wrong (and why)

The agentic loop lets Claude decide the ORDER and COMBINATION of steps.
For example, if tests fail, Claude will read the source file to understand
the bug — you didn't have to hardcode that logic.

Tools the agent has:
  ┌─────────────┬────────────────────────────────────────────────────────┐
  │ list_files  │ Discover .py files in a directory                      │
  │ read_file   │ Read the contents of any file                          │
  │ run_tests   │ Run pytest on a file or directory, capture results      │
  └─────────────┴────────────────────────────────────────────────────────┘

Flow for "run all tests and explain any failures":

  list_files("tests/")
        │
        ▼
  read_file("tests/test_math.py")   ← Claude decides to read before running
        │
        ▼
  run_tests("tests/")
        │
        ├── all pass? → summarise
        │
        └── failures? → read_file("src/math_utils.py")
                              │
                              ▼
                         explain the bug in plain English
"""

import json
import os
import subprocess
import anthropic

# ---------------------------------------------------------------------------
# PROJECT ROOT — all file paths are relative to this
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 1.  TOOL DEFINITIONS
#
# Three tools is all a testing agent needs. Notice how the descriptions
# explain *why* you'd use each one — Claude uses descriptions to reason
# about which tool to call and when.
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_files",
        "description": (
            "List all Python (.py) files inside a directory. "
            "Use this first to discover what test files or source files exist "
            "before you try to read or run them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": (
                        "Relative path to the directory to scan, "
                        "e.g. 'tests' or 'src'. Use '.' for the project root."
                    ),
                }
            },
            "required": ["directory"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full contents of a file. "
            "Use this to inspect source code or a test file before running tests, "
            "or to understand a failure after running tests."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "Relative path to the file, e.g. 'src/math_utils.py'",
                }
            },
            "required": ["filepath"],
        },
    },
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
                    "description": (
                        "File or directory to run, e.g. 'tests/' or "
                        "'tests/test_math_utils.py'. Use 'tests/' to run everything."
                    ),
                },
                "verbose": {
                    "type": "boolean",
                    "description": "If true, show each test name as it runs (pytest -v). Default false.",
                },
            },
            "required": ["target"],
        },
    },
]


# ---------------------------------------------------------------------------
# 2.  TOOL IMPLEMENTATIONS
#
# These are the actual functions that execute when Claude calls a tool.
# Each returns a plain string — that string goes back to Claude as the
# tool result.
# ---------------------------------------------------------------------------

def list_files(directory: str) -> str:
    """Return a newline-separated list of .py files under `directory`."""
    abs_dir = os.path.join(PROJECT_ROOT, directory)

    if not os.path.isdir(abs_dir):
        return f"Error: '{directory}' is not a directory."

    found = []
    for root, _, files in os.walk(abs_dir):
        for fname in sorted(files):
            if fname.endswith(".py"):
                # Return paths relative to PROJECT_ROOT for readability
                full = os.path.join(root, fname)
                rel  = os.path.relpath(full, PROJECT_ROOT)
                found.append(rel)

    if not found:
        return f"No .py files found in '{directory}'."

    return "\n".join(found)


def read_file(filepath: str) -> str:
    """Return the contents of a file, with line numbers for easy reference."""
    abs_path = os.path.join(PROJECT_ROOT, filepath)

    if not os.path.isfile(abs_path):
        return f"Error: file '{filepath}' not found."

    with open(abs_path, "r") as f:
        lines = f.readlines()

    # Add line numbers so Claude can say "line 12 has a bug"
    numbered = "".join(f"{i+1:>4} | {line}" for i, line in enumerate(lines))
    return f"# {filepath}\n\n{numbered}"


def run_tests(target: str, verbose: bool = False) -> str:
    """
    Run pytest and return its output.

    We capture both stdout and stderr because pytest writes different
    things to each stream. returncode tells us pass/fail at a glance.
    """
    abs_target = os.path.join(PROJECT_ROOT, target)

    cmd = ["python3", "-m", "pytest", abs_target, "--tb=short"]
    if verbose:
        cmd.append("-v")

    result = subprocess.run(
        cmd,
        capture_output=True,   # capture both stdout and stderr
        text=True,              # decode bytes → str automatically
        cwd=PROJECT_ROOT,
    )

    output = result.stdout + result.stderr

    # Prepend a clear pass/fail banner so Claude doesn't have to parse
    # the return code itself (though it could read it from the text too)
    status = "PASSED" if result.returncode == 0 else "FAILED"
    return f"[pytest exit code: {result.returncode} — {status}]\n\n{output}"


# ---------------------------------------------------------------------------
# 3.  TOOL DISPATCHER
#
# A single function that routes tool_name → the right Python function.
# Keeping this separate from the loop keeps the code clean.
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Call the right function and return its string result."""
    if tool_name == "list_files":
        return list_files(tool_input["directory"])

    elif tool_name == "read_file":
        return read_file(tool_input["filepath"])

    elif tool_name == "run_tests":
        return run_tests(
            tool_input["target"],
            verbose=tool_input.get("verbose", False),
        )

    else:
        return f"Unknown tool: {tool_name!r}"


# ---------------------------------------------------------------------------
# 4.  THE AGENTIC LOOP
#
# Identical in structure to any other agent — the magic is that Claude
# decides which tools to call and in what order based on the results it
# gets back. We never hardcode "first list, then read, then run".
# Claude figures that out from the tool descriptions and the user's goal.
# ---------------------------------------------------------------------------

def run_agent(task: str) -> str:
    """
    Run the testing agent on a task description.

    The system prompt shapes Claude's *persona* — it tells Claude it's a
    QA engineer, so it brings that lens to every decision it makes.
    """
    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY")
    )

    # -----------------------------------------------------------------------
    # SYSTEM PROMPT
    #
    # Think of this as the agent's job description. It runs once at the
    # start of every conversation. Good system prompts:
    #   - Define the role/persona
    #   - Set expectations for output format
    #   - Give constraints (e.g. "always read the source before explaining")
    # -----------------------------------------------------------------------
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

    # The conversation starts with just the user's task.
    messages = [{"role": "user", "content": task}]

    print(f"\n{'='*65}")
    print(f"TASK: {task}")
    print(f"{'='*65}")

    iteration = 0

    # -----------------------------------------------------------------------
    # THE LOOP
    # Same pattern as before — but now Claude is autonomous:
    # it decides what to do based on tool results, not a hardcoded script.
    # -----------------------------------------------------------------------
    while True:
        iteration += 1
        print(f"\n[loop #{iteration}] Sending {len(messages)} messages to Claude...")

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,        # more tokens — analysis responses can be long
            system=system_prompt,   # persona/instructions (separate from messages)
            tools=TOOLS,
            messages=messages,
        )

        print(f"[loop #{iteration}] stop_reason = {response.stop_reason!r}")

        # -------------------------------------------------------------------
        # DONE: extract and return Claude's final analysis
        # -------------------------------------------------------------------
        if response.stop_reason == "end_turn":
            final = next(
                (b.text for b in response.content if b.type == "text"),
                "(no response)"
            )
            print(f"\n{'='*65}")
            print("AGENT REPORT:")
            print(f"{'='*65}")
            print(final)
            return final

        # -------------------------------------------------------------------
        # TOOL USE: execute each requested tool, collect results, loop back
        #
        # Key protocol rules:
        #   1. Append Claude's FULL response (not just text) as "assistant"
        #   2. All tool results go in ONE "user" message (not separate ones)
        #   3. tool_use_id in the result must match the id in the request
        # -------------------------------------------------------------------
        if response.stop_reason == "tool_use":

            # Rule 1: append the full assistant response to history
            messages.append({
                "role": "assistant",
                "content": response.content,   # list of TextBlock + ToolUseBlock
            })

            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                print(f"  → tool: {block.name}({json.dumps(block.input)})")

                result = execute_tool(block.name, block.input)

                # Print a preview (first 200 chars) so we can follow along
                preview = result[:200].replace("\n", "\\n")
                print(f"  ← result: {preview}{'...' if len(result) > 200 else ''}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,   # Rule 3: must match block.id
                    "content": result,
                })

            # Rule 2: all results in one user message
            messages.append({
                "role": "user",
                "content": tool_results,
            })

            continue   # back to top → send updated history to Claude

        # Unexpected stop reason
        print(f"Unexpected stop_reason: {response.stop_reason}")
        break

    return "(agent stopped unexpectedly)"


# ---------------------------------------------------------------------------
# 5.  ENTRY POINTS
#
# Two different tasks to show how the same agent adapts its behaviour
# based on the goal — without any code changes.
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    # Task A: full audit — Claude will discover, read, run, and analyse
    run_agent(
        "Run the full test suite in the 'tests/' directory. "
        "For any failures, read the relevant source file and tell me "
        "exactly what the bug is and how to fix it."
    )

    print("\n\n")

    # Task B: targeted — just run one file, no source reading needed
    run_agent(
        "Quickly run tests/test_math_utils.py and give me a pass/fail summary. "
        "Don't read any source files."
    )
