"""
bench_strategies.py — compare zero/one/few-shot prompting on a single role,
starting with the Planner (cheapest, most directly measurable: its output
is a fixed acceptance-criteria list, not a whole codebase).

Scoped deliberately small: running the FULL 6-role swarm once per strategy
would multiply the cost of every benchmark by however many strategies are
compared. Measuring one role's output quality in isolation (1 API call per
strategy instead of an entire swarm run) gets the actual question —
"does adding examples change this role's output for the better" —
answered at a fraction of the cost, and the same pattern generalizes to
any other role later without a redesign.

Usage:
    python -m bench.bench_strategies "write a rate limiter for an API"
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from swarm_corp import complete, TokenBudget, load_persona, resolve_models

RESULTS_PATH = Path(__file__).parent / "results.jsonl"

# Worked examples for the Planner, used to build one-shot/few-shot variants.
# Kept short — these get prepended to the persona on every call in that mode.
_EXAMPLES = [
    (
        "Task: write a function that validates an email address.",
        "- Rejects strings with no '@' or no domain part\n"
        "- Accepts standard local@domain.tld shape\n"
        "- Rejects consecutive or leading/trailing dots\n"
        "- Returns a bool, never raises on malformed input",
    ),
    (
        "Task: write a function that finds the median of a list of numbers.",
        "- Works for both even and odd-length lists\n"
        "- Handles an empty list explicitly (documented behavior, not a crash)\n"
        "- Does not mutate the input list\n"
        "- Returns a float even when the input is all integers",
    ),
    (
        "Task: write a rate limiter for an API endpoint.",
        "- Enforces a max request count per fixed time window per client\n"
        "- Returns a specific rejection response, not a generic error, when limited\n"
        "- Resets correctly at window boundaries (no off-by-one on the edge)\n"
        "- Thread-safe under concurrent requests",
    ),
]


def _persona_for(strategy: str) -> str:
    base = load_persona("planner.md")
    if strategy == "zero-shot":
        return base
    n = {"one-shot": 1, "few-shot": 3}[strategy]
    examples = "\n\n".join(f"{t}\nCriteria:\n{c}" for t, c in _EXAMPLES[:n])
    return f"{base}\n\nExamples of good acceptance criteria:\n\n{examples}"


def run_comparison(task: str, strategies: list[str] | None = None) -> list[dict]:
    strategies = strategies or ["zero-shot", "one-shot", "few-shot"]
    models = resolve_models()
    planner_ref = models["planner"]
    results = []
    for strategy in strategies:
        persona = _persona_for(strategy)
        budget = TokenBudget()
        start = time.time()
        try:
            criteria = complete(budget, planner_ref, persona, f"Task:\n{task}", temperature=0.3)
            ok = True
        except RuntimeError as exc:
            criteria = str(exc)
            ok = False
        elapsed = time.time() - start
        criterion_lines = [line for line in criteria.split("\n") if line.strip().startswith("-")]
        record = {
            "task": task,
            "strategy": strategy,
            "model": planner_ref,
            "ok": ok,
            "wall_clock_sec": round(elapsed, 2),
            "criterion_count": len(criterion_lines),
            "criteria": criteria,
        }
        results.append(record)
        with open(RESULTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return results


def print_comparison(results: list[dict]) -> None:
    print(f"{'strategy':10s} {'ok':4s} {'sec':6s} {'criteria':8s}")
    for r in results:
        print(f"{r['strategy']:10s} {str(r['ok']):4s} {r['wall_clock_sec']:<6.2f} {r['criterion_count']:<8d}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m bench.bench_strategies \"<task>\"")
        sys.exit(1)
    results = run_comparison(sys.argv[1])
    print_comparison(results)
    print(f"\nAppended to {RESULTS_PATH}")
