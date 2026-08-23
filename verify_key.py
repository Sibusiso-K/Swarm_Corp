"""
verify_key.py — sanity-check the Groq key and the model registry before
agent_swarm.py ever runs.

What it does, in order:
  1. Loads .env and confirms GROQ_API_KEY is set.
  2. Calls Groq's live /models endpoint (this is the part that catches slug
     drift — model names on Groq change more often than this file does).
  3. For each role in MODEL_REGISTRY, walks its candidate list and picks the
     first slug that's actually live. Prints what it picked.
  4. Fails loudly (exit 1) if a role has zero live candidates, or if the
     Coder and Reviewer resolved to the same model family — that's a hard
     constraint from CLAUDE.md, not a suggestion.
  5. Does a tiny real completion call (a couple tokens) against the resolved
     Coder model, just to prove the key itself works end to end.

Run this after every "it stopped working" — 90% of the time it's a dead slug.
"""

import os
import sys

from dotenv import load_dotenv
from groq import Groq

from agent_swarm import MODEL_REGISTRY, family_of

# Windows terminals often default to cp1252, which can't encode the em-dashes
# and other punctuation used in these messages. Force UTF-8 on stdout so
# output doesn't get mangled into replacement characters.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()


def main() -> int:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        print("[FAIL] GROQ_API_KEY is not set. Copy .env.example to .env and paste your key in.")
        return 1

    client = Groq(api_key=api_key)

    print("Checking key + fetching live model list from Groq...")
    try:
        live = client.models.list()
    except Exception as exc:  # noqa: BLE001 - we want to show the raw error to the user
        print(f"[FAIL] Could not reach Groq / key rejected: {exc}")
        return 1

    live_ids = {m.id for m in live.data}
    print(f"  {len(live_ids)} models currently live on this key.\n")

    resolved: dict[str, str] = {}
    ok = True

    for role, candidates in MODEL_REGISTRY.items():
        pick = next((slug for slug in candidates if slug in live_ids), None)
        if pick is None:
            ok = False
            print(f"[FAIL] {role}: none of {candidates} are live. Update MODEL_REGISTRY in agent_swarm.py.")
            print(f"  Live models containing a hint of what you might want:")
            for m in sorted(live_ids):
                print(f"    - {m}")
            continue
        resolved[role] = pick
        stale = "" if pick == candidates[0] else "  (fell back — first choice is dead, update the registry)"
        print(f"[OK] {role}: {pick}{stale}")

    if not ok:
        return 1

    coder_family = family_of(resolved["coder"])
    reviewer_family = family_of(resolved["reviewer"])
    if coder_family == reviewer_family:
        print(
            f"\n[FAIL] Coder ({resolved['coder']}) and Reviewer ({resolved['reviewer']}) are both "
            f"'{coder_family}' family. CLAUDE.md requires the Reviewer to be a different model "
            f"family than the Coder — a model can't reliably audit its own family's blind spots."
        )
        return 1
    print(f"\n[OK] Coder family '{coder_family}' != Reviewer family '{reviewer_family}' — constraint satisfied.")

    print("\nRunning a small completion against the Coder model to confirm the key actually works...")
    resp = client.chat.completions.create(
        model=resolved["coder"],
        messages=[{"role": "user", "content": "Say OK."}],
        max_tokens=50,  # gpt-oss models spend some of this on hidden reasoning
        # tokens before any visible content — 5 was too tight to see output.
    )
    print(f"[OK] Live response: {resp.choices[0].message.content!r}")

    print("\nAll checks passed. Run: python agent_swarm.py \"your task here\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
