# my-agents — Project Context

## What this project is

A learning project for building AI agents from scratch using the Anthropic API
(`anthropic` Python SDK). Each agent is a standalone Python file with its own
agentic loop. The goal is to understand every layer — tools, system prompts,
the agentic loop, message history — by building real agents, not reading theory.

The `structure/` directory contains annotated reference docs for each concept.
The agent directories each contain a working implementation of that concept.

---

## Directory map

```
my-agents/
├── structure/                  # Concept reference docs (read these to understand the "why")
│   ├── 01_tools.md             # What tools are, how to define them, tool dispatcher pattern
│   ├── 02_system_prompt.md     # How to write system prompts, role/workflow/rules structure
│   ├── 03_agentic_loop.md      # The while-True loop, stop_reason handling, the 3 loop rules
│   └── 04_message_history.md  # How messages accumulate, context window growth, seeding
│
├── testing_agent/              # Single agent: QA engineer persona
│   ├── agent.py                # Entry point: run_agent(task: str) -> str
│   ├── src/math_utils.py       # Sample source with a deliberate bug (divide() no zero guard)
│   └── tests/test_math_utils.py
│
├── review_agent/               # Single agent: code review + auto-fix persona
│   ├── agent.py                # Entry point: run_review(changed_files: list) -> str
│   ├── src/
│   │   ├── math_utils.py       # Sample source with intentional lint + logic bugs
│   │   └── string_utils.py     # Sample source with intentional lint + logic bugs
│   └── tests/
│       ├── test_math_utils.py
│       └── test_string_utils.py
│
├── web_search_agent/           # Single agent: web research persona
│   └── agent.py                # Entry point: research(question: str) -> str
│                               # Uses DuckDuckGo (no API key), stdlib urllib only
│
└── pr_pipeline/                # Multi-agent: orchestrator + two specialist subagents
    └── orchestrator.py         # Entry point: run_pipeline(changed_files: list) -> str
```

---

## How each agent is structured (single agent pattern)

Every `agent.py` follows the same five-section layout:

```
1. TOOL DEFINITIONS   — list of dicts, each with name / description / input_schema
2. TOOL IMPLEMENTATIONS — Python functions that execute when Claude calls a tool
3. TOOL DISPATCHER    — execute_tool(name, input) routes to the right function
4. SYSTEM PROMPT      — Role / Workflow / Hard Rules / Report Format sections
5. AGENTIC LOOP       — while True: API call → end_turn or tool_use → repeat
```

The loop has three rules that cannot be broken:
- Append the full `response.content` (not just text) as the assistant turn
- All tool results go in ONE user message (two consecutive user messages = 400 error)
- `tool_use_id` in each result must exactly match `block.id`

---

## How the multi-agent setup works (pr_pipeline)

```
run_pipeline(changed_files)
      │
      │  Orchestrator loop (MAX 6 iterations)
      ├── tool: run_testing_agent  →  testing_agent/agent.py:run_agent()
      │                               (runs its own full agentic loop)
      ├── tool: run_review_agent   →  review_agent/agent.py:run_review()
      │                               (runs its own full agentic loop, MAX 20 iters)
      └── tool: run_testing_agent  →  called again post-fix to verify no regressions
```

The orchestrator's `execute_tool()` calls full agent functions instead of simple
utility functions. Each subagent runs its own while-True loop and returns a string.
The orchestrator sees it as a plain tool result — identical to `read_file()` returning text.

**Subagent imports in orchestrator.py:**
```python
from review_agent.agent import run_review
from testing_agent.agent import run_agent as _testing_agent_run
```
These work because `orchestrator.py` adds the project root (`my-agents/`) to `sys.path`.
Each subagent uses its own `PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))`,
so `testing_agent` always works in `testing_agent/` and `review_agent` in `review_agent/`.

---

## Running agents

```bash
# Single agents — run from their own directory or from project root
python testing_agent/agent.py
python review_agent/agent.py
python web_search_agent/agent.py

# Multi-agent pipeline
python pr_pipeline/orchestrator.py

# Web search agent also accepts a CLI argument
python web_search_agent/agent.py "What are the new features in Python 3.13?"
```

All agents read `ANTHROPIC_API_KEY` from the environment. No other env vars needed
except `WEB_SEARCH_BASE_URL` (optional override for the DuckDuckGo endpoint).

---

## Dependencies

```bash
pip install anthropic          # required by all agents
pip install flake8             # required by review_agent (linter)
# pytest, urllib — stdlib, no install needed
```

No `requirements.txt` or `pyproject.toml` yet — this is a learning project, not a package.

---

## Key design decisions (don't change these without good reason)

- **Error handling in tools returns strings, never raises.** If a tool raises,
  the loop crashes. If it returns `"Error: ..."`, Claude reads it and adapts.

- **`write_file` in review_agent refuses to write test files.** Tests are the
  source of truth — the agent fixes source code, not tests.

- **`PROJECT_ROOT` is set per-agent, not globally.** Each agent directory is
  self-contained. The orchestrator imports subagents as functions; their internal
  paths still resolve correctly because `__file__` is evaluated at import time.

- **System prompts are stable; dynamic data goes in the first user message.**
  This keeps the system prompt cacheable and separates concerns (persona vs task).

- **`MAX_ITERATIONS` caps every loop.** testing_agent has no explicit cap in code
  (relies on Claude's natural end_turn), review_agent caps at 20, orchestrator at 6.

---

## What's been built so far

| Agent | Pattern | Persona | Key tools |
|---|---|---|---|
| `testing_agent` | Single agent | QA engineer | list_files, read_file, run_tests |
| `review_agent` | Single agent | Senior code reviewer | list_files, read_file, write_file, run_linter, run_tests |
| `web_search_agent` | Single agent | Research assistant | web_search (DuckDuckGo), web_fetch |
| `pr_pipeline` | Multi-agent orchestrator | Pipeline coordinator | run_testing_agent, run_review_agent |
