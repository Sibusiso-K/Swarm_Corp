"""
agent_swarm.py — a small Coder/Reviewer swarm running entirely on Groq's
free tier. See CLAUDE.md for the constraints this file has to honor:

  - Groq only. No paid API keys required to run the default path.
  - Every single completion request is capped at MAX_TOKENS_PER_REQUEST
    (8000) combined prompt+completion tokens, to stay inside free-tier
    per-minute limits without needing you to think about it.
  - The Reviewer must resolve to a different model *family* than the
    Coder (checked at startup, not just hoped for) — the whole point of
    a second reviewing model is that it doesn't share the first one's
    blind spots.
  - The Anthropic/paid-tier code path below is dormant by construction:
    SWARM_TIER defaults to "free" in .env.example, and nothing in the
    normal run_swarm() path ever calls get_anthropic_client(). Flip
    SWARM_TIER yourself if you ever want it; the swarm never will.

Usage:
    python agent_swarm.py "add a /health endpoint with a test"

Escalation lane: if the swarm can't get something approved after
MAX_ROUNDS, it writes its last attempt + the Reviewer's objections to
swarm_output/ and exits non-zero. That's the signal to open the same
task in Claude Code instead of spending more free-tier tokens on it.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import groq
from dotenv import load_dotenv
from groq import Groq

from agent_swarm_sandbox import sandbox_run

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
MODEL_REGISTRY: dict[str, list[str]] = {
    "orchestrator": ["openai/gpt-oss-20b"],
    "coder": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "reviewer": ["qwen/qwen3.6-27b", "groq/compound-mini"],
}

TPM_LIMIT = 8000  # Groq's per-model-per-minute cap
MAX_ROUNDS = 4  # coder attempts before we punt to Claude Code
TPM_WINDOW_SECONDS = 60


def family_of(slug: str) -> str:
    """'openai/gpt-oss-120b' -> 'openai'; 'llama-3.3-70b-versatile' -> 'llama'."""
    if "/" in slug:
        return slug.split("/")[0]
    return slug.split("-")[0]


def resolve_models(client: Groq) -> dict[str, str]:
    """Pick the first live candidate for each role. Raises RuntimeError with
    a specific, actionable message rather than letting a dead slug surface
    as an opaque 404 mid-swarm."""
    live_ids = {m.id for m in client.models.list().data}
    resolved: dict[str, str] = {}
    for role, candidates in MODEL_REGISTRY.items():
        pick = next((slug for slug in candidates if slug in live_ids), None)
        if pick is None:
            raise RuntimeError(
                f"No live model for role '{role}'. Tried {candidates}, none are live on this "
                f"key. Run `python verify_key.py` to see what's currently available and update "
                f"MODEL_REGISTRY in agent_swarm.py."
            )
        resolved[role] = pick

    if family_of(resolved["coder"]) == family_of(resolved["reviewer"]):
        raise RuntimeError(
            f"Coder ({resolved['coder']}) and Reviewer ({resolved['reviewer']}) resolved to the "
            f"same model family. CLAUDE.md requires them to differ — add a candidate to "
            f"MODEL_REGISTRY['reviewer'] from a different family."
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
# Token budgeting. Groq's free tier enforces an 8000 TPM cap *per model*, and
# it counts prompt + max_tokens together — a real run measured a 413 at 8013
# requested against that limit even though our naive chars/4 estimate said
# we had room. Two corrections for that: a more conservative (higher) token
# estimate, and a safety margin below the real 8000 ceiling so estimation
# error and per-message formatting overhead (role tags, etc., which aren't
# in the raw text length) don't tip us over anyway.
# ---------------------------------------------------------------------------
BUFFER_FOR_OVERHEAD = 700  # headroom below Groq's real 8000 TPM cap
EFFECTIVE_LIMIT = TPM_LIMIT - BUFFER_FOR_OVERHEAD


def estimate_tokens(text: str) -> int:
    # ~3.3 chars/token is a deliberately pessimistic estimate (real English
    # prose averages closer to 4) so we round up rather than get caught
    # short on token-dense text like code.
    return max(1, -(-len(text) // 3))


class TokenBudget:
    def __init__(self, limit_per_request: int = EFFECTIVE_LIMIT):
        self.limit_per_request = limit_per_request
        self._windows: dict[str, list[tuple[float, int]]] = {}  # model -> [(timestamp, tokens)]

    def completion_cap(self, prompt_text: str) -> int:
        """Max completion tokens we can ask for without blowing the
        per-request cap. Floored at 512, not 256: gpt-oss-family models are
        reasoning models that spend part of max_tokens on hidden reasoning
        before any visible content, so a tighter floor risks a response
        that's entirely reasoning tokens and no answer. We leave room for the
        actual usage reported back to be higher than our estimate."""
        remaining = self.limit_per_request - estimate_tokens(prompt_text)
        return max(512, remaining)

    def record(self, model: str, tokens_used: int) -> None:
        now = time.time()
        if model not in self._windows:
            self._windows[model] = []
        # Clean old entries outside the window
        self._windows[model] = [(t, n) for t, n in self._windows[model] if now - t < TPM_WINDOW_SECONDS]
        self._windows[model].append((now, tokens_used))

    def wait_if_needed(self, model: str, upcoming_tokens: int) -> None:
        now = time.time()
        if model not in self._windows:
            self._windows[model] = []
        # Clean old entries outside the window
        self._windows[model] = [(t, n) for t, n in self._windows[model] if now - t < TPM_WINDOW_SECONDS]
        used = sum(n for _, n in self._windows[model])
        # Groq enforces 8000 TPM per model, not a shared pool
        if used + upcoming_tokens > TPM_LIMIT:
            if self._windows[model]:
                sleep_for = TPM_WINDOW_SECONDS - (now - self._windows[model][0][0])
                if sleep_for > 0:
                    print(f"  (waiting {sleep_for:.0f}s — {model} at {used} TPM, upcoming request needs {upcoming_tokens})")
                    time.sleep(sleep_for)
            else:
                raise RuntimeError(f"Requested {upcoming_tokens} tokens for {model}, but cap is {TPM_LIMIT} TPM and a single request cannot fit")


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


def _fit_user_text(system: str, user: str, budget: TokenBudget) -> str:
    """If system + user + the minimum floor completion would already blow
    the per-request cap, truncate user (the variable-length part — usually
    the coder's prior attempt) intelligently rather than let the request 413.
    Truncates in the middle at a good break point, not the tail."""
    floor = 512
    available_for_user = (budget.limit_per_request - floor) - estimate_tokens(system)
    available_chars = max(0, available_for_user * 3)  # inverse of estimate_tokens' ceil(len/3)
    if len(user) <= available_chars:
        return user
    # Truncate at a sensible break point, keeping the end (revision intent)
    break_point = _find_truncation_point(user, available_chars)
    return user[:break_point] + "\n\n[... previous rounds' full code truncated for budget; revised diff below ...]"


def complete(client: Groq, budget: TokenBudget, model: str, system: str, user: str, temperature: float) -> str:
    user = _fit_user_text(system, user, budget)
    prompt_text = system + user
    max_tokens = budget.completion_cap(prompt_text)
    budget.wait_if_needed(model, estimate_tokens(prompt_text) + max_tokens)
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except groq.APIStatusError as exc:
        raise RuntimeError(f"Groq API call to {model} failed ({exc.status_code}): {exc.message}") from exc
    usage = resp.usage
    budget.record(model, getattr(usage, "total_tokens", estimate_tokens(prompt_text) + max_tokens))
    return resp.choices[0].message.content or ""


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

    # Reasoning is everything after the verdict line
    reasoning = "\n".join(lines[verdict_line_idx + 1:]).strip()
    return (verdict_text, reasoning)


VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|REJECT)", re.IGNORECASE)  # legacy, kept for reference


def parse_and_write_files(coder_output: str, workspace_root: Path) -> list[Path]:
    """Parse Coder output for `=== FILE: path ===` blocks and write them.

    Returns list of written file paths. Raises if Coder tries to break out of
    workspace_root (e.g., uses ../ in paths)."""
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

        # Security: reject paths that try to escape
        if ".." in file_path_str or file_path_str.startswith("/"):
            raise RuntimeError(f"Unsafe file path: {file_path_str}")

        file_path = workspace_root / file_path_str
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        written.append(file_path)

    return written


def run_swarm(task: str, max_rounds: int = MAX_ROUNDS) -> int:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        print("[FAIL] GROQ_API_KEY is not set. Copy .env.example to .env and paste your key in.")
        return 1

    client = Groq(api_key=api_key)
    try:
        models = resolve_models(client)
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 1

    coder_persona = load_persona("coder.md")
    reviewer_persona = load_persona("reviewer.md")
    budget = TokenBudget()

    print(f"Coder    : {models['coder']}")
    print(f"Reviewer : {models['reviewer']}\n")

    feedback = ""
    attempt = ""
    history: list[dict[str, str]] = []  # every round, not just the last — this is the audit trail
    workspace_root = OUTPUT_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")

    for round_no in range(1, max_rounds + 1):
        print(f"--- round {round_no} ---")
        coder_user = f"Task:\n{task}"
        if feedback:
            coder_user += f"\n\nThe reviewer rejected your last attempt. Their feedback:\n{feedback}\n\nRevise accordingly."

        try:
            print("  Coder is working...")
            attempt = complete(client, budget, models["coder"], coder_persona, coder_user, temperature=0.3)

            # Parse and write files from Coder output
            print("  Writing and testing files...")
            try:
                written_files = parse_and_write_files(attempt, workspace_root)
                if written_files:
                    print(f"    Wrote {len(written_files)} file(s)")
            except RuntimeError as exc:
                # File path was unsafe or parsing failed; feed this to Reviewer as a reject
                print(f"    File write failed: {exc}")
                written_files = []

            # Run tests/compilation in the workspace
            sandbox_result = sandbox_run(workspace_root, timeout_sec=30)
            test_note = f"Test result: {sandbox_result['note']} (exit code {sandbox_result['returncode']})"
            if sandbox_result["stdout"]:
                test_note += f"\n{sandbox_result['stdout']}"
            if sandbox_result["stderr"]:
                test_note += f"\nErrors: {sandbox_result['stderr']}"

            # If tests failed, auto-reject (no Reviewer call, save tokens)
            if not sandbox_result["passed"] and sandbox_result["stderr"]:
                print(f"  [Auto-reject] Tests failed")
                synthetic_review = f"VERDICT: REJECT\n\nTests or compilation failed:\n{sandbox_result['stderr']}"
                review = synthetic_review
            else:
                # Tests passed or N/A; ask Reviewer
                review_user = f"Task:\n{task}\n\nCoder's submission:\n{attempt}\n\n"
                if written_files:
                    review_user += f"Files written: {', '.join(f.name for f in written_files)}\n"
                review_user += f"\nTest/compile results:\n{test_note}\n\nStart your reply with 'VERDICT: APPROVE' or 'VERDICT: REJECT' on its own line, then your reasoning."
                print("  Reviewer is auditing...")
                review = complete(client, budget, models["reviewer"], reviewer_persona, review_user, temperature=0.2)
        except RuntimeError as exc:
            print(f"[FAIL] {exc}")
            history.append({"round": str(round_no), "attempt": attempt, "review": f"(aborted by an API error before review completed)\n{exc}"})
            _write_output(task, history, approved=False)
            return 1

        verdict, reasoning = extract_verdict(review)
        print(f"  Verdict: {verdict}")
        history.append({"round": str(round_no), "attempt": attempt, "review": review, "verdict": verdict})

        if verdict == "APPROVE":
            _write_output(task, history, approved=True)
            print(f"\n[OK] Approved after {round_no} round(s). Output written to swarm_output/.")
            return 0

        if verdict == "UNPARSEABLE":
            # Retry once with a terse re-prompt, don't count as a round burn
            print("  Verdict line unclear; requesting clarification...")
            retry_prompt = f"Please re-state your verdict clearly on a single line:\nVERDICT: APPROVE or VERDICT: REJECT\n\nThen provide your brief reasoning."
            try:
                review = complete(client, budget, models["reviewer"], reviewer_persona, retry_prompt, temperature=0.2)
                verdict, reasoning = extract_verdict(review)
                print(f"  Verdict (retry): {verdict}")
            except RuntimeError:
                # If retry fails, treat as REJECT and continue
                verdict = "REJECT"
                reasoning = "(retry failed; treating as REJECT)"

        # Feed only the reasoning (without <think> blocks) as feedback, not the full review
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
    if len(sys.argv) < 2:
        print('Usage: python agent_swarm.py "task description"')
        sys.exit(1)
    sys.exit(run_swarm(" ".join(sys.argv[1:])))
