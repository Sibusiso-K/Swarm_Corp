"""
verify_key.py — sanity-check every configured provider key and the model
registry before swarm_corp.py ever runs.

What it does, in order:
  1. Loads .env and reports which providers have a key set (Groq is the only
     one required; Cerebras/NVIDIA/Gemini are optional extras).
  2. Calls each active provider's live /models endpoint and unions the
     result — this is the part that catches slug drift, and it's also what
     caught two providers listing models they then refused to serve.
  3. For each role in MODEL_REGISTRY, walks its candidate list and picks the
     first ref that's actually live. Prints what it picked.
  4. Fails loudly (exit 1) if a role has zero live candidates, or if the
     Coder/Reviewer/Tester/Arbiter family-diversity constraints aren't met.
  5. Does a tiny real completion call against the resolved Coder model, to
     prove the key doesn't just list models but can actually serve them —
     a provider can 200 on /models and still 402/404 on every real call.

Run this after every "it stopped working" — usually a dead slug, or (as
happened once) a model that's listed but not actually enabled on your key.
"""

import sys

from dotenv import load_dotenv

from providers import available_providers
from swarm_corp import MODEL_REGISTRY, family_of, live_model_refs, resolve_models, complete, TokenBudget

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()


def main() -> int:
    providers = available_providers()
    if not providers:
        print("[FAIL] No provider API keys are set. Copy .env.example to .env and paste GROQ_API_KEY in.")
        return 1
    print(f"Configured providers: {', '.join(providers)}\n")

    print("Fetching live model lists from each provider...")
    live_ids = live_model_refs()
    print(f"  {len(live_ids)} models currently live across all configured providers.\n")

    ok = True
    for role, candidates in MODEL_REGISTRY.items():
        pick = next((ref for ref in candidates if ref in live_ids), None)
        if pick is None:
            ok = False
            print(f"[FAIL] {role}: none of {candidates} are live. Update MODEL_REGISTRY in swarm_corp.py.")
            continue
        stale = "" if pick == candidates[0] else "  (fell back — check higher-priority candidates)"
        print(f"[OK] {role}: {pick}{stale}")

    if not ok:
        return 1

    try:
        resolved = resolve_models(live_ids)
    except RuntimeError as exc:
        print(f"\n[FAIL] {exc}")
        return 1

    print(f"\n[OK] Family diversity constraints satisfied across all roles.")

    print("\nRunning a small completion against the Coder model to confirm it actually serves, not just lists...")
    try:
        resp = complete(TokenBudget(), resolved["coder"], "You are terse.", "Say OK.", temperature=0.0)
        print(f"[OK] Live response from {resolved['coder']}: {resp[:80]!r}")
    except RuntimeError as exc:
        print(f"[FAIL] Coder model is listed as live but the call failed: {exc}")
        print("  This is exactly the kind of gap live_model_refs() can't catch on its own —")
        print("  a provider can list a model without actually having it enabled on your key.")
        return 1

    print("\nAll checks passed. Run: python swarm_corp.py \"your task here\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
