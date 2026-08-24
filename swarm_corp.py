"""
swarm_corp.py — a small multi-role coding swarm running entirely on free
LLM provider tiers (Groq, Cerebras, NVIDIA NIM, Google AI Studio; Ollama for
local/Phase 5). See CLAUDE.md for the constraints this file has to honor:

  - Free tiers only, across every provider in providers.py. No paid API
    keys required to run the default (Groq-only) path.
  - Every completion request is capped per its OWN provider's TPM limit
    (see providers.PROVIDERS) — Cerebras's 60K is not Groq's 8K, and both
    are enforced independently via TokenBudget.
  - The Reviewer must resolve to a different model *family* than the
    Coder (checked at startup, not just hoped for) — the whole point of
    a second reviewing model is that it doesn't share the first one's
    blind spots.
  - The Anthropic/paid-tier code path below is dormant by construction:
    SWARM_TIER defaults to "free" in .env.example, and nothing in the
    normal run_swarm() path ever calls get_anthropic_client(). Flip
    SWARM_TIER yourself if you ever want it; the swarm never will.

Usage:
    python swarm_corp.py "add a /health endpoint with a test"

Escalation lane: if the swarm can't get something approved after
MAX_ROUNDS, it writes its last attempt + the Reviewer's objections to
swarm_output/ and exits non-zero. That's the signal to open the same
task in Claude Code instead of spending more free-tier tokens on it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import groq
import openai
from dotenv import load_dotenv

from sandbox import sandbox_run
from context import load_repo_context
from providers import available_providers, get_client, get_tpm_limit, split_ref
import ui
import tools
import gates

# Windows terminals often default to cp1252, which can't encode the em-dashes
# and other punctuation used in these messages. Force UTF-8 on stdout so
# output doesn't get mangled into replacement characters.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

ROOT = Path(__file__).parent
AGENTS_DIR = ROOT / "agents"
OUTPUT_DIR = ROOT / "swarm_output"

# ---------------------------------------------------------------------------
# Model registry. Each role lists candidates in preference order; the first
# one that's actually live on your key wins (see resolve_models). Groq slugs
# drift — that's expected, not a bug — so this list is meant to be edited.
# Run `python verify_key.py` after editing it.
# ---------------------------------------------------------------------------
# Candidates are "provider:model" refs, tried in order; the first one live on
# your configured keys wins. A bare slug (no colon) means Groq, so the original
# single-provider config still resolves unchanged.
#
# NOTE on ordering: live_model_refs() only confirms a model appears in the
# provider's /models catalog, not that inference actually succeeds. Live
# testing found Cerebras lists models on this account but returns 402
# payment_required on every call (free-tier trial not active on this key) —
# so Cerebras candidates are placed LAST, not first. If billing gets
# activated later, they'll start winning automatically with no code change;
# until then, Coder/Planner/Tester fall through to providers that actually
# answer (NVIDIA, Groq).
MODEL_REGISTRY: dict[str, list[str]] = {
    "planner": [
        "groq:openai/gpt-oss-20b",
        "gemini:gemini-3.6-flash",
        "groq:openai/gpt-oss-120b",
        "cerebras:gpt-oss-120b",
    ],
    "tester": [
        "groq:groq/compound-mini",
        "mistral:mistral-medium-2508",
        "groq:groq/compound",
        "nvidia:meta/llama-3.3-70b-instruct",
        "cerebras:gemma-4-31b",
    ],
    "coder": [
        "groq:openai/gpt-oss-120b",
        "mistral:codestral-latest",
        "openrouter:cohere/north-mini-code:free",
        "groq:openai/gpt-oss-20b",
        "gemini:gemini-3.6-flash",
        "cerebras:gpt-oss-120b",
    ],
    "security": [
        "groq:openai/gpt-oss-safeguard-20b",
        "openrouter:nvidia/nemotron-3.5-content-safety:free",
        "groq:groq/compound",
        "nvidia:meta/llama-3.3-70b-instruct",
        "cerebras:llama-3.3-70b",
    ],
    "reviewer": [
        "groq:qwen/qwen3.6-27b",
        "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
        "gemini:gemini-pro-latest",
        "groq:groq/compound-mini",
    ],
    "arbiter": [
        "nvidia:meta/llama-3.3-70b-instruct",
        "mistral:mistral-medium-2508",
        "gemini:gemini-pro-latest",
        "groq:groq/compound",
        "groq:groq/compound-mini",
    ],
    # Phase 6: structural audit, informational only — not a gate. Prefers
    # a strong reasoner distinct from Coder's family where possible.
    "architect": [
        "cohere:c4ai-aya-expanse-32b",
        "huggingface:deepseek-ai/DeepSeek-V4-Flash-0731",
        "gemini:gemini-pro-latest",
        "groq:groq/compound",
    ],
}

# Phase 5: every role runs on the local model in --private mode. Family
# diversity is deliberately NOT enforced here — with one local model there
# is only one family available, so the choice is "one model reviewing its
# own output" or "send private code to a third party". For clinical/PII
# work the privacy guarantee is worth more than the diversity check, but
# it IS a real loss of review quality, and run_swarm() says so out loud
# rather than letting it pass silently.
PRIVATE_MODEL = "ollama:qwen2.5-coder:3b"

MAX_ROUNDS = 4  # coder attempts before we punt to Claude Code
# Cap on info-gathering rounds. Without this the Coder can spend every one
# of MAX_ROUNDS asking for tools and never submit code — the run then
# "fails" having produced nothing, which looks like a model failure but is
# really the harness letting itself be starved.
MAX_TOOL_ROUNDS = 2
TPM_WINDOW_SECONDS = 60


def family_of(ref: str) -> str:
    """Model family, ignoring the provider prefix.

    'cerebras:llama-3.3-70b' -> 'llama'; 'groq:openai/gpt-oss-120b' -> 'openai'.
    The provider is deliberately NOT the family: the same open-weight model
    served by two providers shares its blind spots, so diversity has to be
    measured on the model, not on who hosts it.

    Different providers publish the SAME model under different slug shapes —
    Groq calls it 'openai/gpt-oss-120b', Cerebras just 'gpt-oss-120b' — so a
    naive parse would report those as different families and let two
    instances of the identical model satisfy a diversity check. Known
    multi-provider model names are normalised here first.
    """
    _, slug = split_ref(ref)
    slug = slug.lower()
    for known in ("gpt-oss", "llama-3.3", "llama-4", "qwen3", "qwen-3", "deepseek", "mistral", "gemma"):
        if known in slug:
            return known
    if "/" in slug:
        return slug.split("/")[0]
    return slug.split("-")[0]


def live_model_refs() -> set[str]:
    """Union of "provider:model" refs live across every configured provider.

    Queried at startup rather than hardcoded: this key advertised 20+ Groq
    models but only 6 were actually reachable, so a static list rots silently.
    A provider that errors (bad key, outage) is skipped with a warning instead
    of taking the whole run down.
    """
    refs: set[str] = set()
    for name in available_providers():
        try:
            client = get_client(name)
            for m in client.models.list().data:
                refs.add(f"{name}:{m.id}")
        except Exception as exc:  # noqa: BLE001 - degrade, don't abort
            print(f"  (provider '{name}' unavailable, skipping: {type(exc).__name__})")
    return refs


def resolve_models(live_ids: set[str] | None = None) -> dict[str, str]:
    """Pick the first live candidate for each role. Raises RuntimeError with
    a specific, actionable message rather than letting a dead slug surface
    as an opaque 404 mid-swarm."""
    if live_ids is None:
        live_ids = live_model_refs()
    resolved: dict[str, str] = {}
    for role, candidates in MODEL_REGISTRY.items():
        # Normalise bare slugs to groq: so old-style entries still match.
        pick = next(
            (ref for ref in candidates if f"{split_ref(ref)[0]}:{split_ref(ref)[1]}" in live_ids),
            None,
        )
        if pick is None:
            raise RuntimeError(
                f"No live model for role '{role}'. Tried {candidates}, none are live on the "
                f"configured providers ({', '.join(available_providers()) or 'none'}). Run "
                f"`python verify_key.py` to see what's available and update MODEL_REGISTRY."
            )
        resolved[role] = pick

    # Enforce family diversity: coder ≠ reviewer, coder ≠ tester, arbiter ≠ coder/reviewer
    if family_of(resolved["coder"]) == family_of(resolved["reviewer"]):
        raise RuntimeError(
            f"Coder ({resolved['coder']}) and Reviewer ({resolved['reviewer']}) must differ in family."
        )
    if family_of(resolved["coder"]) == family_of(resolved["tester"]):
        raise RuntimeError(
            f"Coder ({resolved['coder']}) and Tester ({resolved['tester']}) must differ in family."
        )
    if family_of(resolved["arbiter"]) in {family_of(resolved["coder"]), family_of(resolved["reviewer"])}:
        raise RuntimeError(
            f"Arbiter ({resolved['arbiter']}) must differ from both Coder and Reviewer."
        )
    return resolved


# ---------------------------------------------------------------------------
# Dormant paid-tier path. SWARM_TIER defaults to "free" (see .env.example).
# Nothing below this block references get_anthropic_client — it exists so a
# future paid tier doesn't require restructuring this file, not because the
# swarm ever uses it today.
# ---------------------------------------------------------------------------
SWARM_TIER = os.environ.get("SWARM_TIER", "free")


def get_anthropic_client():
    """Unused by every code path in this file. Only reachable if you call it
    yourself after setting SWARM_TIER to something other than 'free' and
    providing ANTHROPIC_API_KEY. Left as-is per project constraints — this
    project does not spend on the Anthropic API; Claude Code (your existing
    Pro subscription) is the escalation lane instead."""
    if SWARM_TIER == "free":
        raise RuntimeError("SWARM_TIER is 'free' — the anthropic provider is intentionally disabled.")
    import anthropic  # optional dep, deliberately not in requirements.txt

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("SWARM_TIER != 'free' but ANTHROPIC_API_KEY is not set.")
    return anthropic.Anthropic(api_key=key)


# ---------------------------------------------------------------------------
# Token budgeting. Each provider enforces its own TPM cap *per model*
# (Groq ~8000, Cerebras 60000, ...), and Groq counts prompt + max_tokens
# together — a real run measured a 413 at 8013 requested even though our
# naive chars/4 estimate said we had room. Two corrections for that: a more
# conservative (higher) token estimate, and a per-provider safety margin so
# estimation error and per-message formatting overhead don't tip us over.
# ---------------------------------------------------------------------------
BUFFER_FOR_OVERHEAD = 700  # headroom below each provider's real TPM cap


def estimate_tokens(text: str) -> int:
    # ~3.3 chars/token is a deliberately pessimistic estimate (real English
    # prose averages closer to 4) so we round up rather than get caught
    # short on token-dense text like code.
    return max(1, -(-len(text) // 3))


class TokenBudget:
    """Tracks TPM usage per "provider:model" ref, each against ITS OWN
    provider's limit. A single global cap (the old design) would either
    starve Cerebras's 60K budget down to Groq's 8K, or blow through Groq's
    real cap by applying Cerebras's — providers are not interchangeable
    here."""

    def __init__(self):
        self._windows: dict[str, list[tuple[float, int]]] = {}  # ref -> [(timestamp, tokens)]

    def _limit_for(self, ref: str) -> int:
        provider, _ = split_ref(ref)
        try:
            return get_tpm_limit(provider) - BUFFER_FOR_OVERHEAD
        except ValueError:
            return 8000 - BUFFER_FOR_OVERHEAD  # unknown provider: fall back conservative

    def completion_cap(self, ref: str, prompt_text: str) -> int:
        """Max completion tokens we can ask for without blowing this ref's
        per-request cap. Floored at 512 (reasoning models need headroom for
        hidden <think> tokens before visible output). Capped at 3500 so
        large models have room to emit full implementations without
        truncation, even on providers with a much bigger ceiling."""
        remaining = self._limit_for(ref) - estimate_tokens(prompt_text)
        return max(512, min(remaining, 3500))

    def record(self, ref: str, tokens_used: int) -> None:
        now = time.time()
        if ref not in self._windows:
            self._windows[ref] = []
        self._windows[ref] = [(t, n) for t, n in self._windows[ref] if now - t < TPM_WINDOW_SECONDS]
        self._windows[ref].append((now, tokens_used))

    def wait_if_needed(self, ref: str, upcoming_tokens: int) -> None:
        now = time.time()
        if ref not in self._windows:
            self._windows[ref] = []
        self._windows[ref] = [(t, n) for t, n in self._windows[ref] if now - t < TPM_WINDOW_SECONDS]
        used = sum(n for _, n in self._windows[ref])
        limit = self._limit_for(ref) + BUFFER_FOR_OVERHEAD  # compare against the real cap, not the padded one
        if used + upcoming_tokens > limit:
            if self._windows[ref]:
                sleep_for = TPM_WINDOW_SECONDS - (now - self._windows[ref][0][0])
                if sleep_for > 0:
                    ui.status(f"  (waiting {sleep_for:.0f}s — {ref} at {used} TPM, upcoming request needs {upcoming_tokens})")
                    time.sleep(sleep_for)
            else:
                raise RuntimeError(f"Requested {upcoming_tokens} tokens for {ref}, but cap is {limit} TPM and a single request cannot fit")


# ---------------------------------------------------------------------------
# Persona loading — agents/*.md files with a tiny frontmatter block:
#   ---
#   role: coder
#   ---
#   <system prompt body>
# ---------------------------------------------------------------------------
def load_persona(filename: str) -> str:
    path = AGENTS_DIR / filename
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        _, _, body = rest.partition("---")
        return body.strip()
    return text.strip()


def _find_truncation_point(text: str, max_chars: int) -> int:
    """Find a good break point to truncate text. Prefers to break:
    1. At the last '\n\n' (paragraph break) before max_chars
    2. At the last '\n' (line break) if no paragraph break
    3. At max_chars if no breaks found"""
    if len(text) <= max_chars:
        return len(text)
    # Try to find a paragraph break in the safe zone (90% of available)
    safe_zone = int(max_chars * 0.9)
    last_para = text.rfind("\n\n", 0, safe_zone)
    if last_para != -1:
        return last_para
    # Fall back to last line break
    last_line = text.rfind("\n", 0, safe_zone)
    if last_line != -1:
        return last_line
    # Fall back to the safe zone itself
    return safe_zone


ELISION_MARKER = "\n\n[... middle truncated for budget ...]\n\n"


def _fit_user_text(system: str, user: str, budget: TokenBudget, ref: str) -> str:
    """If system + user + the minimum floor completion would already blow
    the per-request cap, truncate user (the variable-length part — usually
    criteria/tests + task + feedback) rather than let the request 413.

    Cuts the MIDDLE, keeping a head chunk and a tail chunk: the task and the
    Reviewer/Arbiter's feedback live at the end of the prompt (see run_swarm),
    so a tail-only cut silently drops the one thing the Coder most needs to
    act on. The head still carries enough context (criteria/tests) to be
    useful, and the elision marker is only emitted when something was
    actually removed."""
    floor = 3500  # must match completion_cap's cap so truncation reserves enough room
    available_for_user = (budget._limit_for(ref) - floor) - estimate_tokens(system)
    available_chars = max(0, available_for_user * 3)  # inverse of estimate_tokens' ceil(len/3)
    if len(user) <= available_chars:
        return user

    marker_chars = len(ELISION_MARKER)
    budget_for_content = max(0, available_chars - marker_chars)
    head_chars = budget_for_content // 2
    tail_chars = budget_for_content - head_chars

    head_point = _find_truncation_point(user, head_chars)
    head = user[:head_point]
    # For the tail, find a break point within the last tail_chars of the text.
    tail_start = max(head_point, len(user) - tail_chars)
    next_break = user.find("\n", tail_start)
    if next_break != -1 and next_break < len(user) - 1:
        tail_start = next_break + 1
    tail = user[tail_start:]

    return head + ELISION_MARKER + tail


def complete(
    budget: TokenBudget,
    ref: str,
    system: str,
    user: str,
    temperature: float,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """Call the ref's provider with exponential backoff on 429/503.

    ref is a "provider:model" string (e.g. "cerebras:llama-3.3-70b"); a bare
    slug with no colon defaults to groq. One client per call keeps this
    stateless with respect to which provider each role landed on this run.

    on_chunk, if given, streams the response and is called once per text
    delta as it arrives (ui.stream_chunk). The returned string is always the
    full assembled text either way — streaming changes what you see live,
    not what callers receive. Token accounting is unaffected: providers that
    support stream_options={"include_usage": True} (Groq, OpenAI-compatible)
    return real usage in the final chunk; if a provider doesn't, usage falls
    back to the same estimate the non-streaming path already used."""
    provider, model = split_ref(ref)
    client = get_client(provider)

    user = _fit_user_text(system, user, budget, ref)
    prompt_text = system + user
    max_tokens = budget.completion_cap(ref, prompt_text)

    max_retries = 3
    for attempt in range(max_retries):
        budget.wait_if_needed(ref, estimate_tokens(prompt_text) + max_tokens)
        try:
            if on_chunk is not None:
                stream = client.chat.completions.create(
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                text_parts: list[str] = []
                usage_tokens = None
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        delta = chunk.choices[0].delta.content
                        text_parts.append(delta)
                        on_chunk(delta)
                    if getattr(chunk, "usage", None):
                        usage_tokens = chunk.usage.total_tokens
                full_text = "".join(text_parts)
                budget.record(ref, usage_tokens or (estimate_tokens(prompt_text) + estimate_tokens(full_text)))
                return full_text

            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            usage = resp.usage
            budget.record(ref, getattr(usage, "total_tokens", estimate_tokens(prompt_text) + max_tokens))
            return resp.choices[0].message.content or ""
        except (openai.APIStatusError, groq.APIStatusError) as exc:
            status = getattr(exc, "status_code", None)
            if status in {429, 503} and attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 1s, 2s, 4s
                ui.status(f"  (rate limited; retrying in {wait_time}s...)")
                time.sleep(wait_time)
                continue
            raise RuntimeError(f"API call to {ref} failed ({status}): {exc}") from exc
    raise RuntimeError(f"API call to {ref} failed after {max_retries} retries")


def extract_verdict(review: str) -> tuple[str, str]:
    """Parse a Reviewer response for the verdict. Handles <think> blocks
    and misplaced verdicts by:
    1. Stripping <think>...</think> blocks (model deliberation)
    2. Finding the LAST occurrence of VERDICT: (final decision, not mid-thought changes)
    3. Returning (verdict, reasoning_only) where reasoning_only has <think> stripped

    Returns:
        (verdict, reasoning) where verdict is one of:
        - "APPROVE"
        - "REJECT"
        - "UNPARSEABLE" (if no valid VERDICT: line found)
    """
    # Strip <think> blocks first — they contain model deliberation, not final output
    stripped = re.sub(r"<think>.*?</think>", "", review, flags=re.DOTALL)

    # Find the LAST VERDICT: line (final decision, not waffling mid-thought)
    lines = stripped.split("\n")
    verdict_line_idx = None
    verdict_text = None
    for i in range(len(lines) - 1, -1, -1):
        match = re.search(r"VERDICT:\s*(APPROVE|REJECT)", lines[i], re.IGNORECASE)
        if match:
            verdict_line_idx = i
            verdict_text = match.group(1).upper()
            break

    if verdict_text is None:
        return ("UNPARSEABLE", stripped)

    # Reasoning is everything after the verdict line; if nothing follows,
    # fall back to lines before it (some models front-load reasoning).
    reasoning = "\n".join(lines[verdict_line_idx + 1:]).strip()
    if not reasoning:
        reasoning = "\n".join(lines[:verdict_line_idx]).strip()
    return (verdict_text, reasoning)



def parse_and_write_files(coder_output: str, workspace_root: Path, allow_tests: bool = False) -> list[Path]:
    """Parse `=== FILE: path ===` blocks and write them.

    allow_tests=True is for the Tester, which owns the tests/ subtree. Every
    other caller (i.e. the Coder) is refused there, so the independently
    authored tests can't be overwritten by the code they're meant to judge.

    Strips accidental markdown code fences (```lang / ```) as a hard backstop.

    Returns list of written file paths. Raises if the writer tries to break out
    of workspace_root (e.g., uses ../ in paths). Does NOT roll back on error —
    files written before an error are kept."""
    workspace_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # Split by file marker
    parts = re.split(r"^=== FILE: (.+?) ===$", coder_output, flags=re.MULTILINE)
    # parts[0] is preamble, then alternates [path, content, path, content, ...]

    for i in range(1, len(parts), 2):
        if i + 1 >= len(parts):
            break
        file_path_str = parts[i].strip()
        content = parts[i + 1]

        # Strip accidental markdown fences (backstop against fence-wrapped
        # output). re.split leaves the newline that followed the marker line,
        # so content starts with "\n" — the leading \s* is load-bearing.
        content = re.sub(r"^\s*```[\w+-]*[ \t]*\n", "", content)
        content = re.sub(r"\n```[ \t]*\s*$", "\n", content)

        # Strip an invented trailing sentinel some models append after the
        # last file (e.g. "=== END ==="), even though it's not part of the
        # === FILE: ... === contract. Left in, it lands as the last line of
        # real code and breaks ast.parse on anything Python.
        content = re.sub(r"\n=+\s*END\s*=+\s*$", "\n", content, flags=re.IGNORECASE)

        # Security: reject paths that try to escape or overwrite tests
        if ".." in file_path_str or file_path_str.startswith("/"):
            raise RuntimeError(f"Unsafe file path: {file_path_str}")
        if not allow_tests and (file_path_str.startswith("tests/") or file_path_str.startswith("tests\\")):
            raise RuntimeError(f"Coder cannot write to tests/: {file_path_str}. Tester writes all tests.")

        file_path = workspace_root / file_path_str
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        written.append(file_path)

    return written


# ---------------------------------------------------------------------------
# Tool calling — bounded, opt-in, free-tools-only from this loop.
#
# The Coder may ask for AT MOST ONE tool call per round, via a
# "=== TOOL: name(k=v, ...) ===" block on its own line anywhere in its
# output (same split-and-parse shape as === FILE: === above). The result
# is fed back as feedback for the NEXT round — this is deliberately not a
# nested per-round loop (call tool, re-prompt, call tool again...): that
# would make the token cost of a single round open-ended, which is exactly
# what TokenBudget exists to prevent. One request, one result, one round.
# ---------------------------------------------------------------------------
_TOOL_REQUEST_RE = re.compile(r"^=== TOOL:\s*(\w+)\((.*?)\)\s*===\s*$", re.MULTILINE)

# Only tools.py's free/unattended functions — gated ones require input()
# and have no place firing automatically inside an unattended run.
TOOL_DISPATCH: dict[str, Callable[..., dict]] = {
    "read_file": tools.read_file,
    "list_dir": tools.list_dir,
    "grep": tools.grep,
    "git_status": tools.git_status,
    "git_diff": tools.git_diff,
    "git_log": tools.git_log,
    "http_get": tools.http_get,
}


def parse_tool_request(coder_output: str) -> tuple[str, dict[str, str]] | None:
    """Find the first (only the first is honored — one call per round)
    '=== TOOL: name(k=v, ...) ===' block. Returns (name, kwargs) or None."""
    match = _TOOL_REQUEST_RE.search(coder_output)
    if match is None:
        return None
    name, arg_str = match.group(1), match.group(2)
    kwargs: dict[str, str] = {}
    for part in arg_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue  # malformed arg, skip rather than crash the round
        k, v = part.split("=", 1)
        kwargs[k.strip()] = v.strip().strip("'\"")
    return name, kwargs


def run_tool_request(coder_output: str) -> str | None:
    """If the Coder asked for a tool, run it and return a feedback string
    to prepend to next round's prompt. Returns None if no request was made
    or the requested tool isn't in the free-tool dispatch table."""
    parsed = parse_tool_request(coder_output)
    if parsed is None:
        return None
    name, kwargs = parsed
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return f"Tool request '{name}' is not available (not a free/unattended tool). Available: {', '.join(TOOL_DISPATCH)}."
    try:
        result = fn(**kwargs)
    except TypeError as exc:
        return f"Tool '{name}' call failed — bad arguments: {exc}"
    if result.get("ok"):
        # Already redacted inside tools.py before this ever reaches here.
        body = result.get("content") or result.get("output") or result.get("entries") or result.get("matches") or "(no content)"
        return f"Tool '{name}' result:\n{body}"
    return f"Tool '{name}' failed: {result.get('error', 'unknown error')}"


def run_swarm(task: str, max_rounds: int = MAX_ROUNDS, repo_path: str | None = None, private: bool = False) -> int:
    if private:
        from providers import ollama_is_running
        if not ollama_is_running():
            print("[FAIL] --private needs a local Ollama server, but nothing is answering on localhost:11434.")
            return 1
        try:
            local_ids = {m.id for m in get_client("ollama").models.list().data or []}
        except Exception as exc:
            print(f"[FAIL] --private: could not query local Ollama models: {exc}")
            return 1
        _, want = split_ref(PRIVATE_MODEL)
        if want not in local_ids:
            print(f"[FAIL] --private needs '{want}' pulled locally. Run: ollama pull {want}")
            return 1
        ui.note("PRIVATE MODE — every role runs locally; nothing leaves this machine.")
        ui.note(f"  All roles: {PRIVATE_MODEL}")
        ui.note("  NOTE: one local model means Coder and Reviewer share a family.")
        ui.note("  The cross-family review guarantee does NOT hold in this mode.")
        models = {role: PRIVATE_MODEL for role in MODEL_REGISTRY}
        return _run_pipeline(task, models, max_rounds, repo_path, private=True)

    providers = available_providers()
    if not providers:
        print(
            "[FAIL] No provider API keys are set. Copy .env.example to .env and paste at "
            "least GROQ_API_KEY in — see .env.example for the other free providers."
        )
        return 1
    ui.note(f"Providers: {', '.join(providers)}")

    try:
        models = resolve_models()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 1

    return _run_pipeline(task, models, max_rounds, repo_path, private=False)


def _run_pipeline(
    task: str,
    models: dict[str, str],
    max_rounds: int,
    repo_path: str | None,
    private: bool,
) -> int:
    """The Planner->Tester->Coder<->Sandbox<->Review->Arbiter pipeline,
    shared by the normal multi-provider path and --private (where `models`
    is every role pointed at the one local Ollama model instead of
    resolve_models()'s per-role picks)."""
    # Load all personas
    coder_persona = load_persona("coder.md")
    reviewer_persona = load_persona("reviewer.md")
    planner_persona = load_persona("planner.md")
    tester_persona = load_persona("tester.md")
    security_persona = load_persona("security.md")
    arbiter_persona = load_persona("arbiter.md")
    architect_persona = load_persona("architect.md")

    rubric_path = ROOT / "RUBRIC.md"
    if rubric_path.exists():
        reviewer_persona = reviewer_persona + "\n\n" + rubric_path.read_text(encoding="utf-8")

    budget = TokenBudget()
    repo_context = load_repo_context(repo_path) if repo_path else None
    workspace_root = OUTPUT_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    # Tests live in tests/ and import modules written at the workspace root
    # (the normal Python layout). pytest won't put that root on sys.path by
    # itself, so without this every such task fails at collection with
    # ModuleNotFoundError no matter what the Coder writes.
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )

    # Free tools (read_file/list_dir/grep/git-readonly/http_get) are
    # confined to the workspace plus --repo, if given. Gated tools exist in
    # tools.py but aren't auto-dispatched from this loop — an unattended
    # run shouldn't block on input() unexpectedly mid-swarm.
    tool_roots = [workspace_root]
    if repo_path:
        tool_roots.append(Path(repo_path).resolve())
    tools.configure(allowed_roots=tool_roots)
    if private:
        tools.disable_network()

    ui.note(f"Planner  : {models['planner']}")
    ui.note(f"Tester   : {models['tester']}")
    ui.note(f"Coder    : {models['coder']}")
    ui.note(f"Security : {models['security']}")
    ui.note(f"Reviewer : {models['reviewer']}")
    ui.note(f"Arbiter  : {models['arbiter']}")
    ui.note(f"Architect: {models['architect']}")
    if repo_context:
        ui.note(f"Repo     : {repo_path}\n")
    else:
        ui.note("")

    # Phase 1: Planner writes acceptance criteria
    ui.status("--- planning ---")
    try:
        ui.start_role("Planner", models["planner"])
        criteria = complete(budget, models["planner"], planner_persona, f"Task:\n{task}", temperature=0.3, on_chunk=ui.stream_chunk)
        ui.end_role()
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 1

    # Phase 2: Tester writes tests based on criteria
    ui.status("--- testing (independent) ---")
    try:
        ui.start_role("Tester", models["tester"])
        tester_user = f"Acceptance criteria:\n{criteria}\n\nWrite tests based on these criteria, not implementation."
        tests_output = complete(budget, models["tester"], tester_persona, tester_user, temperature=0.3, on_chunk=ui.stream_chunk)
        ui.end_role()
        try:
            test_files = parse_and_write_files(tests_output, workspace_root, allow_tests=True)
            if not test_files:
                print("[FAIL] Tester produced no test files (bad === FILE: === format).")
                return 1
            ui.status(f"  Wrote {len(test_files)} test file(s)")
        except RuntimeError as exc:
            print(f"[FAIL] Tester output invalid: {exc}")
            return 1
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 1

    # Phase 3: Main loop — Coder revises until approved
    feedback = ""
    attempt = ""
    security_passed = False
    architect_ran = False
    tool_rounds_used = 0
    history: list[dict[str, str]] = []

    for round_no in range(1, max_rounds + 1):
        ui.status(f"--- round {round_no} ---")
        # The Coder sees the tests so it matches their import surface and
        # function signatures. Independence comes from the tests being written
        # before/blind to the implementation, not from hiding them.
        coder_user = (
            f"Acceptance criteria:\n{criteria}\n\n"
            f"These tests were written independently and WILL be run against your code. "
            f"Match their imports and signatures exactly. Do not rewrite them:\n{tests_output}\n\n"
            f"Task:\n{task}"
        )
        if repo_context and round_no == 1:
            coder_user = f"Repository:\n{repo_context}\n\n" + coder_user
        if feedback:
            coder_user += f"\n\nFeedback from prior round:\n{feedback}\n\nRevise accordingly."

        try:
            ui.start_role("Coder", models["coder"])
            attempt = complete(budget, models["coder"], coder_persona, coder_user, temperature=0.3, on_chunk=ui.stream_chunk)
            ui.end_role()

            # A tool request consumes this round as a pure info-gathering
            # round: skip file-writing/sandbox/review, feed the result back
            # as feedback, move straight to the next round. This does cost
            # one of MAX_ROUNDS's code-writing attempts — a deliberate
            # tradeoff for keeping tool calls bounded (see run_tool_request's
            # docstring) rather than letting a round's token cost be
            # open-ended.
            tool_feedback = run_tool_request(attempt) if tool_rounds_used < MAX_TOOL_ROUNDS else None
            if tool_feedback is not None:
                tool_rounds_used += 1
                remaining = MAX_TOOL_ROUNDS - tool_rounds_used
                ui.status(f"  Tool request handled (info-gathering round; {remaining} tool round(s) left)")
                feedback = (
                    f"{tool_feedback}\n\n"
                    f"You have {remaining} tool request(s) remaining. "
                    f"Write the implementation now unless you genuinely cannot proceed."
                    if remaining == 0
                    else f"{tool_feedback}\n\nYou have {remaining} tool request(s) remaining."
                )
                history.append({"round": str(round_no), "attempt": attempt, "review": f"(tool round)\n{tool_feedback}", "verdict": "TOOL"})
                continue

            # Parse and write files (excluding tests, which are Tester's domain)
            try:
                written_files = parse_and_write_files(attempt, workspace_root)
                if written_files:
                    ui.status(f"  Wrote {len(written_files)} file(s)")
            except RuntimeError as exc:
                ui.status(f"  File write failed: {exc}")
                written_files = []

            # Run Tester's tests against Coder's implementation
            sandbox_result = sandbox_run(workspace_root, timeout_sec=30)
            test_note = f"Test result: {sandbox_result['note']} (exit code {sandbox_result['returncode']})"
            if sandbox_result["stdout"]:
                test_note += f"\n{sandbox_result['stdout']}"
            if sandbox_result["stderr"]:
                test_note += f"\nErrors: {sandbox_result['stderr']}"

            # Auto-reject if tests failed (free reject, save a Reviewer call)
            # pytest reports failures on stdout, not stderr, so check both
            if not sandbox_result["passed"] and (sandbox_result["stderr"] or sandbox_result["stdout"]):
                ui.status("  [Auto-reject] Tests failed")
                output = sandbox_result["stderr"] or sandbox_result["stdout"]
                synthetic_review = f"VERDICT: REJECT\n\nTests failed — fix the implementation:\n{output}"
                review = synthetic_review
            else:
                # Tests passed; run Security check (once, after first pass)
                if not security_passed:
                    ui.start_role("Security", models["security"])
                    security_user = f"Task:\n{task}\n\nCode:\n{attempt}"
                    try:
                        security_output = complete(budget, models["security"], security_persona, security_user, temperature=0.2, on_chunk=ui.stream_chunk)
                        ui.end_role()
                        security_verdict, _ = extract_verdict(security_output)
                        if security_verdict == "REJECT":
                            ui.status("  [Security] Issues found")
                            review = security_output
                        else:
                            security_passed = True
                            review = None
                    except RuntimeError as exc:
                        print(f"[FAIL] Security check failed: {exc}")
                        return 1

                    # Architect audits once, alongside Security — informational
                    # only, never blocks approval. A failure here shouldn't
                    # abort a run that has otherwise passed tests and security.
                    if security_passed and not architect_ran:
                        architect_ran = True
                        try:
                            ui.start_role("Architect", models["architect"])
                            architect_user = f"Task:\n{task}\n\nCode:\n{attempt}"
                            architect_output = complete(budget, models["architect"], architect_persona, architect_user, temperature=0.2, on_chunk=ui.stream_chunk)
                            ui.end_role()
                            (workspace_root / "ARCHITECTURE.md").write_text(architect_output, encoding="utf-8")
                            ui.status("  [Architect] Wrote ARCHITECTURE.md")
                        except RuntimeError as exc:
                            ui.status(f"  [Architect] Skipped (call failed: {exc})")
                else:
                    review = None

                # Reviewer audits if Security passed
                if review is None:
                    ui.start_role("Reviewer", models["reviewer"])
                    review_user = f"Task:\n{task}\n\nAcceptance criteria:\n{criteria}\n\nCoder's submission:\n{attempt}\n\nTest results:\n{test_note}"
                    review = complete(budget, models["reviewer"], reviewer_persona, review_user, temperature=0.2, on_chunk=ui.stream_chunk)
                    ui.end_role()

        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            history.append({"round": str(round_no), "attempt": attempt, "review": f"(error)\n{exc}"})
            _write_output(task, history, approved=False)
            return 1

        verdict, reasoning = extract_verdict(review)
        # Hard gate: no role may approve while the test suite is red. Personas
        # are told this, but prompt instructions are advisory — enforce it in
        # code so a false APPROVE can't exit 0 on broken output.
        if verdict == "APPROVE" and not sandbox_result["passed"]:
            ui.status("  (APPROVE overridden — tests are still failing)")
            verdict = "REJECT"
            reasoning = f"Tests are still failing, so this cannot be approved:\n{sandbox_result['stderr'] or sandbox_result['stdout']}"
        ui.status(f"  Verdict: {verdict}")
        history.append({"round": str(round_no), "attempt": attempt, "review": review, "verdict": verdict})

        if verdict == "APPROVE":
            _write_output(task, history, approved=True)
            print(f"\n[OK] Approved after {round_no} round(s). Output written to swarm_output/.")
            return 0

        if verdict == "UNPARSEABLE":
            ui.status("  Verdict unclear; retrying...")
            retry_prompt = f"Task:\n{task}\n\nCode:\n{attempt[:500]}\n\nPlease state: VERDICT: APPROVE or VERDICT: REJECT (one line only)."
            try:
                ui.start_role("Reviewer (retry)", models["reviewer"])
                review = complete(budget, models["reviewer"], reviewer_persona, retry_prompt, temperature=0.2, on_chunk=ui.stream_chunk)
                ui.end_role()
                verdict, reasoning = extract_verdict(review)
                if verdict == "APPROVE" and not sandbox_result["passed"]:
                    verdict = "REJECT"
                    reasoning = "Tests are still failing, so this cannot be approved."
                ui.status(f"  Verdict (retry): {verdict}")
                if verdict == "APPROVE":
                    _write_output(task, history, approved=True)
                    print(f"\n[OK] Approved after {round_no} round(s). Output written to swarm_output/.")
                    return 0
            except RuntimeError:
                verdict = "REJECT"
                reasoning = "(retry failed)"

        # Deadlock check: if round 3 and still rejected, escalate to Arbiter
        if round_no == max_rounds - 1 and verdict == "REJECT":
            ui.status("  Round 3 rejection; escalating to Arbiter...")
            arbiter_user = f"Task:\n{task}\n\nCriteria:\n{criteria}\n\nRounds 1-3:\n"
            for h in history[-3:]:
                arbiter_user += f"\nAttempt {h['round']}:\n{h['attempt'][:300]}\nReviewer: {h['review'][:200]}\n"
            arbiter_user += f"\nFinal attempt:\n{attempt[:500]}\n\n"
            # The Arbiter was previously asked to rule on test-driven work
            # without ever being shown the test results.
            arbiter_user += f"Test results for the final attempt:\n{test_note}\n\nBreak the deadlock."
            try:
                ui.start_role("Arbiter", models["arbiter"])
                arbiter_review = complete(budget, models["arbiter"], arbiter_persona, arbiter_user, temperature=0.2, on_chunk=ui.stream_chunk)
                ui.end_role()
                arbiter_verdict, arbiter_reasoning = extract_verdict(arbiter_review)
                if arbiter_verdict == "APPROVE" and not sandbox_result["passed"]:
                    ui.status("  (Arbiter APPROVE overridden — tests are still failing)")
                    arbiter_verdict = "REJECT"
                    arbiter_reasoning = "Tests are still failing, so this cannot be approved."
                ui.status(f"  Arbiter: {arbiter_verdict}")
                history.append({"round": "arbiter", "attempt": attempt, "review": arbiter_review, "verdict": arbiter_verdict})
                if arbiter_verdict == "APPROVE":
                    _write_output(task, history, approved=True)
                    print(f"\n[OK] Arbiter approved. Output written to swarm_output/.")
                    return 0
                # Feed Arbiter's specific instruction, not the Reviewer's
                reasoning = arbiter_reasoning
            except RuntimeError as exc:
                print(f"Arbiter call failed: {exc}")

        feedback = reasoning

    _write_output(task, history, approved=False)
    print(
        f"\n[FAIL] Not approved after {max_rounds} rounds. Full round-by-round transcript written to "
        f"swarm_output/. Escalate this task to Claude Code instead of burning more free-tier tokens on it."
    )
    return 1


def _write_output(task: str, history: list[dict[str, str]], approved: bool) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    status = "approved" if approved else "escalate"
    path = OUTPUT_DIR / f"{stamp}-{status}.md"
    sections = [f"# Task\n\n{task}\n"]
    for entry in history:
        sections.append(f"## Round {entry['round']} — Coder\n\n{entry['attempt']}\n")
        sections.append(f"## Round {entry['round']} — Reviewer\n\n{entry['review']}\n")
    path.write_text("\n".join(sections), encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Swarm_Corp: multi-provider coding swarm")
    parser.add_argument("task", help="Task description")
    parser.add_argument("--repo", help="Optional: path to repo for context injection", default=None)
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Disable the Rich live UI (plain print()-per-line output; use for CI or piping).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Never execute a gated side effect; log what would have run instead.",
    )
    parser.add_argument(
        "--allow",
        default="",
        help=(
            "Comma-separated gate categories to pre-approve for this run "
            "(e.g. 'git_commit,write_outside_workspace'). Set before the run "
            "by you — nothing a model emits can add to this."
        ),
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help=(
            "Route every role to the local Ollama model instead of cloud "
            "providers, and disable network tools. Nothing leaves this "
            "machine. Family diversity does NOT hold in this mode — see "
            "the printed warning."
        ),
    )
    args = parser.parse_args()

    gates.configure(
        dry_run=args.dry_run,
        pre_allow={c.strip() for c in args.allow.split(",") if c.strip()},
    )

    ui.init(plain=args.plain)
    sys.exit(run_swarm(args.task, repo_path=args.repo, private=args.private))
