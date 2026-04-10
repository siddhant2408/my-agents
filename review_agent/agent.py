"""
Autonomous Code Review & Fix Agent

Given a list of changed files (as in a pull request), this agent:
  1. Reads every changed file to understand the scope
  2. Runs the linter on each file — finds style and import issues
  3. Runs the full test suite — finds logic bugs
  4. Writes minimal fixes for every issue it finds
  5. Re-runs linter and tests after each round of fixes to confirm
  6. Produces a structured final report: what it found, what it fixed,
     what (if anything) it could not resolve, and a merge verdict

The agent decides the ORDER and COMBINATION of steps autonomously.
You do not hardcode "read then lint then test then fix" — Claude reasons
that sequence from the tool descriptions and the system prompt.

Tools the agent has:
  ┌──────────────┬─────────────────────────────────────────────────────────┐
  │ list_files   │ Discover .py files in a directory                       │
  │ read_file    │ Read a file with line numbers                           │
  │ write_file   │ Write a corrected file (full content, no line numbers)  │
  │ run_linter   │ Run flake8 on a file or directory                       │
  │ run_tests    │ Run pytest on a file or directory                       │
  └──────────────┴─────────────────────────────────────────────────────────┘

Typical agent flow for a two-file PR:

  ┌─ Iteration 1 ──────────────────────────────────────────────────────────┐
  │  read_file("src/string_utils.py")   ← parallel: both files at once    │
  │  read_file("src/math_utils.py")                                        │
  └────────────────────────────────────────────────────────────────────────┘
  ┌─ Iteration 2 ──────────────────────────────────────────────────────────┐
  │  run_linter("src/string_utils.py")  ← parallel: lint both files       │
  │  run_linter("src/math_utils.py")                                       │
  └────────────────────────────────────────────────────────────────────────┘
  ┌─ Iteration 3 ──────────────────────────────────────────────────────────┐
  │  run_tests("tests/")               ← run full suite                    │
  └────────────────────────────────────────────────────────────────────────┘
  ┌─ Iteration 4 ──────────────────────────────────────────────────────────┐
  │  write_file("src/string_utils.py", ...)  ← lint fix + logic fix       │
  │  write_file("src/math_utils.py",   ...)  ← lint fix + logic fix       │
  └────────────────────────────────────────────────────────────────────────┘
  ┌─ Iteration 5 ──────────────────────────────────────────────────────────┐
  │  run_linter("src/")                ← verify all lint issues gone       │
  │  run_tests("tests/")               ← verify all tests now pass         │
  └────────────────────────────────────────────────────────────────────────┘
  ┌─ Iteration 6 (end_turn) ───────────────────────────────────────────────┐
  │  Final structured report                                               │
  └────────────────────────────────────────────────────────────────────────┘
"""

import json
import os
import subprocess
import anthropic

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# All file paths the agent works with are relative to this root.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Hard cap on agentic-loop iterations.  Each iteration is one API call.
# A simple two-file review takes ~6 iterations; 20 gives generous headroom
# for retries without risking an unbounded loop.
MAX_ITERATIONS = 20


# ---------------------------------------------------------------------------
# 1.  TOOL DEFINITIONS
#
# Five tools.  The description is the most important field: Claude reads it
# to decide WHEN to call the tool, in what ORDER, and with what arguments.
#
# Notice the division of responsibility:
#   - "always read before writing"   → enforced in write_file description
#   - "run after every write_file"   → enforced in run_linter / run_tests
#   - "parallel reads are fine"      → implied by list/read descriptions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_files",
        "description": (
            "List all Python (.py) files inside a directory. "
            "Use this to discover which source files or test files exist "
            "before reading or running them. "
            "You can call this in parallel for multiple directories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": (
                        "Relative path to the directory to scan, "
                        "e.g. 'src' or 'tests'. Use '.' for the project root."
                    ),
                }
            },
            "required": ["directory"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full contents of a file, with line numbers. "
            "Use this to understand what a file does before linting or testing, "
            "and always use it before writing a fix — never edit code you haven't read. "
            "You can call this in parallel for multiple files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "Relative path to the file, e.g. 'src/math_utils.py'.",
                }
            },
            "required": ["filepath"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Overwrite a file with new content. "
            "Use this to apply a fix — for lint issues, logic bugs, or both at once. "
            "IMPORTANT rules for this tool:\n"
            "  1. Always call read_file on the target before calling write_file.\n"
            "  2. Write the COMPLETE file content — this tool replaces the whole file.\n"
            "  3. Do NOT include line-number prefixes (e.g. '  1 | ') in the content — "
            "     write raw Python source only.\n"
            "  4. Make the minimal change needed; do not reformat unrelated code.\n"
            "  5. After every write_file call you MUST re-run run_linter and run_tests "
            "     to confirm the fix worked."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "Relative path to the file to write, e.g. 'src/math_utils.py'.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        "The complete new file content as a raw Python string. "
                        "No line-number prefixes. No markdown fences."
                    ),
                },
            },
            "required": ["filepath", "content"],
        },
    },
    {
        "name": "run_linter",
        "description": (
            "Run flake8 on a file or directory and return all style violations. "
            "Use this on every changed file before running tests, "
            "and again after every write_file call to confirm lint issues are gone. "
            "Returns 'No lint issues found.' when the target is clean."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "File or directory to lint, "
                        "e.g. 'src/math_utils.py' or 'src/'. "
                        "Use 'src/' to lint all source files at once."
                    ),
                }
            },
            "required": ["target"],
        },
    },
    {
        "name": "run_tests",
        "description": (
            "Run pytest on a file or directory and return the full output, "
            "including tracebacks for any failures. "
            "Use this after linting, and again after every write_file call "
            "to confirm fixes did not break anything. "
            "Never mark the review complete without a final clean run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "File or directory to test, "
                        "e.g. 'tests/' or 'tests/test_math_utils.py'. "
                        "Use 'tests/' to run the full suite."
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
# Each function returns a plain string — that string becomes the tool_result
# content that Claude reads on the next iteration.
#
# Error handling philosophy: return errors as strings, never raise.
# If you raise, the loop crashes. If you return "Error: ...", Claude reads
# it and can adapt (try a different path, report the issue, etc.).
# ---------------------------------------------------------------------------

def list_files(directory: str) -> str:
    """Return a newline-separated list of .py files under *directory*."""
    abs_dir = os.path.join(PROJECT_ROOT, directory)

    if not os.path.isdir(abs_dir):
        return f"Error: '{directory}' is not a directory."

    found = []
    for root, _, files in os.walk(abs_dir):
        for fname in sorted(files):
            if fname.endswith(".py"):
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, PROJECT_ROOT)
                found.append(rel)

    if not found:
        return f"No .py files found in '{directory}'."

    return "\n".join(found)


def read_file(filepath: str) -> str:
    """Return the contents of *filepath* with 1-based line numbers.

    Line numbers let Claude say "the bug is on line 15" and let write_file
    know exactly what to replace — without Claude having to count manually.
    """
    abs_path = os.path.join(PROJECT_ROOT, filepath)

    if not os.path.isfile(abs_path):
        return f"Error: file '{filepath}' not found."

    with open(abs_path) as fh:
        lines = fh.readlines()

    numbered = "".join(f"{i + 1:>4} | {line}" for i, line in enumerate(lines))
    return f"# {filepath}\n\n{numbered}"


def write_file(filepath: str, content: str) -> str:
    """Write *content* to *filepath*, replacing the file completely.

    Safety checks:
      - Path must resolve inside PROJECT_ROOT (no directory traversal).
      - Only .py files are writable (protects test files from accidental edits).
    """
    # Resolve to absolute path and check it stays inside the project
    abs_path = os.path.realpath(os.path.join(PROJECT_ROOT, filepath))
    project_root_real = os.path.realpath(PROJECT_ROOT)

    if not abs_path.startswith(project_root_real + os.sep):
        return f"Error: '{filepath}' resolves outside the project directory — write refused."

    if not filepath.endswith(".py"):
        return f"Error: only .py files may be written (got '{filepath}')."

    # Refuse to overwrite test files — tests are the source of truth
    if os.path.join("tests", "") in filepath.replace("\\", "/") + "/":
        return (
            f"Error: writing to test files is not allowed. "
            f"Fix the source code, not the tests."
        )

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    with open(abs_path, "w") as fh:
        fh.write(content)

    line_count = len(content.splitlines())
    return f"Successfully wrote {line_count} lines to '{filepath}'."


def run_linter(target: str) -> str:
    """Run flake8 on *target* (file or directory) and return violations."""
    abs_target = os.path.join(PROJECT_ROOT, target)

    if not os.path.exists(abs_target):
        return f"Error: '{target}' does not exist."

    cmd = [
        "python3", "-m", "flake8",
        abs_target,
        "--max-line-length=88",   # matches black's default — avoids noise
        "--extend-ignore=E203",   # whitespace before ':' — black-incompatible rule
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
    except FileNotFoundError:
        return "Error: flake8 is not installed. Run: pip install flake8"

    if result.returncode == 0:
        return "No lint issues found."

    # Make absolute paths relative so Claude sees 'src/foo.py:3:1' not '/Users/.../src/foo.py:3:1'
    output = result.stdout + result.stderr
    output = output.replace(abs_target, target)
    # Also handle subdirectory paths when target is a directory
    output = output.replace(PROJECT_ROOT + os.sep, "")

    return f"[flake8 exit code: {result.returncode}]\n\n{output}"


def run_tests(target: str, verbose: bool = False) -> str:
    """Run pytest on *target* and return the full output including tracebacks."""
    abs_target = os.path.join(PROJECT_ROOT, target)

    if not os.path.exists(abs_target):
        return f"Error: '{target}' does not exist."

    cmd = ["python3", "-m", "pytest", abs_target, "--tb=short"]
    if verbose:
        cmd.append("-v")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )

    output = result.stdout + result.stderr
    status = "PASSED" if result.returncode == 0 else "FAILED"
    return f"[pytest exit code: {result.returncode} — {status}]\n\n{output}"


# ---------------------------------------------------------------------------
# 3.  TOOL DISPATCHER
#
# Routes tool_name → the correct Python function.
# Keeping this separate from the loop makes it easy to add new tools:
# add a definition to TOOLS, add a function above, add a branch here.
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Dispatch a tool call from Claude to the correct implementation."""
    if tool_name == "list_files":
        return list_files(tool_input["directory"])

    elif tool_name == "read_file":
        return read_file(tool_input["filepath"])

    elif tool_name == "write_file":
        return write_file(tool_input["filepath"], tool_input["content"])

    elif tool_name == "run_linter":
        return run_linter(tool_input["target"])

    elif tool_name == "run_tests":
        return run_tests(
            tool_input["target"],
            verbose=tool_input.get("verbose", False),
        )

    else:
        return f"Error: unknown tool '{tool_name}'."


# ---------------------------------------------------------------------------
# 4.  SYSTEM PROMPT
#
# Structured into four sections that each shape a different dimension of
# Claude's behaviour:
#
#   ## Role          — WHO Claude is (affects reasoning style and tone)
#   ## Workflow      — WHAT to do and in what order (not a rigid script —
#                      Claude adapts if the user's request changes the scope)
#   ## Hard Rules    — non-negotiable constraints, stated with "Never/Always"
#                      language so Claude treats them as absolute
#   ## Report Format — exact output format Claude must follow at the end
#
# The system prompt is STABLE — it never changes between runs.
# Dynamic data (which files changed, the PR description) lives in the
# first user message so it doesn't break prompt caching.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
## Role
You are a senior software engineer performing automated code review.
Your goal: take a set of changed files, find every lint issue and test failure,
write minimal fixes, and produce a clear report that tells the team whether
the pull request is ready to merge.

## Workflow
Work through these steps in order:

1. **Read all changed files** — understand the code before doing anything else.
   You may read multiple files in parallel.

2. **Run the linter on each changed file** — find style and import issues.
   You may lint multiple files in parallel.

3. **Run the full test suite** — identify all failures.

4. **Fix issues** — for each lint issue or test failure:
   a. If you haven't read the affected source file yet, do so now.
   b. Identify the exact problem and the line it is on.
   c. Call write_file with the complete corrected file content (no line numbers).
      Fix ALL issues in a file in a single write_file call — do not write
      the same file twice if you can batch the changes.

5. **Verify fixes** — after every round of write_file calls:
   a. Re-run the linter on fixed files.
   b. Re-run the full test suite.
   This step is mandatory — never skip it.

6. **If an issue persists after two attempts**, stop retrying and mark it as
   unresolved in your report. Do not loop indefinitely.

7. **Produce the final report** (see format below).

## Hard Rules
- Never mark the review complete without a final clean run of both linter and tests.
- Never read a bug from a traceback and guess at the fix — always read the source file first.
- Never delete or modify test files. Tests are the source of truth.
- write_file replaces the entire file. Always read the file first, make the minimal
  change needed, then write the full corrected content. Never include line-number
  prefixes like '  1 | ' in the content you write.
- If you cannot fix an issue after two attempts, report it clearly rather than
  silently dropping it.

## Report Format
End your final response with exactly this structure:

---
### Review Summary
**Files reviewed:** <comma-separated list>
**Lint issues:** <N found> → <N fixed> fixed, <N> unresolved
**Tests before fixes:** <N passed, N failed>
**Tests after fixes:** <N passed, N failed>

### Fixes Applied
<For each fix: one line — filename:line — what changed and why>

### Unresolved Issues
<Any issues you could not fix, with a brief explanation. Write "None." if all clear.>

### Verdict
<One of:>
  ✅ APPROVED — all lint checks pass and all tests pass.
  ❌ CHANGES REQUIRED — <N> unresolved issue(s) remain (see above).
---
""".strip()


# ---------------------------------------------------------------------------
# 5.  THE AGENTIC LOOP
#
# The loop is identical in structure to the testing agent — the only
# differences are the tools available, the system prompt, and the seeded
# first message.
#
# Three things to watch in the loop:
#
#   (a) Iteration cap — prevents runaway loops when a fix keeps breaking
#
#   (b) Token monitoring — messages grow on every iteration because the full
#       history is re-sent on each API call.  Logging input_tokens lets you
#       see when you're approaching the 200K context limit.
#
#   (c) Seeded history — we pre-populate the first user message with the
#       list of changed files so Claude doesn't need an extra list_files
#       iteration just to know what it's reviewing.
# ---------------------------------------------------------------------------

def run_review(changed_files: list) -> str:
    """Run the code review agent on a list of changed file paths.

    Args:
        changed_files: Relative paths to the files that changed in this PR,
                       e.g. ["src/string_utils.py", "src/math_utils.py"].

    Returns:
        The agent's final report as a plain string.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Build the initial user message.
    # Listing the changed files upfront (seeded history) saves one iteration
    # that would otherwise be spent on list_files("src/") discovery.
    file_list = "\n".join(f"  - {f}" for f in changed_files)
    initial_message = (
        "The following files have changed in this pull request and need review:\n\n"
        f"{file_list}\n\n"
        "Review these files, fix every lint issue and test failure you find, "
        "then produce your report."
    )

    messages = [{"role": "user", "content": initial_message}]

    print(f"\n{'=' * 65}")
    print("CODE REVIEW AGENT")
    print(f"Files: {', '.join(changed_files)}")
    print(f"{'=' * 65}")

    iteration = 0

    while True:
        iteration += 1

        # (a) Iteration cap — bail out before burning unlimited API credits
        if iteration > MAX_ITERATIONS:
            msg = (
                f"Agent hit the {MAX_ITERATIONS}-iteration limit. "
                "The task may be too complex or a fix may be looping. "
                "Review the output above and continue manually."
            )
            print(f"\n[WARN] {msg}")
            return msg

        print(f"\n[loop #{iteration}] Sending {len(messages)} messages …")

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8096,      # fixes can be verbose — give Claude room
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # (b) Token monitoring — log usage on every iteration so you can
        #     see context growth and catch runaway history accumulation early
        print(
            f"[loop #{iteration}] stop_reason={response.stop_reason!r}  "
            f"tokens=({response.usage.input_tokens} in / "
            f"{response.usage.output_tokens} out)"
        )

        # ------------------------------------------------------------------
        # CASE A: Claude has finished — extract and return the final report
        # ------------------------------------------------------------------
        if response.stop_reason == "end_turn":
            final = next(
                (b.text for b in response.content if b.type == "text"),
                "(no text response)",
            )
            print(f"\n{'=' * 65}")
            print("AGENT REPORT")
            print(f"{'=' * 65}")
            print(final)
            return final

        # ------------------------------------------------------------------
        # CASE B: Claude wants to call tools
        #
        # Protocol (three rules that cannot be broken):
        #   1. Append the full response.content as the assistant turn FIRST,
        #      before executing anything.  The API needs to see the
        #      tool_use blocks before the tool_result blocks.
        #   2. Collect ALL tool results into ONE user message.
        #      Two consecutive user messages → 400 error.
        #   3. tool_use_id in each result must exactly match block.id.
        # ------------------------------------------------------------------
        if response.stop_reason == "tool_use":

            # Rule 1: append complete assistant content (TextBlocks +
            #         ToolUseBlocks) — never just the text portion
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                print(f"  → {block.name}({json.dumps(block.input)})")
                result = execute_tool(block.name, block.input)

                # Print a preview so progress is visible in the terminal
                preview = result[:200].replace("\n", "\\n")
                print(f"  ← {preview}{'…' if len(result) > 200 else ''}")

                # Rule 3: id must match exactly
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            # Rule 2: all results in one message
            messages.append({"role": "user", "content": tool_results})

            continue  # next iteration → Claude sees results, decides next step

        # Unexpected stop reason (max_tokens, pause_turn, etc.)
        print(f"[WARN] Unexpected stop_reason: {response.stop_reason!r} — exiting loop.")
        break

    return "(agent stopped unexpectedly)"


# ---------------------------------------------------------------------------
# 6.  ENTRY POINT
#
# Simulates reviewing a two-file PR: both files have lint issues AND logic
# bugs.  The agent discovers them, fixes them, verifies, and reports.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_review(
        changed_files=[
            "src/string_utils.py",
            "src/math_utils.py",
        ]
    )
