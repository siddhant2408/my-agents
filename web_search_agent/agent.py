"""
Web Search Agent

Given a research question or task, this agent:
  1. Breaks the question into targeted search queries
  2. Searches DuckDuckGo (no API key required) to find relevant pages
  3. Fetches and reads the most promising pages
  4. Synthesizes findings into a clear, sourced answer

Inspired by the web search tool in https://github.com/ultraworkers/claw-code
(Rust implementation translated to Python).

Tools the agent has:
  ┌────────────────┬──────────────────────────────────────────────────────────┐
  │ web_search     │ Search the web via DuckDuckGo (no API key needed)        │
  │ web_fetch      │ Fetch and read the text content of a URL                 │
  └────────────────┴──────────────────────────────────────────────────────────┘

Typical agent flow for "What is the latest version of Python?":

  ┌─ Iteration 1 ──────────────────────────────────────────────────────────────┐
  │  web_search("latest Python version release 2024")                         │
  └────────────────────────────────────────────────────────────────────────────┘
  ┌─ Iteration 2 ──────────────────────────────────────────────────────────────┐
  │  web_fetch("https://www.python.org/downloads/")   ← most relevant result  │
  │  web_fetch("https://docs.python.org/...")         ← parallel: 2nd result  │
  └────────────────────────────────────────────────────────────────────────────┘
  ┌─ Iteration 3 (end_turn) ───────────────────────────────────────────────────┐
  │  Final synthesized answer with sources                                     │
  └────────────────────────────────────────────────────────────────────────────┘
"""

import html.parser
import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from time import time
import anthropic


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# Hard cap on agentic-loop iterations.
MAX_ITERATIONS = 20

# HTTP timeout in seconds — matches claw-code's 20-second timeout
HTTP_TIMEOUT = 20

# Max results returned per search — matches claw-code's truncate(8)
MAX_SEARCH_RESULTS = 8

# Max characters of page text returned per web_fetch
MAX_FETCH_CHARS = 8_000

# User-agent — mirrors claw-code's agent string pattern
USER_AGENT = "web-search-agent/0.1 (research bot)"

# DuckDuckGo HTML endpoint — no API key needed, same as claw-code
DDGO_SEARCH_URL = os.environ.get(
    "WEB_SEARCH_BASE_URL",
    "https://html.duckduckgo.com/html/"
)


# ---------------------------------------------------------------------------
# 1.  TOOL DEFINITIONS
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for information using DuckDuckGo. "
            "Returns up to 8 results — title, URL, and a short snippet. "
            "Use this when you need to find recent information, specific facts, "
            "or discover which pages to read in detail. "
            "You can call this multiple times with different queries to gather "
            "information from different angles. "
            "Prefer specific, targeted queries over broad ones."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The search query, e.g. 'Python 3.13 release date' or "
                        "'best practices for REST API design 2024'."
                    ),
                },
                "allowed_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional. If set, only return results from these domains "
                        "(e.g. ['docs.python.org', 'realpython.com']). "
                        "Leave unset to search all domains."
                    ),
                },
                "blocked_domains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional. Exclude results from these domains "
                        "(e.g. ['pinterest.com', 'quora.com'])."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch and read the text content of a web page. "
            "Use this to read the full content of a specific URL found via web_search. "
            "HTML is converted to plain text automatically. "
            "Content is capped at 8,000 characters — for long pages, "
            "the most relevant section is returned. "
            "You can fetch multiple URLs in parallel to save iterations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to fetch, e.g. 'https://docs.python.org/3/whatsnew/3.13.html'.",
                },
                "focus": {
                    "type": "string",
                    "description": (
                        "Optional. A hint about what you're looking for on this page "
                        "(e.g. 'release date', 'installation steps'). "
                        "Used to surface the most relevant section of long pages."
                    ),
                },
            },
            "required": ["url"],
        },
    },
]


# ---------------------------------------------------------------------------
# 2.  HTML → TEXT CONVERTER
#     Plain stdlib html.parser — no third-party deps needed.
#     Strips tags, decodes entities, collapses whitespace.
# ---------------------------------------------------------------------------

class _TextExtractor(html.parser.HTMLParser):
    """Minimal HTML-to-text converter using stdlib only."""

    # Tags whose content we skip entirely (scripts, styles, nav clutter)
    _SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "svg"}

    # Block-level tags that introduce line breaks
    _BLOCK_TAGS = {
        "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "td", "th", "blockquote", "pre", "article",
        "section", "main",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of whitespace / blank lines
        lines = [line.strip() for line in raw.splitlines()]
        non_empty = [l for l in lines if l]
        return "\n".join(non_empty)


def html_to_text(html_content: str) -> str:
    """Convert HTML to clean plain text using stdlib only."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html_content)
        return extractor.get_text()
    except Exception:
        # Fallback: strip all tags with regex if the parser chokes
        text = re.sub(r"<[^>]+>", " ", html_content)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


# ---------------------------------------------------------------------------
# 3.  HTTP HELPERS
#     Ported from claw-code's Rust build_http_client / normalize_fetch_url.
#     Uses stdlib urllib so no pip install needed.
# ---------------------------------------------------------------------------

def _make_request(url: str, timeout: int = HTTP_TIMEOUT) -> tuple[int, str, str]:
    """
    GET *url* and return (status_code, content_type, body_text).
    Follows redirects automatically (urllib does this by default).
    Upgrades http → https unless the host is localhost.
    """
    # Upgrade to HTTPS (mirrors claw-code's normalize_fetch_url)
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        url = url.replace("http://", "https://", 1)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read(2 * 1024 * 1024)  # cap at 2 MB
            content_type = resp.headers.get_content_type() or ""
            charset = resp.headers.get_content_charset("utf-8") or "utf-8"
            try:
                body = raw_bytes.decode(charset, errors="replace")
            except LookupError:
                body = raw_bytes.decode("utf-8", errors="replace")
            return resp.status, content_type, body
    except urllib.error.HTTPError as e:
        return e.code, "", f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return 0, "", f"URL error: {e.reason}"
    except Exception as e:
        return 0, "", f"Error: {e}"


# ---------------------------------------------------------------------------
# 4.  WEB SEARCH IMPLEMENTATION
#     Ported from claw-code's execute_web_search + extract_search_hits.
#     Hits DuckDuckGo's HTML endpoint — no API key needed.
# ---------------------------------------------------------------------------

def _decode_ddgo_url(redirect_url: str) -> str:
    """
    Decode a DuckDuckGo redirect URL to the actual target URL.

    DuckDuckGo uses several URL forms:
      - Direct:   https://example.com/page          → returned as-is
      - Protocol-relative: //duckduckgo.com/l/?uddg=https%3A...
      - Path-only: /l/?uddg=https%3A...
      - All of the above with HTML-entity-encoded ampersands (&amp;)

    Ported from claw-code's decode_duckduckgo_redirect (Rust → Python).
    Fixes vs original implementation:
      1. Handles /l/ path detection (not just uddg= substring check)
      2. Handles path-only URLs (prepends https://duckduckgo.com)
      3. Handles single-quoted href attributes (via html.unescape caller)
    """
    # Already a direct URL — decode any HTML entities and return
    if redirect_url.startswith("http://") or redirect_url.startswith("https://"):
        return html.unescape(redirect_url)

    # Protocol-relative → make absolute so urlparse works
    if redirect_url.startswith("//"):
        joined = "https:" + redirect_url
    elif redirect_url.startswith("/"):
        # Path-only URL (e.g. /l/?uddg=...) — prepend DDG origin
        joined = "https://duckduckgo.com" + redirect_url
    else:
        return redirect_url  # unrecognised form, return as-is

    try:
        parsed = urllib.parse.urlparse(joined)
    except Exception:
        return joined

    # DDG redirect path is /l/ or /l — extract the uddg param
    if parsed.path in ("/l/", "/l"):
        qs = urllib.parse.parse_qs(parsed.query)
        targets = qs.get("uddg", [])
        if targets:
            return html.unescape(urllib.parse.unquote(targets[0]))

    return joined


def _extract_ddgo_hits(html_body: str) -> list[dict]:
    """
    Parse DuckDuckGo HTML search results.
    Looks for anchors with class 'result__a' — DuckDuckGo's result link class.
    Falls back to generic <a href> extraction if nothing found.
    Mirrors claw-code's extract_search_hits + extract_search_hits_from_generic_links.
    """
    hits = []
    seen_urls = set()

    # Primary: find result__a anchors (DuckDuckGo-specific)
    # Pattern: <a class="result__a" href="...">Title text</a>
    pattern = re.compile(
        r'<a\s[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(html_body):
        raw_url = html.unescape(m.group(1))
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        url = _decode_ddgo_url(raw_url)

        if not url.startswith("http"):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        hits.append({"title": title or url, "url": url})

    # Fallback: generic links if DDG-specific parse yielded nothing
    if not hits:
        fallback = re.compile(
            r'<a\s[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        for m in fallback.finditer(html_body):
            url = html.unescape(m.group(1))
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if url in seen_urls:
                continue
            seen_urls.add(url)
            hits.append({"title": title or url, "url": url})

    return hits


def _normalize_domain_filter(domain: str) -> str:
    """
    Normalise a domain filter string so it can be compared against URL hostnames.

    Accepts bare domains ("python.org"), full URLs ("https://python.org/"),
    or anything in between. Returns a lowercase hostname string with leading
    dots and trailing slashes trimmed.

    Ported from claw-code's normalize_domain_filter (Rust → Python).
    """
    trimmed = domain.strip()
    # Try parsing as a full URL first — extract just the hostname
    try:
        parsed = urllib.parse.urlparse(trimmed)
        if parsed.hostname:
            return parsed.hostname.lower().strip(".").rstrip("/")
    except Exception:
        pass
    # Fall back to treating the whole string as a hostname
    return trimmed.lower().strip(".").rstrip("/")


def _host_matches_list(url: str, domains: list) -> bool:
    """Return True if the URL's host matches any domain in *domains*.
    Supports subdomain matching (e.g. 'python.org' matches 'docs.python.org').
    Domain filter entries are normalised via _normalize_domain_filter so callers
    can pass bare hostnames ("python.org") or full URLs ("https://python.org/").
    Ported from claw-code's host_matches_list (Rust → Python).
    """
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(
        (normalized := _normalize_domain_filter(d))
        and (host == normalized or host.endswith("." + normalized))
        for d in domains
    )


def web_search(
    query: str,
    allowed_domains=None,
    blocked_domains=None,
) -> str:
    """
    Search DuckDuckGo and return formatted results.
    Ported from claw-code's execute_web_search (Rust → Python).
    """
    started = time()

    # Build search URL
    params = urllib.parse.urlencode({"q": query})
    search_url = f"{DDGO_SEARCH_URL}?{params}"

    status, content_type, body = _make_request(search_url)

    if status == 0:
        return f"Search error: {body}"

    # Extract hits from HTML
    hits = _extract_ddgo_hits(body)

    # Apply domain filters (mirrors claw-code's retain/filter logic)
    if allowed_domains:
        hits = [h for h in hits if _host_matches_list(h["url"], allowed_domains)]
    if blocked_domains:
        hits = [h for h in hits if not _host_matches_list(h["url"], blocked_domains)]

    # Cap at MAX_SEARCH_RESULTS — matches claw-code's hits.truncate(8)
    hits = hits[:MAX_SEARCH_RESULTS]

    elapsed = time() - started

    if not hits:
        return (
            f'No web search results found for "{query}". '
            f"Try rephrasing the query or using different keywords."
        )

    lines = [f'Search results for "{query}" ({len(hits)} results, {elapsed:.1f}s):\n']
    for i, hit in enumerate(hits, 1):
        lines.append(f"{i}. [{hit['title']}]({hit['url']})")

    lines.append(
        "\nUse web_fetch to read the full content of any of these pages."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5.  WEB FETCH IMPLEMENTATION
#     Ported from claw-code's execute_web_fetch.
#     Fetches a URL, converts HTML → text, trims to MAX_FETCH_CHARS.
# ---------------------------------------------------------------------------

def web_fetch(url: str, focus: str = "") -> str:
    """
    Fetch a URL and return its text content.
    Ported from claw-code's execute_web_fetch (Rust → Python).
    """
    started = time()

    status, content_type, body = _make_request(url)
    elapsed_ms = int((time() - started) * 1000)

    if status == 0:
        return f"Fetch error for {url}: {body}"

    # Convert HTML to text (mirrors claw-code's normalize_fetched_content)
    is_html = "html" in content_type or body.lstrip().startswith("<!") or "<html" in body[:200].lower()
    if is_html:
        text = html_to_text(body)
    else:
        text = body.strip()

    # Trim to MAX_FETCH_CHARS
    # If a focus hint is given, try to surface the most relevant section
    if focus and len(text) > MAX_FETCH_CHARS:
        focus_lower = focus.lower()
        text_lower = text.lower()
        idx = text_lower.find(focus_lower.split()[0]) if focus_lower.split() else -1
        if idx != -1:
            # Start from 200 chars before the first keyword match
            start = max(0, idx - 200)
            text = text[start:start + MAX_FETCH_CHARS]
            if start > 0:
                text = f"[…content trimmed, showing section near '{focus}'…]\n\n" + text
        else:
            text = text[:MAX_FETCH_CHARS]
            text += f"\n\n[…page truncated at {MAX_FETCH_CHARS} characters…]"
    elif len(text) > MAX_FETCH_CHARS:
        text = text[:MAX_FETCH_CHARS]
        text += f"\n\n[…page truncated at {MAX_FETCH_CHARS} characters…]"

    return (
        f"URL: {url}\n"
        f"Status: {status} | Content-type: {content_type} | Fetched in {elapsed_ms}ms\n"
        f"{'─' * 60}\n"
        f"{text}"
    )


# ---------------------------------------------------------------------------
# 6.  TOOL DISPATCHER
# ---------------------------------------------------------------------------

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Route a Claude tool call to the correct Python implementation."""
    if tool_name == "web_search":
        return web_search(
            query=tool_input["query"],
            allowed_domains=tool_input.get("allowed_domains"),
            blocked_domains=tool_input.get("blocked_domains"),
        )
    elif tool_name == "web_fetch":
        return web_fetch(
            url=tool_input["url"],
            focus=tool_input.get("focus", ""),
        )
    else:
        return f"Error: unknown tool '{tool_name}'."


# ---------------------------------------------------------------------------
# 7.  SYSTEM PROMPT
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
## Role
You are a research assistant that finds accurate, up-to-date information from the web.
Given a question or research task, you search for relevant sources, read them,
and synthesize a clear, well-sourced answer.

## Workflow
1. **Plan your search** — break the task into 1-3 focused search queries.
2. **Search** — call web_search with each query. Prefer specific queries over broad ones.
3. **Read the best sources** — call web_fetch on the most relevant URLs from your results.
   You can fetch multiple URLs in parallel to save time.
4. **Synthesize** — combine what you've learned into a clear, accurate answer.
5. **Cite sources** — always include a Sources section with the URLs you used.

## Hard Rules
- Never make up facts — only report what you actually found on the web.
- Always include sources for every claim in your final answer.
- If search results are thin or contradictory, say so honestly.
- Prefer primary sources (official docs, official announcements) over secondary ones.
- If a page fetch fails, try the next best result from your search.

## Answer Format
End your final response with this structure:

---
### Answer
<Clear, direct answer to the user's question>

### Key Findings
<Bullet points of the most important facts you found>

### Sources
<Numbered list of URLs you actually read and cited>
---
""".strip()


# ---------------------------------------------------------------------------
# 8.  THE AGENTIC LOOP
# ---------------------------------------------------------------------------

def research(question: str) -> str:
    """
    Run the web search agent on a research question.

    Args:
        question: The research question or task.

    Returns:
        The agent's final synthesized answer as a plain string.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    messages = [{"role": "user", "content": question}]

    print(f"\n{'=' * 65}")
    print("WEB SEARCH AGENT")
    print(f"Question: {question}")
    print(f"{'=' * 65}")

    iteration = 0

    while True:
        iteration += 1

        if iteration > MAX_ITERATIONS:
            msg = (
                f"Agent hit the {MAX_ITERATIONS}-iteration limit. "
                "Returning what was gathered so far."
            )
            print(f"\n[WARN] {msg}")
            return msg

        print(f"\n[loop #{iteration}] Sending {len(messages)} messages …")

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        print(
            f"[loop #{iteration}] stop_reason={response.stop_reason!r}  "
            f"tokens=({response.usage.input_tokens} in / "
            f"{response.usage.output_tokens} out)"
        )

        # ── CASE A: Claude is done ─────────────────────────────────────────
        if response.stop_reason == "end_turn":
            final = next(
                (b.text for b in response.content if b.type == "text"),
                "(no text response)",
            )
            print(f"\n{'=' * 65}")
            print("AGENT ANSWER")
            print(f"{'=' * 65}")
            print(final)
            return final

        # ── CASE B: Claude wants to call tools ────────────────────────────
        if response.stop_reason == "tool_use":

            # Append full assistant content before executing tools
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                print(f"  → {block.name}({json.dumps(block.input)})")
                result = execute_tool(block.name, block.input)

                preview = result[:200].replace("\n", "\\n")
                print(f"  ← {preview}{'…' if len(result) > 200 else ''}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            # All results in one user message
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        print(f"[WARN] Unexpected stop_reason: {response.stop_reason!r} — exiting loop.")
        break

    return "(agent stopped unexpectedly)"


# ---------------------------------------------------------------------------
# 9.  ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # Allow passing a question as a command-line argument:
        # python agent.py "What is the latest version of Python?"
        question = " ".join(sys.argv[1:])
    else:
        question = "What are the key new features in Python 3.13?"

    research(question)
